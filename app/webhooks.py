"""ClickUp -> app sync. A webhook registered with `flask clickup-webhook-setup` calls
POST /webhooks/clickup whenever a task's status changes on the board; the matching applicant's
status is updated here without pushing back (the app's own pushes come back through this endpoint
too and are ignored because the status already matches — no loops).

Signature: ClickUp signs each delivery with HMAC-SHA256 of the raw body using the webhook secret
(X-Signature header). Set CLICKUP_WEBHOOK_SECRET to enforce verification.
"""
import hashlib
import hmac
import logging

from flask import Blueprint, current_app, jsonify, request

from . import clickup
from .models import Applicant, db, log_event
from .sync import webhook_secret

log = logging.getLogger(__name__)
bp = Blueprint("webhooks", __name__)


def _verify(raw):
    secret = webhook_secret(current_app._get_current_object())
    if not secret:
        return True  # verification not configured yet
    given = request.headers.get("X-Signature", "")
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, want)


def _status_name(payload):
    """Pull the new status name out of a taskStatusUpdated payload (shape varies slightly)."""
    for item in payload.get("history_items") or []:
        after = item.get("after")
        if isinstance(after, dict) and after.get("status"):
            return after["status"]
        if isinstance(after, str) and item.get("field") == "status":
            return after
    status = payload.get("task", {}).get("status")
    if isinstance(status, dict):
        return status.get("status")
    return None


@bp.post("/clickup")
def clickup_webhook():
    raw = request.get_data()
    if not _verify(raw):
        return jsonify(error="bad signature"), 401
    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "")
    task_id = payload.get("task_id") or payload.get("task", {}).get("id")
    if event != "taskStatusUpdated" or not task_id:
        return jsonify(ok=True, ignored=event or "no event")

    a = Applicant.query.filter_by(clickup_task_id=str(task_id)).filter(Applicant.deleted_at.is_(None)).first()
    if not a:
        return jsonify(ok=True, ignored="unknown task")

    name = _status_name(payload)
    slug = clickup.status_from_clickup(name)
    if not slug:
        log.info("ClickUp webhook: unmapped status %r on task %s", name, task_id)
        return jsonify(ok=True, ignored=f"unmapped status {name!r}")
    if slug == a.status:
        return jsonify(ok=True, unchanged=slug)  # our own push echoing back — no loop

    old = a.status
    a.status = slug
    log_event(a, "status_changed", f"{old} -> {slug} (changed on ClickUp)", actor="clickup")
    db.session.commit()
    log.info("ClickUp webhook: %s %s -> %s", a.public_id, old, slug)
    return jsonify(ok=True, applicant=a.public_id, status=slug)
