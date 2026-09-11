"""Sourcing & outreach (Milestone 1) — the brain behind the browser worker.

The app decides everything; the worker only does browser actions:

  search (admin) -> worker runs it -> POST results -> Claude pre-scores each profile
  -> recruiter approves a shortlist -> drafts rendered from the role's templates
  -> queue: connect -> check_accept -> message (with the personalised apply link) -> reminder
  -> candidate opens /r/<token> -> pre-filled apply form -> uploads CV -> normal pipeline (M3/M4)

Human-confirm mode (default): each send waits for the recruiter to click "Send" (released=True).
Roles with sourcing_autosend=True release automatically. Daily caps, warm-up and the pause-on-warning
switch are all enforced here, so a buggy or over-eager worker cannot exceed them.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta

import requests
from flask import current_app

from . import channels, mailer, regions
from .models import (ACTIONS, CHANNEL_SEARCH, Applicant, Role, Setting, SourcedProfile, SourcingAction,
                     SourcingSearch, db, log_event, utcnow)

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ------------------------------------------------------------------------------------ templates

DEFAULT_CONNECTION = (
    "Hi {first_name}, I'm hiring a {role} for {company} (e-commerce brands in Europe and the US). "
    "{icebreaker} Would love to connect and share the details."
)
DEFAULT_MESSAGE = """Hi {first_name}, thanks for connecting!

We are hiring a {role} at {company}. {icebreaker}

If you are open to it, the fastest way to apply is this personal link (takes 2 minutes, just upload your CV):
{apply_url}

Happy to answer any question here.
{sender}"""
DEFAULT_REMINDER = """Hi {first_name}, just a quick nudge in case my last message got buried — the {role} position at {company} is still open and your personal application link is here:
{apply_url}

No worries if the timing isn't right.
{sender}"""
DEFAULT_REFERRAL_EMAIL = """Hello {first_name},

{referred_by} from {company} suggested you might be a great fit for our {role} position and asked us to reach out.

You can apply in two minutes with this personal link (just upload your CV):
{apply_url}

Looking forward to reading your profile.
The {company} recruitment team"""


class _Safe(dict):
    def __missing__(self, key):
        return ""


def apply_url(profile):
    return f"{current_app.config['PUBLIC_BASE_URL']}/r/{profile.apply_token}"


def render(template, profile, extra=None):
    role = profile.role
    cfg = current_app.config
    values = _Safe(
        first_name=profile.first_name or "there",
        full_name=profile.full_name or "",
        role=role.title,
        company=cfg["COMPANY_NAME"],
        apply_url=apply_url(profile),
        icebreaker=(profile.icebreaker or "").strip(),
        sender=cfg["SOURCING_SENDER_NAME"] or "",
        referred_by=profile.referred_by or "a colleague",
        location=profile.location or "",
        company_name=profile.company or "",
    )
    if extra:
        values.update(extra)
    text = (template or "").format_map(values)
    text = re.sub(r"[ \t]{2,}", " ", text)            # double spaces left by an empty {icebreaker}
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def prepare_drafts(profile, force=False):
    """Fill the three drafts from the role's templates (kept editable by the recruiter)."""
    role = profile.role
    if force or not profile.draft_note:
        note = render(role.msg_connection or DEFAULT_CONNECTION, profile)
        if len(note) > 300:  # LinkedIn's hard limit for connection notes
            note = render(role.msg_connection or DEFAULT_CONNECTION, profile, {"icebreaker": ""})
        profile.draft_note = note[:300]
    if force or not profile.draft_message:
        profile.draft_message = render(role.msg_message or DEFAULT_MESSAGE, profile)
    if force or not profile.draft_reminder:
        profile.draft_reminder = render(role.msg_reminder or DEFAULT_REMINDER, profile)


# ------------------------------------------------------------------------------------ Claude

PRESCORE_PROMPT = """You are helping a recruiter at {company} shortlist people found on {channel} for this role.

ROLE: {title}
WHAT WE ARE LOOKING FOR:
{requirements}

TARGET LOCATIONS: {targets} (a candidate clearly outside these, or who cannot work our hours, should lose points)

PUBLIC PROFILE (only what a search result shows — be fair about missing information):
Name: {name}
Headline: {headline}
Location: {location}
Current company: {company_name}
Extra: {about}

Return ONLY a JSON object:
- "score": integer 0-100, likelihood this person fits the role (50 = unclear, 80+ = clearly relevant)
- "reason": one sentence, factual, mentioning the strongest signal and the main doubt
- "icebreaker": one short, natural sentence (max 120 characters) that references something specific in their
  headline or company and could open a friendly message. No flattery, no exclamation marks. Empty string if nothing specific.
"""

SEARCH_PROMPT = """You are a technical sourcer. Propose LinkedIn people-search queries for this role.

ROLE: {title}
LOCATION / SETUP: {location}
WHAT WE ARE LOOKING FOR:
{requirements}
RECRUITER'S BRIEF (optional):
{brief}

Return ONLY a JSON object with "searches": a list of 3 to 5 objects, each {{"query": "...", "location": "...", "why": "..."}}.
"query" is what to type in LinkedIn's search box: title variants and must-have keywords, using quotes, AND/OR/NOT.
Keep each query under 150 characters. Vary the angle (title synonyms, adjacent roles, key tools, language).
"""

CLASSIFY_PROMPT = """A candidate replied on LinkedIn to a recruiter's message about a {title} position.

REPLY:
\"\"\"{reply}\"\"\"

Classify it. Return ONLY a JSON object: {{"class": one of "interested", "question", "not_now", "negative", "other",
"summary": "one short sentence"}}.
"""


def _claude(prompt, max_tokens=1024):
    cfg = current_app.config
    if not cfg["ANTHROPIC_API_KEY"]:
        return None
    r = requests.post(
        f"{cfg['ANTHROPIC_API_BASE']}/v1/messages",
        headers={"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["SUMMARY_MODEL"], "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:300]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") != "thinking")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def prescore(profile):
    cfg = current_app.config
    if not cfg["SOURCING_PRESCORE_ENABLED"]:
        return None
    role = profile.role
    data = _claude(PRESCORE_PROMPT.format(
        company=cfg["COMPANY_NAME"], channel=profile.channel, title=role.title,
        requirements=(role.requirements or "(no requirements written yet — judge relevance of the title)")[:8000],
        name=profile.full_name, headline=profile.headline or "-", location=profile.location or "-",
        company_name=profile.company or "-", about=(profile.about or "-")[:3000],
        targets=regions.describe(role.target_regions, role.target_custom),
    ))
    if not data:
        return None
    profile.pre_score = max(0, min(100, int(data.get("score", 0))))
    profile.pre_reason = (data.get("reason") or "")[:1000]
    profile.icebreaker = (data.get("icebreaker") or "")[:300]
    return data


def suggest_searches(role):
    """Claude proposes 3-5 LinkedIn queries from the role's requirements (+ optional brief)."""
    cfg = current_app.config
    data = _claude(SEARCH_PROMPT.format(
        title=role.title, location=regions.describe(role.target_regions, role.target_custom)
        if (role.target_regions or role.target_custom) else (role.location or "remote / France / Algeria"),
        requirements=(role.requirements or "-")[:8000], brief=(role.sourcing_brief or "-")[:3000],
    ), max_tokens=1500)
    return (data or {}).get("searches", [])


def classify_reply(profile):
    """Label a pasted/inbox reply. Keyword fallback when Claude is off (tests / no API key)."""
    text = (profile.reply_text or "").lower()
    if any(s in text for s in ("not interested", "no thank", "pas intéress", "non merci")):
        guessed = "negative"
    elif any(s in text for s in ("tell me more", "sounds interesting", "intéressé")):
        guessed = "interested"
    else:
        guessed = "other"
    if not current_app.config["SOURCING_PRESCORE_ENABLED"]:
        profile.reply_class = guessed
        return profile.reply_class
    try:
        data = _claude(CLASSIFY_PROMPT.format(title=profile.role.title, reply=(profile.reply_text or "")[:3000]),
                       max_tokens=300)
    except Exception as extra:
        log.warning("reply classification failed: %s", extra)
        data = None
    profile.reply_class = (data or {}).get("class", guessed)
    return profile.reply_class

# ------------------------------------------------------------------------------------ results

def ingest_results(search, profiles, done=False, error=None):
    """Upsert scraped profiles for a search; new ones are pre-scored in a background thread."""
    role = search.role
    new_ids = []
    for p in profiles:
        url = _clean_url(p.get("profile_url"))
        name = (p.get("full_name") or "").strip()
        if not url or not name:
            continue
        row = SourcedProfile.query.filter_by(role_id=role.id, profile_url=url).first()
        if row is None:
            row = SourcedProfile(role=role, search=search, channel=p.get("channel") or search.channel,
                                 profile_url=url, full_name=name)
            db.session.add(row)
            new_ids.append(row)
        row.headline = (p.get("headline") or row.headline or "")[:400] or None
        row.location = (p.get("location") or row.location or "")[:200] or None
        row.company = (p.get("company") or row.company or "")[:200] or None
        if p.get("about"):
            row.about = p["about"][:5000]
        if p.get("email"):
            row.email = p["email"].strip().lower()[:200]
    search.results_count = (search.results_count or 0) + len(new_ids)
    search.status = "error" if error else ("done" if done else "running")
    search.error = error
    if search.started_at is None:
        search.started_at = utcnow()
    if done or error:
        search.finished_at = utcnow()
    db.session.commit()
    ids = [r.id for r in new_ids]
    if ids and current_app.config["SOURCING_PRESCORE_ENABLED"]:
        app = current_app._get_current_object()
        if current_app.config.get("PROCESS_ASYNC", True) and not current_app.config.get("TESTING"):
            threading.Thread(target=_prescore_many, args=(app, ids), daemon=True).start()
        else:
            _prescore_many(app, ids)
    return len(new_ids)


def create_searches(role, keywords, channel="linkedin", region_keys=None, custom=None, max_results=50,
                    with_cities=False, status="pending"):
    """One SourcingSearch per target location (regions expanded per channel). No location -> one search."""
    locs = regions.expand(region_keys, channel=channel, custom=custom, with_cities=with_cities)
    region_of = {}
    for k in regions.parse(region_keys):
        for c in regions.REGIONS[k]["countries"] + (regions.REGIONS[k]["cities"] if with_cities else []):
            region_of.setdefault(c, k)
    created = []
    for loc in (locs or [None]):
        s = SourcingSearch(role=role, channel=channel, keywords=keywords[:400], location=loc,
                           region=region_of.get(loc) if loc else ("remote" if regions.is_remote(region_keys) else None),
                           max_results=max_results, status=status)
        db.session.add(s)
        created.append(s)
    db.session.flush()
    return created


def start_search(search):
    """API channels (GitHub) run inside the app right away; worker channels wait for the worker."""
    if CHANNEL_SEARCH.get(search.channel) != "api":
        return False
    search.status = "running"
    search.started_at = utcnow()
    db.session.commit()
    app = current_app._get_current_object()
    if current_app.config.get("PROCESS_ASYNC", True) and not current_app.config.get("TESTING"):
        threading.Thread(target=_run_api_search_bg, args=(app, search.id), daemon=True).start()
    else:
        _run_api_search(search)
    return True


def _run_api_search_bg(app, search_id):
    with app.app_context():
        s = db.session.get(SourcingSearch, search_id)
        if s:
            _run_api_search(s)


def _run_api_search(s):
    try:
        profiles = channels.run_search(s)
    except Exception as exc:
        log.warning("API search %s failed: %s", s.id, exc)
        ingest_results(s, [], done=True, error=str(exc)[:300])
        return
    ingest_results(s, profiles, done=True)


def import_profiles(role, urls, channel=None):
    """Recruiter pasted profile URLs (Behance, ArtStation, Kaggle, Contra, GitHub, …): enrich + pre-score."""
    infos = channels.import_urls(urls)
    if channel and channel != "auto":
        for i in infos:
            i["channel"] = channel
    label = f"import: {len(infos)} profile(s)"
    s = SourcingSearch(role=role, channel=(channel if channel and channel != "auto" else
                                           (infos[0]["channel"] if infos else "manual")),
                       keywords=label, max_results=len(infos) or 1, status="running")
    db.session.add(s)
    db.session.flush()
    n = ingest_results(s, infos, done=True)
    return s, n


def email_link(profile, actor="admin"):
    """Send the drafted message (with the personal link) by email — for channels without automation."""
    if not profile.email:
        return False, "no email on this profile"
    prepare_drafts(profile)
    subject = f"{current_app.config['COMPANY_NAME']} — {profile.role.title} position"
    ok, err = mailer.send(profile.email, subject, profile.draft_message)
    if ok:
        action = "reminder" if profile.message_sent_at else "message"
        record_outcome(profile, action, ok=True, detail=f"emailed to {profile.email}", actor=actor)
    else:
        _log(profile, "email", False, err, actor=actor)
    return ok, err


def _prescore_many(app, ids):
    with app.app_context():
        for pid in ids:
            p = db.session.get(SourcedProfile, pid)
            if not p or p.pre_score is not None:
                continue
            try:
                prescore(p)
            except Exception as exc:
                log.warning("prescore failed for %s: %s", p.profile_url, exc)
                p.pre_reason = f"pre-score failed: {exc}"[:500]
            db.session.commit()


def _clean_url(url):
    if not url:
        return None
    url = url.strip().split("?")[0].rstrip("/")
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url[:400]


# ------------------------------------------------------------------------------------ decisions

def approve(profile, actor="admin"):
    role = profile.role
    profile.decision = "approved"
    profile.decided_at = utcnow()
    prepare_drafts(profile)
    if profile.channel == "linkedin":
        profile.next_action = "connect"
    else:
        # channels where we already have a way to message (email known): go straight to the link message
        profile.next_action = "message"
    profile.next_due_at = utcnow()
    profile.released = bool(role.sourcing_autosend)
    profile.closed_at = None
    profile.close_reason = None
    _log(profile, "approved", True, f"approved by {actor}; next={profile.next_action}", actor=actor)


def reject(profile, actor="admin", reason="rejected by recruiter"):
    profile.decision = "rejected"
    profile.decided_at = utcnow()
    _close(profile, reason)
    _log(profile, "rejected", True, reason, actor=actor)


def release(profile, actor="admin"):
    """Human confirmation: the pending action may now be executed by the worker."""
    if profile.next_action:
        profile.released = True
        _log(profile, "released", True, f"{profile.next_action} released by {actor}", actor=actor)


def hold(profile, actor="admin"):
    profile.released = False


def _close(profile, reason):
    profile.next_action = None
    profile.next_due_at = None
    profile.released = False
    profile.lock_at = None
    profile.closed_at = utcnow()
    profile.close_reason = reason[:60]


def _log(profile, action, ok, detail=None, actor="worker"):
    db.session.add(SourcingAction(profile=profile, action=action, ok=ok, detail=detail, actor=actor))


# ------------------------------------------------------------------------------------ caps / pause

def _today_start():
    cfg = current_app.config
    if ZoneInfo:
        tz = ZoneInfo(cfg["SOURCING_TZ"])
        local = datetime.now(tz)
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def caps():
    """Today's usage vs limits. Views = every profile visit (connect/check/message/reminder/view)."""
    cfg = current_app.config
    start = _today_start()
    q = SourcingAction.query.filter(SourcingAction.created_at >= start, SourcingAction.ok.is_(True),
                                    SourcingAction.actor == "worker")
    connects = q.filter(SourcingAction.action == "connect").count()
    messages = q.filter(SourcingAction.action.in_(("message", "reminder"))).count()
    views = q.filter(SourcingAction.action.in_(("connect", "check_accept", "message", "reminder", "view"))).count()
    first = db.session.query(db.func.min(SourcingAction.created_at)).filter(SourcingAction.actor == "worker").scalar()
    warmup = cfg["SOURCING_WARMUP_DAYS"] > 0 and (first is None or (utcnow() - first).days < cfg["SOURCING_WARMUP_DAYS"])
    factor = 0.5 if warmup else 1.0
    limits = {
        "connect": max(1, int(cfg["SOURCING_CAP_CONNECTS"] * factor)),
        "message": max(1, int(cfg["SOURCING_CAP_MESSAGES"] * factor)),
        "view": max(1, int(cfg["SOURCING_CAP_VIEWS"] * factor)),
    }
    return {
        "used": {"connect": connects, "message": messages, "view": views},
        "limits": limits,
        "left": {"connect": max(0, limits["connect"] - connects), "message": max(0, limits["message"] - messages),
                 "view": max(0, limits["view"] - views)},
        "warmup": warmup,
        "day_start_utc": start.isoformat() + "Z",
    }


def paused_until():
    v = Setting.get("sourcing_paused_until")
    if not v:
        return None
    try:
        until = datetime.fromisoformat(v)
    except ValueError:
        return None
    return until if until > utcnow() else None


def pause(hours=None, reason="paused"):
    hours = hours if hours is not None else current_app.config["SOURCING_PAUSE_HOURS"]
    until = utcnow() + timedelta(hours=hours)
    Setting.put("sourcing_paused_until", until.isoformat())
    Setting.put("sourcing_paused_reason", reason[:300])
    return until


def resume():
    Setting.put("sourcing_paused_until", "")
    Setting.put("sourcing_paused_reason", "")


def worker_status():
    cfg = current_app.config
    return {
        "paused_until": paused_until().isoformat() + "Z" if paused_until() else None,
        "paused_reason": Setting.get("sourcing_paused_reason") or None,
        "caps": caps(),
        "hours": cfg["SOURCING_HOURS"], "tz": cfg["SOURCING_TZ"],
        "min_delay": cfg["SOURCING_MIN_DELAY"], "max_delay": cfg["SOURCING_MAX_DELAY"],
        "pending_searches": SourcingSearch.query.filter_by(status="pending").count(),
        "due_actions": due_query().count(),
        "server_time": utcnow().isoformat() + "Z",
    }


# ------------------------------------------------------------------------------------ queue

def due_query():
    now = utcnow()
    return SourcedProfile.query.filter(
        SourcedProfile.decision == "approved", SourcedProfile.closed_at.is_(None),
        SourcedProfile.next_action.isnot(None), SourcedProfile.released.is_(True),
        db.or_(SourcedProfile.next_due_at.is_(None), SourcedProfile.next_due_at <= now),
        db.or_(SourcedProfile.lock_at.is_(None), SourcedProfile.lock_at < now - timedelta(hours=2)),
    )


def next_queue(limit=10, channel="linkedin"):
    """Actions the worker should execute now, within today's caps. Locks them for 2 hours."""
    if paused_until():
        return []
    expire_stale()
    c = caps()
    left = dict(c["left"])
    out = []
    rows = (due_query().filter(SourcedProfile.channel == channel)
            .order_by(SourcedProfile.next_due_at.asc().nullsfirst(), SourcedProfile.pre_score.desc().nullslast())
            .limit(limit * 3).all())
    for p in rows:
        a = p.next_action
        if a == "connect":
            if left["connect"] <= 0 or left["view"] <= 0:
                continue
            left["connect"] -= 1
            left["view"] -= 1
            text = p.draft_note
        elif a in ("message", "reminder"):
            if left["message"] <= 0 or left["view"] <= 0:
                continue
            left["message"] -= 1
            left["view"] -= 1
            text = p.draft_message if a == "message" else p.draft_reminder
        else:  # check_accept
            if left["view"] <= 0:
                continue
            left["view"] -= 1
            text = None
        p.lock_at = utcnow()
        out.append({"profile_id": p.id, "action": a, "profile_url": p.profile_url, "full_name": p.full_name,
                    "text": text, "role": p.role.title})
        if len(out) >= limit:
            break
    db.session.commit()
    return out


def record_outcome(profile, action, ok=True, detail=None, accepted=None, reply_text=None, warning=None,
                   actor="worker"):
    """State machine. Called by the worker after each action (and by admins for manual steps)."""
    cfg = current_app.config
    now = utcnow()
    profile.lock_at = None
    _log(profile, action, ok, detail, actor=actor)
    if warning:
        pause(reason=f"LinkedIn warning reported by the worker: {warning}"[:300])
    if not ok:
        # retry later; after 3 failures on the same step, close it for a human to look at
        fails = profile.actions.filter_by(action=action, ok=False).count()
        if fails >= 3:
            _close(profile, f"{action} failed 3x")
        else:
            profile.next_due_at = now + timedelta(hours=6)
        return
    if action == "connect":
        profile.connect_sent_at = now
        profile.next_action = "check_accept"
        profile.next_due_at = now + timedelta(days=1)
        profile.released = True   # checking is passive, never needs confirmation
    elif action == "check_accept":
        if accepted:
            profile.accepted_at = now
            profile.next_action = "message"
            profile.next_due_at = now
            profile.released = bool(profile.role.sourcing_autosend)
        elif (now - (profile.connect_sent_at or now)).days >= cfg["SOURCING_ACCEPT_WAIT_DAYS"]:
            _close(profile, "not accepted")
        else:
            profile.next_due_at = now + timedelta(days=2)
    elif action == "message":
        profile.message_sent_at = now
        profile.next_action = "reminder"
        profile.next_due_at = now + timedelta(days=cfg["SOURCING_REMINDER_DAYS"])
        profile.released = bool(profile.role.sourcing_autosend)
    elif action == "reminder":
        profile.reminder_sent_at = now
        _close(profile, "sequence complete")
    if reply_text:
        ingest_reply(profile, reply_text)


def ingest_reply(profile, text, at=None):
    text = (text or "").strip()
    if not text or text == (profile.reply_text or "").strip():
        return None
    profile.reply_text = text[:5000]
    profile.reply_at = at or utcnow()
    cls = classify_reply(profile)
    _log(profile, "reply", True, f"{cls}: {text[:200]}", actor="candidate")
    if cls in ("negative", "not_now"):
        _close(profile, f"replied: {cls}")
    elif cls in ("question", "interested", "other") and profile.next_action == "reminder":
        # a human should answer; don't fire the automatic reminder on top of a conversation
        profile.released = False
    return cls


def ingest_inbox(messages):
    """messages: [{profile_url, text, at?}] scraped from the LinkedIn inbox (only threads we started)."""
    matched = 0
    for m in messages:
        url = _clean_url(m.get("profile_url"))
        if not url:
            continue
        p = (SourcedProfile.query.filter_by(profile_url=url)
             .filter(SourcedProfile.decision == "approved").order_by(SourcedProfile.created_at.desc()).first())
        if not p:
            continue
        at = None
        if m.get("at"):
            try:
                at = datetime.fromisoformat(m["at"].replace("Z", ""))
            except ValueError:
                at = None
        if ingest_reply(p, m.get("text"), at):
            matched += 1
    db.session.commit()
    return matched


def expire_stale():
    """Close profiles waiting for acceptance for too long (in case check_accept never ran)."""
    cfg = current_app.config
    cutoff = utcnow() - timedelta(days=cfg["SOURCING_ACCEPT_WAIT_DAYS"] + 2)
    rows = SourcedProfile.query.filter(SourcedProfile.next_action == "check_accept",
                                       SourcedProfile.connect_sent_at < cutoff,
                                       SourcedProfile.closed_at.is_(None)).all()
    for p in rows:
        _close(p, "not accepted")
        _log(p, "expired", True, "no acceptance", actor="system")
    if rows:
        db.session.commit()


# ------------------------------------------------------------------------------------ link & apply

def on_link_opened(profile):
    if profile.link_opened_at is None:
        profile.link_opened_at = utcnow()
        _log(profile, "link_opened", True, actor="candidate")
    if profile.next_action == "reminder":      # they saw it; no nudge needed
        _close(profile, "link opened")
    db.session.commit()


def on_applied(profile, applicant):
    profile.applicant_id = applicant.id
    if profile.link_opened_at is None:
        profile.link_opened_at = utcnow()
    _close(profile, "applied")
    _log(profile, "applied", True, f"applicant {applicant.public_id}", actor="candidate")
    if profile.referred_by and not applicant.referred_by:
        applicant.referred_by = profile.referred_by
    log_event(applicant, "sourced", f"Came through sourcing ({profile.channel}) — profile {profile.profile_url}",
              actor="system")


# ------------------------------------------------------------------------------------ referrals / manual

def add_manual(role, full_name, profile_url=None, email=None, channel="manual", referred_by=None,
               headline=None, location=None, actor="admin", send_email=True):
    """Referral or hand-added candidate: approved immediately, link emailed if we have an address."""
    url = _clean_url(profile_url)
    if not url and email:
        url = f"mailto:{email.strip().lower()}"
    if not url:
        raise ValueError("A LinkedIn URL or an email is required.")
    p = SourcedProfile.query.filter_by(role_id=role.id, profile_url=url).first()
    if p is None:
        p = SourcedProfile(role=role, channel=channel, profile_url=url, full_name=full_name.strip())
        db.session.add(p)
    p.email = (email or p.email or "").strip().lower() or None
    p.referred_by = referred_by or p.referred_by
    p.headline = headline or p.headline
    p.location = location or p.location
    db.session.flush()
    p.decision = "approved"
    p.decided_at = utcnow()
    prepare_drafts(p)
    sent = None
    if p.email and send_email:
        body = render(role.msg_referral or DEFAULT_REFERRAL_EMAIL, p)
        subject = f"{current_app.config['COMPANY_NAME']} — {role.title} position"
        ok, err = mailer.send(p.email, subject, body)
        sent = ok
        _log(p, "email", ok, err or f"link emailed to {p.email}", actor=actor)
        if ok:
            p.message_sent_at = utcnow()
            p.next_action = "reminder"
            p.next_due_at = utcnow() + timedelta(days=current_app.config["SOURCING_REMINDER_DAYS"])
            p.released = False   # email reminders are sent by a human from the admin (no worker)
    elif channel == "linkedin" and p.profile_url.startswith("https://"):
        p.next_action = "connect"
        p.next_due_at = utcnow()
        p.released = bool(role.sourcing_autosend)
    db.session.commit()
    return p, sent


# ------------------------------------------------------------------------------------ funnel

FUNNEL_STAGES = ["found", "approved", "contacted", "accepted", "link_sent", "link_opened", "applied", "selected"]
SELECTED_STATUSES = ("selected", "test_sent", "test_returned", "interview_done", "interview2_done",
                     "contract_sent", "hired")


def funnel(role=None):
    """Counts per channel for the Sourcing tab (and the API)."""
    q = SourcedProfile.query
    if role is not None:
        q = q.filter_by(role_id=role.id)
    out = {}
    for p in q.all():
        f = out.setdefault(p.channel, {s: 0 for s in FUNNEL_STAGES})
        f["found"] += 1
        if p.decision == "approved":
            f["approved"] += 1
        if p.connect_sent_at or (p.message_sent_at and p.channel != "linkedin"):
            f["contacted"] += 1
        if p.accepted_at:
            f["accepted"] += 1
        if p.message_sent_at:
            f["link_sent"] += 1
        if p.link_opened_at:
            f["link_opened"] += 1
        if p.applicant_id:
            f["applied"] += 1
            if p.applicant and p.applicant.status in SELECTED_STATUSES:
                f["selected"] += 1
    return out
