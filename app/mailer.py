"""Candidate email sending.

MAIL_BACKEND=smtp       — Gmail (or any SMTP): MAIL_USERNAME + MAIL_PASSWORD (Gmail App Password).
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


def _gmail_send(msg):
    """Send through the Gmail API with the same OAuth credentials used for Drive.

    A new client is built per send (httplib2 is not thread-safe). From must be the
    authorised Gmail account; MAIL_FROM is used as Reply-To when it differs.
    """
    import base64

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from .storage import _prefer_google_auth
    _prefer_google_auth()

    cfg = current_app.config
    creds = Credentials(None, refresh_token=cfg["GOOGLE_OAUTH_REFRESH_TOKEN"],
                        client_id=cfg["GOOGLE_OAUTH_CLIENT_ID"], client_secret=cfg["GOOGLE_OAUTH_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token")
    with _gmail_lock:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        me = service.users().getProfile(userId="me").execute().get("emailAddress") or "me"
        wanted = (cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"] or "").strip()
        if "From" in msg:
            msg.replace_header("From", me)
        else:
            msg["From"] = me
        if wanted and wanted.lower() != me.lower():
            if "Reply-To" in msg:
                msg.replace_header("Reply-To", wanted)
            else:
                msg["Reply-To"] = wanted
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
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
    msg["From"] = cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"] or "me"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if cfg["MAIL_BACKEND"] == "gmail_api":
            _gmail_send(msg)
        else:
            with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20) as s:
                s.starttls()
                s.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
                s.send_message(msg)
        log.info("Mail sent to %s (%s): %r", to, cfg["MAIL_BACKEND"], subject)
        return True, None
    except Exception as exc:
        err = str(exc)
        log.warning("Mail send failed to %s via %s: %s", to, cfg["MAIL_BACKEND"], exc)
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
