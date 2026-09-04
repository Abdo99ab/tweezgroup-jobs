"""Post-application automation, in order:

  1. Claude reads the CV and writes a score + summary (if ANTHROPIC_API_KEY is set)
  2. The score decides the status automatically (AUTO_STATUS_ENABLED):
       score <  REJECT_BELOW (50)          -> rejected
       REJECT_BELOW <= score <= SELECT_ABOVE (60) -> filtered   (FILTRED APPLICATION)
       score >  SELECT_ABOVE               -> selected  (SELECTED/ IN PROGRESS)
         ... and if the role has a written test: the test is emailed to the candidate
             (unique online link) and the status becomes test_sent (TEST SENT) instead.
  3. ClickUp task is created in the role's list, assigned to Mehdi Mahcene, with that status
  4. The summary is posted as a comment tagging Ahmidou, Taoufik Mousselmal, Abderrahmane Hammia

When the candidate submits the test online, Claude grades it against the role's answer key,
the status moves to test_returned (TEST RETURNED) and the results are posted on the ClickUp task.

Runs in a background thread right after the candidate submits (PROCESS_ASYNC=true), or inline.
Idempotent: re-running only does the steps that are still missing. `flask process-pending` retries
anything that failed (e.g. ClickUp was down).
"""
import logging
import secrets
import threading

from flask import current_app

from . import clickup, mailer, summarize
from .models import Applicant, Event, db, log_event, utcnow

log = logging.getLogger(__name__)


def process_application(applicant_id, background=True):
    app = current_app._get_current_object()
    if background:
        threading.Thread(target=_run, args=(app, applicant_id), daemon=True).start()
        return ["queued"]
    return _run(app, applicant_id)


def decide_status(applicant, cfg):
    """The agreed score policy. Returns the target status slug, or None to leave as is."""
    if not cfg["AUTO_STATUS_ENABLED"] or applicant.score is None:
        return None
    if applicant.status not in ("new",):  # never override a decision already taken (human or webhook)
        return None
    if applicant.score < cfg["REJECT_BELOW"]:
        return "rejected"
    if applicant.score <= cfg["SELECT_ABOVE"]:
        return "filtered"
    role = applicant.role
    if role.test_questions and role.test_questions.strip():
        return "test_sent"
    return "selected"


def send_test(a):
    """Email the candidate their unique online test link. Returns (sent, url, err)."""
    if not a.test_token:
        a.test_token = secrets.token_urlsafe(24)
    test_url = f"{current_app.config['PUBLIC_BASE_URL']}/test/{a.test_token}"
    subject, body = mailer.test_invitation(a, test_url)
    sent, err = mailer.send(a.email, subject, body)
    if sent:
        a.test_sent_at = utcnow()
        log_event(a, "email_sent", f"Technical test sent to {a.email}", actor="system")
    else:
        log_event(a, "error",
                  f"Test email to {a.email} failed: {err} — send this link manually: {test_url}",
                  actor="system")
    return sent, test_url, err


def _eligible_for_test(a, cfg):
    """High score, role has a test, not yet emailed, not in a terminal status."""
    if a.test_sent_at or a.score is None:
        return False
    if a.score <= cfg["SELECT_ABOVE"]:
        return False
    if a.status in ("rejected", "hired", "test_sent", "test_returned"):
        return False
    role = a.role
    return bool(role.test_questions and role.test_questions.strip())


def _promote_to_test_sent(a, reason):
    old = a.status
    if old != "test_sent":
        a.status = "test_sent"
        log_event(a, "status_changed", f"{old} -> test_sent ({reason})", actor="system")
        clickup.sync_status(a)


def _run(app, applicant_id):
    with app.app_context():
        a = db.session.get(Applicant, applicant_id)
        if not a or a.deleted_at:
            return []
        cfg = app.config
        done = []
        try:
            # 1. summary
            if a.ai_summary is None and cfg["SUMMARY_ENABLED"]:
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

            # 2. automatic status from the score (+ test dispatch)
            target = decide_status(a, cfg)
            if target and target != a.status:
                test_note = ""
                if target == "test_sent":
                    sent, test_url, _err = send_test(a)
                    if not sent:
                        target = "selected"  # candidate not notified -> don't claim the test was sent
                        test_note = f" (test link for manual sending: {test_url})"
                old = a.status
                a.status = target
                log_event(a, "status_changed",
                          f"{old} -> {target} (auto: score {a.score}, thresholds <{cfg['REJECT_BELOW']} reject, "
                          f">{cfg['SELECT_ABOVE']} select){test_note}", actor="claude")
                done.append(f"auto_status:{target}")
                db.session.commit()
            elif _eligible_for_test(a, cfg):
                # Re-apply / previous failed send: status is no longer "new" so decide_status skipped.
                sent, test_url, _err = send_test(a)
                if sent:
                    _promote_to_test_sent(a, "test email sent")
                    done.append("auto_status:test_sent")
                db.session.commit()

            # 3. ClickUp task (created with the decided status; team added as watchers)
            if not a.clickup_task_id:
                if clickup.create_task(a):
                    done.append("clickup_task")
                    clickup.add_watchers(a)
                db.session.commit()
            elif f"auto_status:{a.status}" in done:
                clickup.sync_status(a)
                db.session.commit()

            # 4. summary comment with mentions (once)
            if a.clickup_task_id and a.ai_summary and not _has_event(a, "clickup_synced", "Summary comment"):
                if clickup.post_comment(a, a.ai_summary, mentions=True):
                    done.append("clickup_comment")
                db.session.commit()

            # 5. manual-send note on the task if the test email failed
            if a.clickup_task_id and a.status == "selected" and a.role.test_questions and not a.test_sent_at \
                    and not _has_event(a, "clickup_synced", "Test link posted"):
                url = f"{cfg['PUBLIC_BASE_URL']}/test/{a.test_token}" if a.test_token else None
                if url and clickup.post_comment(a, f"Email is not configured — please send the candidate their test "
                                                   f"link manually:\n{url}", mentions=False, label="Test link"):
                    db.session.commit()
        except Exception as exc:
            log.exception("process_application failed: %s", exc)
            db.session.rollback()
        finally:
            db.session.remove()
        return done


def process_test_submission(applicant_id, background=True):
    """After the candidate submits: grade with Claude, move to test_returned, post results on ClickUp."""
    app = current_app._get_current_object()
    if background:
        threading.Thread(target=_run_test, args=(app, applicant_id), daemon=True).start()
        return ["queued"]
    return _run_test(app, applicant_id)


def _run_test(app, applicant_id):
    with app.app_context():
        a = db.session.get(Applicant, applicant_id)
        if not a or a.deleted_at or not a.test_submitted_at:
            return []
        done = []
        try:
            if a.test_evaluation is None:
                try:
                    data = summarize.evaluate_test(a)
                    if data:
                        a.test_score = data["score"]
                        a.test_evaluation = summarize.format_test_result(a, data)
                        log_event(a, "scored", f"Test graded: {a.test_score}/100 ({data.get('verdict')})", actor="claude")
                        done.append("test_graded")
                except Exception as exc:
                    log.warning("Test evaluation failed for %s: %s", a.public_id, exc)
                    log_event(a, "error", f"Test evaluation failed: {exc}", actor="claude")
                db.session.commit()

            if a.status in ("test_sent", "selected"):
                old = a.status
                a.status = "test_returned"
                log_event(a, "status_changed", f"{old} -> test_returned (test submitted)", actor="system")
                clickup.sync_status(a)
                done.append("test_returned")
                db.session.commit()

            if a.clickup_task_id and a.test_evaluation and not _has_event(a, "clickup_synced", "Test result"):
                if clickup.post_comment(a, a.test_evaluation, mentions=True, label="Test result"):
                    done.append("clickup_test_comment")
                db.session.commit()
        except Exception as exc:
            log.exception("process_test_submission failed: %s", exc)
            db.session.rollback()
        finally:
            db.session.remove()
        return done


def retry_unsent_tests():
    """Applicants who qualified for a test but never got the email (mail was down/unconfigured):
    try sending again and move them to test_sent on success."""
    out = []
    rows = (Applicant.query.filter(Applicant.deleted_at.is_(None), Applicant.status == "selected",
                                   Applicant.test_sent_at.is_(None))
            .order_by(Applicant.created_at.asc()).limit(50).all())
    for a in rows:
        if not (a.role.test_questions and a.role.test_questions.strip()):
            continue
        sent, url, _err = send_test(a)
        if sent:
            old = a.status
            a.status = "test_sent"
            log_event(a, "status_changed", f"{old} -> test_sent (test email retried successfully)", actor="system")
            clickup.sync_status(a)
            out.append(f"{a.public_id} {a.full_name}: test sent on retry")
        else:
            out.append(f"{a.public_id} {a.full_name}: mail still failing ({url})")
        db.session.commit()
    return out


def resend_test(applicant):
    """Admin button: (re)send the test email now. Returns (sent, url, err)."""
    sent, url, err = send_test(applicant)
    if sent and applicant.status in ("new", "filtered", "selected"):
        old = applicant.status
        applicant.status = "test_sent"
        log_event(applicant, "status_changed", f"{old} -> test_sent (test sent from admin)", actor="admin")
        clickup.sync_status(applicant)
    db.session.commit()
    return sent, url, err


def _has_event(a, kind, message_prefix):
    return Event.query.filter(Event.applicant_id == a.id, Event.kind == kind,
                              Event.message.like(f"{message_prefix}%")).count() > 0


def pending_query():
    """Applicants still missing a summary, a task, or a grade for a submitted test."""
    return Applicant.query.filter(Applicant.deleted_at.is_(None)).filter(
        db.or_(Applicant.clickup_task_id.is_(None), Applicant.ai_summary.is_(None),
               db.and_(Applicant.test_submitted_at.isnot(None), Applicant.test_evaluation.is_(None)))
    ).order_by(Applicant.created_at.asc())
