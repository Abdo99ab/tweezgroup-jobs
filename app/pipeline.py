"""Post-application automation, in order:

  1. Claude reads the CV and writes a score + summary (if ANTHROPIC_API_KEY is set)
  2. ClickUp task is created in the role's list, assigned to Mehdi Mahcene
  3. The summary is posted as a comment on the task, tagging Ahmidou, Taoufik Mousselmal, Abderrahmane Hammia

Runs in a background thread right after the candidate submits (PROCESS_ASYNC=true), or inline.
Idempotent: re-running only does the steps that are still missing. `flask process-pending` retries
anything that failed (e.g. ClickUp was down).
"""
import logging
import threading

from flask import current_app

from . import clickup, summarize
from .models import Applicant, Event, db, log_event

log = logging.getLogger(__name__)


def process_application(applicant_id, background=True):
    app = current_app._get_current_object()
    if background:
        threading.Thread(target=_run, args=(app, applicant_id), daemon=True).start()
        return ["queued"]
    return _run(app, applicant_id)


def _run(app, applicant_id):
    with app.app_context():
        a = db.session.get(Applicant, applicant_id)
        if not a or a.deleted_at:
            return []
        done = []
        try:
            # 1. summary
            if a.ai_summary is None and app.config["SUMMARY_ENABLED"]:
                try:
                    data = summarize.summarize(a)
                    if data:
                        a.score = data["score"]
                        a.ai_summary = summarize.format_for_task(a, data)
                        log_event(a, "scored", f"Auto-summary: score {a.score}, {data.get('recommendation')}", actor="claude")
                        done.append("summary")
                except Exception as exc:
                    log.warning("Auto-summary failed for %s: %s", a.public_id, exc)
                    log_event(a, "error", f"Auto-summary failed: {exc}", actor="claude")
                db.session.commit()

            # 2. task
            if not a.clickup_task_id:
                if clickup.create_task(a):
                    done.append("clickup_task")
                db.session.commit()

            # 3. comment with summary + mentions (once)
            if a.clickup_task_id and a.ai_summary and not _has_event(a, "clickup_synced", "Summary comment"):
                if clickup.post_comment(a, a.ai_summary, mentions=True):
                    done.append("clickup_comment")
                db.session.commit()
        except Exception as exc:
            log.exception("process_application failed: %s", exc)
            db.session.rollback()
        finally:
            db.session.remove()
        return done


def _has_event(a, kind, message_prefix):
    return Event.query.filter(Event.applicant_id == a.id, Event.kind == kind,
                              Event.message.like(f"{message_prefix}%")).count() > 0


def pending_query():
    """Applicants still missing a summary, a task, or the summary comment."""
    return Applicant.query.filter(Applicant.deleted_at.is_(None)).filter(
        db.or_(Applicant.clickup_task_id.is_(None), Applicant.ai_summary.is_(None))
    ).order_by(Applicant.created_at.asc())
