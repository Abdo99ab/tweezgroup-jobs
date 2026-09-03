"""ClickUp sync: one task per applicant in the role's recruiting list.

On creation the task is assigned to CLICKUP_ASSIGNEE (Mehdi Mahcene) and, once the CV auto-summary exists,
a comment with the summary is posted tagging CLICKUP_MENTIONS (Ahmidou, Taoufik Mousselmal,
Abderrahmane Hammia) for visibility. Status changes are pushed with the board's exact status names.
"""
import logging
import os
import threading

import requests
from flask import current_app

from .models import log_event

log = logging.getLogger(__name__)
API = os.environ.get("CLICKUP_API_BASE", "https://api.clickup.com/api/v2")

# Our status slug -> exact status name in the ClickUp recruiting lists.
STATUS_MAP = {
    "filtered": "FILTRED APPLICATION",
    "selected": "SELECTED/ IN PROGRESS",
    "test_sent": "TEST SENT",
    "test_returned": "TEST RETURNED",
    "interview_done": "INTERVIEW DONE",
    "interview2_done": "2ND INTERVIEW DONE",
    "contract_sent": "CONTRACT SENT",
    "rejected": "REJECTED",
    "hired": "HIRED",
}

_members_cache = {"data": None}
_members_lock = threading.Lock()


def _headers():
    return {"Authorization": current_app.config["CLICKUP_API_TOKEN"], "Content-Type": "application/json"}


def _enabled():
    return current_app.config["CLICKUP_ENABLED"] and current_app.config["CLICKUP_API_TOKEN"]


def _list_id_for(applicant):
    return applicant.role.clickup_list_id or current_app.config["CLICKUP_LIST_ID"]


# ---------------------------------------------------------------------------------------- people

def members():
    """Workspace members: list of {id, username, email}. Cached for the process lifetime."""
    with _members_lock:
        if _members_cache["data"] is not None:
            return _members_cache["data"]
        out = []
        try:
            r = requests.get(f"{API}/team", headers=_headers(), timeout=15)
            r.raise_for_status()
            team_id = str(current_app.config["CLICKUP_TEAM_ID"])
            for team in r.json().get("teams", []):
                if team_id and str(team.get("id")) != team_id:
                    continue
                for m in team.get("members", []):
                    u = m.get("user", {})
                    out.append({"id": u.get("id"), "username": u.get("username") or "", "email": u.get("email") or ""})
        except Exception as exc:
            log.warning("ClickUp: could not load team members: %s", exc)
            return []
        _members_cache["data"] = out
        return out


def resolve_user(name_or_email):
    """Match a display name or email (case-insensitive; partial name allowed) to a member id."""
    if not name_or_email:
        return None
    want = name_or_email.strip().lower()
    if want.isdigit():
        return int(want)
    people = members()
    for m in people:  # exact
        if m["username"].lower() == want or m["email"].lower() == want:
            return m["id"]
    for m in people:  # partial ("Ahmidou" in "Ahmidou Benali")
        if want in m["username"].lower() or want in m["email"].lower():
            return m["id"]
    log.warning("ClickUp: no member matches %r", name_or_email)
    return None


def assignee_id():
    return resolve_user(current_app.config["CLICKUP_ASSIGNEE"])


def mention_ids():
    names = [n.strip() for n in current_app.config["CLICKUP_MENTIONS"].split(",") if n.strip()]
    return [(n, resolve_user(n)) for n in names]


# ----------------------------------------------------------------------------------------- tasks

def _description(applicant):
    admin_url = f"{current_app.config['PUBLIC_BASE_URL']}/admin/applicants/{applicant.public_id}"
    lines = [
        f"**Role:** {applicant.role.title}",
        f"**Email:** {applicant.email}",
        f"**Phone:** {applicant.phone or '-'}",
        f"**LinkedIn:** {applicant.linkedin_url or '-'}",
        f"**Location:** {applicant.location or '-'}",
        f"**Experience in this field:** {applicant.years_experience if applicant.years_experience is not None else '-'} years",
        f"**Source:** {applicant.source}",
        f"**Applied:** {applicant.created_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"**CV (Google Drive):** {applicant.cv_url or '-'}",
        f"**Applicant record:** {admin_url}",
        f"Applicant ID: {applicant.public_id}",
    ]
    if applicant.cover_note:
        lines += ["", "**Cover note:**", applicant.cover_note]
    return "\n".join(lines)


def create_task(applicant, force=False):
    """Create the ClickUp task (assigned to CLICKUP_ASSIGNEE). Never raises; logs an event either way."""
    if not _enabled() or applicant.clickup_task_id:
        return None
    if not force and current_app.config["CLICKUP_CREATE_ON"] != "apply" and applicant.status not in STATUS_MAP:
        return None
    list_id = _list_id_for(applicant)
    if not list_id:
        log_event(applicant, "error", "ClickUp: no list id configured for this role")
        return None
    payload = {
        "name": f"{applicant.full_name} — {applicant.role.title}",
        "markdown_description": _description(applicant),
        "tags": ["applicant", applicant.role.slug],
        "notify_all": True,
    }
    if applicant.status in STATUS_MAP:
        payload["status"] = STATUS_MAP[applicant.status]
    a_id = assignee_id()
    if a_id:
        payload["assignees"] = [a_id]
    try:
        r = requests.post(f"{API}/list/{list_id}/task", json=payload, headers=_headers(), timeout=20)
        if r.status_code >= 400 and "status" in r.text.lower() and "status" in payload:
            payload.pop("status")  # list lacks this status name -> default column
            r = requests.post(f"{API}/list/{list_id}/task", json=payload, headers=_headers(), timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"ClickUp {r.status_code} creating task in list {list_id}: {r.text[:400]}")
        data = r.json()
        applicant.clickup_task_id = data.get("id")
        applicant.clickup_task_url = data.get("url")
        who = f", assigned to {current_app.config['CLICKUP_ASSIGNEE']}" if a_id else ", assignee not found"
        log_event(applicant, "clickup_synced", f"Task {applicant.clickup_task_id} created in list {list_id}{who}")
        return data
    except Exception as exc:
        log.warning("ClickUp task creation failed: %s", exc)
        log_event(applicant, "error", f"ClickUp task creation failed: {exc}")
        return None


def sync_status(applicant):
    """After any status change: create the task if it doesn't exist yet, else update its status."""
    if not _enabled():
        return None
    if not applicant.clickup_task_id:
        return create_task(applicant)
    if applicant.status not in STATUS_MAP:
        return None
    payload = {"status": STATUS_MAP[applicant.status]}
    try:
        r = requests.put(f"{API}/task/{applicant.clickup_task_id}", json=payload, headers=_headers(), timeout=15)
        r.raise_for_status()
        log_event(applicant, "clickup_synced", f"Status -> {STATUS_MAP[applicant.status]}")
    except Exception as exc:
        log.warning("ClickUp status update failed: %s", exc)
        log_event(applicant, "error", f"ClickUp status update failed: {exc}")


update_task_status = sync_status  # backwards-compatible alias


# -------------------------------------------------------------------------------------- comments

REVERSE_STATUS_MAP = {v.lower(): k for k, v in STATUS_MAP.items()}


def status_from_clickup(name):
    """ClickUp status name -> our slug (case-insensitive), or None if unknown."""
    return REVERSE_STATUS_MAP.get((name or "").strip().lower())


def post_comment(applicant, text, mentions=True, label="Summary comment"):
    """Post a comment on the applicant's task. With mentions=True the configured people are @-tagged.

    ClickUp's rich comment format (`comment` array with type=tag items) renders real mentions; if the
    API rejects it we fall back to plain text so the summary is never lost.
    """
    if not _enabled() or not applicant.clickup_task_id:
        return None
    url = f"{API}/task/{applicant.clickup_task_id}/comment"
    tagged = [(n, uid) for n, uid in mention_ids() if uid] if mentions else []
    try:
        if tagged:
            parts = [{"text": text + "\n\ncc "}]
            for i, (name, uid) in enumerate(tagged):
                parts.append({"text": f"@{name}", "type": "tag", "user": {"id": uid}})
                parts.append({"text": ", " if i < len(tagged) - 1 else ""})
            r = requests.post(url, json={"comment": parts, "notify_all": True}, headers=_headers(), timeout=15)
            if r.status_code < 400:
                log_event(applicant, "clickup_synced",
                          f"{label} posted, tagged " + ", ".join(n for n, _ in tagged))
                return r.json()
            log.warning("ClickUp rich comment rejected (%s): %s — falling back to plain text", r.status_code, r.text[:200])
        plain = text + ("\n\ncc " + ", ".join(f"@{n}" for n, _ in tagged) if tagged else "")
        r = requests.post(url, json={"comment_text": plain, "notify_all": True}, headers=_headers(), timeout=15)
        r.raise_for_status()
        log_event(applicant, "clickup_synced", f"{label} posted (plain text)")
        return r.json()
    except Exception as exc:
        log.warning("ClickUp comment failed: %s", exc)
        log_event(applicant, "error", f"ClickUp comment failed: {exc}")
        return None
