"""Candidate email sending.

MAIL_BACKEND=smtp  — Gmail (or any SMTP): MAIL_USERNAME + MAIL_PASSWORD (Gmail App Password), MAIL_FROM.
MAIL_BACKEND=log   — no real sending; the message is logged (development / not yet configured).

send() returns True only when the message actually left; callers use that to decide whether a
candidate can be considered notified.
"""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


def configured():
    cfg = current_app.config
    return cfg["MAIL_BACKEND"] == "smtp" and cfg["MAIL_USERNAME"] and cfg["MAIL_PASSWORD"]


def send(to, subject, body):
    cfg = current_app.config
    if cfg["MAIL_BACKEND"] == "log":  # development: treat as sent, keep a record in the logs
        log.info("MAIL (log backend) to=%s subject=%r\n%s", to, subject, body)
        current_app.extensions.setdefault("_sent_mail", []).append({"to": to, "subject": subject, "body": body})
        return True
    if not configured():
        log.warning("Mail not configured (set MAIL_USERNAME/MAIL_PASSWORD); could not email %s", to)
        return False
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"] or cfg["MAIL_USERNAME"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20) as s:
            s.starttls()
            s.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception as exc:
        log.warning("Mail send failed to %s: %s", to, exc)
        return False


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
