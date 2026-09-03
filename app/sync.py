"""Keeps the app and ClickUp in the same state, without manual setup.

1. Webhook auto-registration (startup, background): if a ClickUp token and a public base URL are
   configured, the app registers its own `taskStatusUpdated` webhook and stores the signing secret
   in the settings table. No CLI command, no env var needed (CLICKUP_WEBHOOK_SECRET still wins if set).

2. Polling reconciler (every CLICKUP_POLL_MINUTES): even if the webhook can't reach the app
   (sleeping instance, registration refused), statuses converge. For each active applicant with a
   task, the task's current status is fetched and compared:
     - app changed recently (last status_changed by admin/agent/claude/system) -> push app -> ClickUp
     - otherwise -> adopt the board's status (ClickUp is the recruiters' working surface)
"""
import logging
import os
import threading
import time
from datetime import timedelta

import requests

from . import clickup
from .models import Applicant, Event, Setting, db, log_event, utcnow

log = logging.getLogger(__name__)
API = os.environ.get("CLICKUP_API_BASE", "https://api.clickup.com/api/v2")

WEBHOOK_SECRET_KEY = "clickup_webhook_secret"
TERMINAL = ("hired", "rejected")


def webhook_secret(app):
    return app.config["CLICKUP_WEBHOOK_SECRET"] or Setting.get(WEBHOOK_SECRET_KEY)


def _headers(app):
    return {"Authorization": app.config["CLICKUP_API_TOKEN"], "Content-Type": "application/json"}


def ensure_webhook(app):
    """Idempotently register the ClickUp -> app webhook and store its secret. Returns a status string."""
    cfg = app.config
    base = cfg["PUBLIC_BASE_URL"]
    if not (cfg["CLICKUP_ENABLED"] and cfg["CLICKUP_API_TOKEN"]):
        return "clickup disabled"
    if "localhost" in base or "127.0.0.1" in base:
        return "skipped (PUBLIC_BASE_URL is local)"
    endpoint = f"{base}/webhooks/clickup"
    try:
        r = requests.get(f"{API}/team/{cfg['CLICKUP_TEAM_ID']}/webhook", headers=_headers(app), timeout=15)
        r.raise_for_status()
        for wh in r.json().get("webhooks", []):
            if wh.get("endpoint") == endpoint:
                secret = wh.get("secret")
                if secret and Setting.get(WEBHOOK_SECRET_KEY) != secret:
                    Setting.put(WEBHOOK_SECRET_KEY, secret)
                return f"already registered ({wh.get('id')})"
        r = requests.post(f"{API}/team/{cfg['CLICKUP_TEAM_ID']}/webhook",
                          json={"endpoint": endpoint, "events": ["taskStatusUpdated"]},
                          headers=_headers(app), timeout=15)
        r.raise_for_status()
        data = r.json()
        wh = data.get("webhook", data)
        secret = wh.get("secret", "")
        if secret:
            Setting.put(WEBHOOK_SECRET_KEY, secret)
        log.info("ClickUp webhook registered: %s -> %s", data.get("id") or wh.get("id"), endpoint)
        return "registered"
    except Exception as exc:
        log.warning("ClickUp webhook auto-registration failed: %s", exc)
        return f"failed: {exc}"


def get_task_status(app, task_id):
    """Current status name of a ClickUp task, or None on any error."""
    try:
        r = requests.get(f"{API}/task/{task_id}", headers=_headers(app), timeout=15)
        r.raise_for_status()
        status = r.json().get("status")
        return status.get("status") if isinstance(status, dict) else status
    except Exception as exc:
        log.warning("ClickUp: could not read task %s: %s", task_id, exc)
        return None


def _app_changed_recently(a, minutes=30):
    """True when the newest status_changed event was made in the app (not from ClickUp) recently."""
    ev = (Event.query.filter(Event.applicant_id == a.id, Event.kind == "status_changed")
          .order_by(Event.created_at.desc()).first())
    if not ev or ev.actor == "clickup":
        return False
    return ev.created_at >= utcnow() - timedelta(minutes=minutes)


def reconcile_once(app, limit=40):
    """One reconciliation pass. Returns a summary dict."""
    out = {"checked": 0, "pushed": 0, "adopted": 0}
    with app.app_context():
        rows = (Applicant.query.filter(Applicant.deleted_at.is_(None),
                                       Applicant.clickup_task_id.isnot(None),
                                       Applicant.status.notin_(TERMINAL))
                .order_by(Applicant.updated_at.desc()).limit(limit).all())
        for a in rows:
            name = get_task_status(app, a.clickup_task_id)
            if not name:
                continue
            out["checked"] += 1
            slug = clickup.status_from_clickup(name)
            if not slug or slug == a.status:
                continue
            if _app_changed_recently(a):
                clickup.sync_status(a)   # our change didn't land on the board yet -> push again
                out["pushed"] += 1
            else:
                old = a.status
                a.status = slug
                log_event(a, "status_changed", f"{old} -> {slug} (reconciled from ClickUp)", actor="clickup")
                out["adopted"] += 1
        db.session.commit()
        db.session.remove()
    return out


def start_background(app):
    """Start the webhook registration (once) and the polling reconciler (looping) in daemon threads."""
    def _boot():
        time.sleep(3)  # let the web server come up first
        with app.app_context():
            if app.config["CLICKUP_WEBHOOK_AUTOREGISTER"]:
                log.info("ClickUp webhook: %s", ensure_webhook(app))
        minutes = app.config["CLICKUP_POLL_MINUTES"]
        if minutes <= 0 or not app.config["CLICKUP_ENABLED"]:
            return
        while True:
            time.sleep(minutes * 60)
            try:
                summary = reconcile_once(app)
                if summary["pushed"] or summary["adopted"]:
                    log.info("ClickUp reconcile: %s", summary)
            except Exception as exc:
                log.warning("ClickUp reconcile failed: %s", exc)

    threading.Thread(target=_boot, daemon=True).start()
