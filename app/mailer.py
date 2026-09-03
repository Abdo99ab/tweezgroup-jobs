"""Candidate email sending.

MAIL_BACKEND=smtp       — Gmail (or any SMTP): MAIL_USERNAME + MAIL_PASSWORD (Gmail App Password).
                          Render cannot reach smtp.gmail.com (Errno 101); prefer gmail_api there.
MAIL_BACKEND=gmail_api  — Gmail API using the same GOOGLE_OAUTH_* credentials as Drive
                          (re-run scripts/gdrive_auth.py so the token includes gmail.send).
MAIL_BACKEND=log        — no real sending; the message is logged (development).

send() returns (ok, error_message). error_message is None on success.
"""
import logging
import smtplib
import threading
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)
_gmail_lock = threading.Lock()


def configured():
    cfg = current_app.config
    if cfg["MAIL_BACKEND"] == "smtp":
        return bool(cfg["MAIL_USERNAME"] and cfg["MAIL_PASSWORD"])
    if cfg["MAIL_BACKEND"] == "gmail_api":
        return bool(cfg["GOOGLE_OAUTH_REFRESH_TOKEN"] and cfg["GOOGLE_OAUTH_CLIENT_ID"])
    return cfg["MAIL_BACKEND"] == "log"


def status():
    """One line for the admin dashboard: which backend, and is it usable."""
    cfg = current_app.config
    b = cfg["MAIL_BACKEND"]
    if b == "off":
        return "off", ("Email is OFF — set MAIL_USERNAME+MAIL_PASSWORD (SMTP) or "
                       "re-run gdrive_auth.py for Gmail API. Tests cannot be sent.")
    if b == "smtp":
        return (("ok", f"SMTP via {cfg['MAIL_SERVER']} as {cfg['MAIL_USERNAME']}")
                if configured() else ("error", "SMTP selected but MAIL_USERNAME/MAIL_PASSWORD missing"))
    if b == "gmail_api":
        return (("ok", "Gmail API using the Google (Drive) authorisation")
                if configured() else ("error", "gmail_api selected but GOOGLE_OAUTH_* credentials missing"))
    return "ok", "log backend (development — mails are only logged)"


def _gmail_api_ready():
    cfg = current_app.config
    return bool(cfg.get("GOOGLE_OAUTH_REFRESH_TOKEN") and cfg.get("GOOGLE_OAUTH_CLIENT_ID"))


def _is_network_error(exc):
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (101, 111, 113, 110, 51, 61, 64, 65):
        return True
    text = str(exc).lower()
    return any(s in text for s in (
        "network is unreachable", "connection refused", "timed out",
        "name or service not known", "temporary failure in name resolution",
        "network unreachable",
    ))


def _smtp_send(msg):
    cfg = current_app.config
    msg["From"] = cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"]
    with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20) as s:
        s.starttls()
        s.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        s.send_message(msg)


def _gmail_error(exc):
    """Prefer the Gmail API error body over the generic HttpError wrapper."""
    content = getattr(exc, "content", None)
    if content:
        try:
            import json
            data = json.loads(content.decode() if isinstance(content, bytes) else content)
            return data.get("error", {}).get("message") or str(exc)
        except Exception:
            pass
    return str(exc)


def _gmail_send(msg):
    """Send through the Gmail API with the same OAuth credentials used for Drive.

    gmail.send cannot call users.getProfile, so From is omitted and Gmail fills in
    the authorised account. MAIL_FROM is Reply-To only.
    """
    import base64
    from email import policy

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from .storage import _prefer_google_auth
    _prefer_google_auth()

    cfg = current_app.config
    if "From" in msg:
        del msg["From"]
    reply = (cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"] or "").strip()
    if reply:
        if "Reply-To" in msg:
            msg.replace_header("Reply-To", reply)
        else:
            msg["Reply-To"] = reply

    creds = Credentials(None, refresh_token=cfg["GOOGLE_OAUTH_REFRESH_TOKEN"],
                        client_id=cfg["GOOGLE_OAUTH_CLIENT_ID"], client_secret=cfg["GOOGLE_OAUTH_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token")
    raw = base64.urlsafe_b64encode(msg.as_bytes(policy=policy.SMTP)).decode()
    with _gmail_lock:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send(to, subject, body):
    """Send an email. Returns (True, None) or (False, error_string)."""
    cfg = current_app.config
    if cfg["MAIL_BACKEND"] == "log":
        log.info("MAIL (log backend) to=%s subject=%r\n%s", to, subject, body)
        current_app.extensions.setdefault("_sent_mail", []).append({"to": to, "subject": subject, "body": body})
        return True, None
    if not configured():
        err = f"Mail not configured (backend={cfg['MAIL_BACKEND']})"
        log.warning("%s; could not email %s", err, to)
        return False, err
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if cfg["MAIL_BACKEND"] == "gmail_api":
            _gmail_send(msg)
            used = "gmail_api"
        else:
            try:
                _smtp_send(msg)
                used = "smtp"
            except Exception as smtp_exc:
                if _gmail_api_ready() and _is_network_error(smtp_exc):
                    log.warning("SMTP unreachable (%s); falling back to Gmail API", smtp_exc)
                    _gmail_send(msg)
                    used = "gmail_api"
                else:
                    raise
        log.info("Mail sent to %s (%s): %r", to, used, subject)
        return True, None
    except Exception as extra:
        err = _gmail_error(extra)
        if _is_network_error(extra) and cfg["MAIL_BACKEND"] == "smtp":
            err = (f"{err}. This host cannot reach {cfg['MAIL_SERVER']} "
                   "(Render blocks Gmail SMTP). Set MAIL_BACKEND=gmail_api.")
        log.warning("Mail send failed to %s via %s: %s", to, cfg["MAIL_BACKEND"], extra)
        return False, err


def test_invitation(applicant, test_url):
    company = current_app.config["COMPANY_NAME"]
    first = applicant.full_name.split(" ")[0]
    subject = f"{company} — technical test for the {applicant.role.title} position"
    body = f"""Hello {first},

Thank you for applying for the {applicant.role.title} position at {company}. We were impressed by your profile, and the next step is a short written technical test.

Complete it online here (one submission only, at your convenience):

{test_url}

There is no strict time limit; take the time to give thoughtful answers, in English or French as you prefer.

Good luck!
The {company} recruitment team
"""
    return subject, body
