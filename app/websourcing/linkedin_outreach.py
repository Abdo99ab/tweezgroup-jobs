"""LinkedIn reach-out for discovered candidates (Web Sourcing → role page).

For each candidate the recruiter gets, ready for a last check before sending by hand on LinkedIn:
  - a verified profile link (see app.linkedin_links) — or a people-search link when we have none
  - a connection-request note (<= 300 characters, LinkedIn's hard limit, no link in it)
  - an InMail subject and a DM / InMail body carrying the candidate's personal apply link

Nothing is ever sent from here. The recruiter copies the text, sends it on LinkedIn, then clicks
"Mark sent" so the outreach pipeline knows the person was contacted (and never invites them twice).

The personal link is /r/<WebCandidate.apply_token>. When the candidate opens it, public.sourced_link
moves them into the outreach pipeline (a SourcedProfile), so opens and applications are tracked and
the resulting applicant is tagged as sourced on ClickUp.
"""
import logging
import re
import secrets
import threading

from flask import current_app

from .. import linkedin_links
from ..models import WebCandidate, db, utcnow
from .llm import claude_json

log = logging.getLogger(__name__)

NOTE_LIMIT = 300            # LinkedIn connection-note limit (characters)
SUBJECT_LIMIT = 120
PLACEHOLDER = "{apply_url}"
KINDS = {"connect": "connection request", "dm": "direct message", "inmail": "InMail"}

FALLBACK_NOTE = ("Hi {first_name}, I'm recruiting a {role} at {company}. {hook}"
                 "I'd be glad to connect and share the details.")
FALLBACK_SUBJECT = "{role} role at {company}"
FALLBACK_MESSAGE = """Hi {first_name},

I'm recruiting a {role} at {company} (we run e-commerce brands across Europe and the US). {hook}

If it sounds interesting, you can apply in two minutes with this personal link — just upload your CV:
{apply_url}

Happy to answer any question here.
{sender}"""

PROMPT = """You write LinkedIn recruiting messages for {company}. The recruiter will read and edit them
before sending them by hand, so they must be accurate, short and natural.

ROLE: {role}
ABOUT THE ROLE (excerpt):
{role_text}

CANDIDATE (public information only — do NOT invent anything beyond this):
First name: {first_name}
Headline: {headline}
Current company: {cand_company}
Location: {location}
Why they look relevant (from our screening, evidence-labelled): {matched}

SENDER SIGNATURE: {sender}

Write:
1. "connection_note": a LinkedIn connection-request note, MAX 280 characters, no link, no emoji,
   no flattery. Greet by first name, say we're hiring a {role} at {company}, reference ONE concrete
   thing from the headline/company, invite them to connect.
2. "inmail_subject": max 60 characters, specific, no clickbait.
3. "message": a DM / InMail body, 450-800 characters. Greet by first name; 1 sentence on the role and
   {company}; 1 sentence on why their background fits (only facts above); then exactly the literal
   placeholder {{apply_url}} on its own line, presented as their personal 2-minute application link
   (upload CV); close politely and sign with the sender signature if there is one.

Language: write in French if the headline is written in French; otherwise English.
Return ONLY a JSON object with keys connection_note, inmail_subject, message."""


class _Safe(dict):
    def __missing__(self, key):
        return ""


def first_name(c):
    return (linkedin_links.clean_name(c.full_name).split(" ") or [""])[0].strip().title() or "there"


def ensure_token(c):
    if not c.apply_token:
        c.apply_token = secrets.token_urlsafe(12)[:16]
    return c.apply_token


def apply_link(c):
    """Personal link put in the messages: the outreach profile's link once moved, else the candidate's own."""
    base = current_app.config["PUBLIC_BASE_URL"]
    if c.sourced_profile_id:
        from ..models import SourcedProfile
        p = db.session.get(SourcedProfile, c.sourced_profile_id)
        if p is not None:
            return f"{base}/r/{p.apply_token}"
    return f"{base}/r/{ensure_token(c)}"


def rendered_message(c):
    return (c.li_message or "").replace(PLACEHOLDER, apply_link(c))


def _hook(c):
    bits = [b for b in (c.headline, c.company) if b]
    if not bits:
        return ""
    h = (c.headline or "").split("|")[0].split("·")[0].strip()
    return f"Your background as {h[:80]} caught my attention. " if h else ""


def _fallback(c, cfg):
    values = _Safe(first_name=first_name(c), role=c.role.title, company=cfg["COMPANY_NAME"], hook=_hook(c),
                   sender=cfg.get("SOURCING_SENDER_NAME") or "", apply_url=PLACEHOLDER)
    note = FALLBACK_NOTE.format_map(values)
    if len(note) > NOTE_LIMIT:
        values["hook"] = ""
        note = FALLBACK_NOTE.format_map(values)
    return {"connection_note": note, "inmail_subject": FALLBACK_SUBJECT.format_map(values),
            "message": FALLBACK_MESSAGE.format_map(values)}


def _tidy(text):
    text = re.sub(r"[ \t]{2,}", " ", (text or "").replace("\r", ""))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fit_note(note):
    note = _tidy(note).replace("\n", " ")
    if len(note) <= NOTE_LIMIT:
        return note
    cut = note[:NOTE_LIMIT]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 120 else cut.rsplit(" ", 1)[0]).strip()


def generate(c, force=False):
    """Write (or rewrite) the three texts for one candidate. Returns True when texts changed."""
    if c.li_message and not force:
        return False
    cfg = current_app.config
    role = c.role
    role_text = "\n".join(x for x in ((role.description or "")[:700], (role.requirements or "")[:700]) if x)
    data = claude_json(PROMPT.format(
        company=cfg["COMPANY_NAME"], role=role.title, role_text=role_text or "-",
        first_name=first_name(c), headline=c.headline or "-", cand_company=c.company or "-",
        location=c.location or "-", matched="; ".join(c.matched[:4]) or "-",
        sender=cfg.get("SOURCING_SENDER_NAME") or "(none)"), max_tokens=900) or {}
    if not (isinstance(data, dict) and data.get("connection_note") and data.get("message")):
        data = _fallback(c, cfg)
    msg = _tidy(str(data.get("message")))
    if PLACEHOLDER not in msg:
        msg = msg + "\n\nYour personal application link (2 minutes, just upload your CV):\n" + PLACEHOLDER
    c.li_note = _fit_note(str(data.get("connection_note")))
    c.li_subject = _tidy(str(data.get("inmail_subject") or ""))[:SUBJECT_LIMIT] or None
    c.li_message = msg
    c.li_generated_at = utcnow()
    ensure_token(c)
    if not c.linkedin_url:
        found = c.linkedin_candidates
        c.linkedin_url = found[0] if found else None
    db.session.commit()
    return True


def _generate_ids(app, ids, force):
    with app.app_context():
        for cid in ids:
            c = db.session.get(WebCandidate, cid)
            if c is None:
                continue
            try:
                generate(c, force=force)
            except Exception:
                log.exception("LinkedIn message generation failed for candidate %s", cid)
                db.session.rollback()
        db.session.remove()


def generate_async(ids, force=False):
    """Generate for several candidates without blocking the request (inline under tests)."""
    ids = list(ids)
    if not ids:
        return 0
    app = current_app._get_current_object()
    if current_app.config.get("PROCESS_ASYNC", True) and not current_app.config.get("TESTING"):
        threading.Thread(target=_generate_ids, args=(app, ids, force), daemon=True,
                         name="websourcing-li-messages").start()
    else:
        _generate_ids(app, ids, force)
    return len(ids)


def save(c, note=None, subject=None, message=None, linkedin_url=None):
    """Recruiter's edits. The personal link shown in the box is stored back as the placeholder.
    Returns an error string for a LinkedIn URL that is not a profile, else None."""
    err = None
    if linkedin_url is not None:
        url = linkedin_url.strip()
        if not url:
            c.linkedin_url = None
        else:
            canon = linkedin_links.normalize(url)
            if canon:
                c.linkedin_url = canon
            else:
                err = "That isn't a LinkedIn profile link — it must look like https://www.linkedin.com/in/<name>."
    if note is not None:
        c.li_note = _fit_note(note) or None
    if subject is not None:
        c.li_subject = _tidy(subject)[:SUBJECT_LIMIT] or None
    if message is not None:
        msg = _tidy(message).replace(apply_link(c), PLACEHOLDER)
        c.li_message = msg or None
    db.session.commit()
    return err


def mark_sent(c, kind):
    kind = kind if kind in KINDS else "dm"
    c.li_sent_at = utcnow()
    c.li_sent_kind = kind
    db.session.commit()
    if c.sourced_profile_id:      # already in the outreach pipeline: record it there too
        from ..models import SourcedProfile
        p = db.session.get(SourcedProfile, c.sourced_profile_id)
        if p is not None:
            apply_sent_state(p, c)
            db.session.commit()


def unmark_sent(c):
    c.li_sent_at = None
    c.li_sent_kind = None
    db.session.commit()


def apply_sent_state(profile, c):
    """A message the recruiter sent by hand on LinkedIn is recorded on the outreach profile so the
    worker never sends the same person a second invitation/message."""
    from datetime import timedelta
    if not c.li_sent_at:
        return
    if c.li_sent_kind == "connect":
        if not profile.connect_sent_at:
            profile.connect_sent_at = c.li_sent_at
        if profile.next_action in (None, "connect"):
            profile.next_action = "check_accept"     # passive check; the link message follows on accept
            profile.next_due_at = utcnow() + timedelta(days=1)
            profile.released = True
    else:
        if not profile.message_sent_at:
            profile.message_sent_at = c.li_sent_at
        if profile.next_action in (None, "connect", "check_accept", "message"):
            profile.next_action = "reminder"
            profile.next_due_at = c.li_sent_at + timedelta(days=current_app.config["SOURCING_REMINDER_DAYS"])
            profile.released = False                 # reminder still waits for the recruiter's Send


def fix_links(role):
    """Re-check every discovered candidate of a role: canonical LinkedIn link stored, name cleaned.
    Returns counts per verdict {ok, check, invalid, missing, renamed}."""
    out = {"ok": 0, "check": 0, "invalid": 0, "missing": 0, "renamed": 0}
    for c in role.web_candidates.filter(WebCandidate.status != "dismissed").all():
        clean = linkedin_links.clean_name(c.full_name)
        if clean != c.full_name:
            c.full_name = clean
            out["renamed"] += 1
        found = c.linkedin_candidates
        if found and c.linkedin_url != found[0]:
            c.linkedin_url = found[0]
        out[c.li_check["state"]] += 1
    db.session.commit()
    return out
