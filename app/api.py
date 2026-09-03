"""JSON API for the recruiting agent (Claude via MCP/HTTP). Auth: X-API-Key header or ?api_key=.

  GET    /api/v1/roles                          list roles (+ requirements knowledge base)
  GET    /api/v1/roles/<slug>                   one role
  PATCH  /api/v1/roles/<slug>                   update requirements / description / is_open
  GET    /api/v1/applicants?status=new&role=..&since=ISO&limit=50
  GET    /api/v1/applicants/<id>                full record incl. cv_text and events
  GET    /api/v1/applicants/<id>/cv             raw CV file
  PATCH  /api/v1/applicants/<id>                {status, score, ai_summary, notes}
  POST   /api/v1/applicants/<id>/process        re-run auto-summary + ClickUp task/comment
  POST   /api/v1/applicants/<id>/events         {kind, message}  e.g. email_sent
  DELETE /api/v1/applicants/<id>                purge personal data (GDPR request)
  POST   /api/v1/applicants                     create applicant without a file (e.g. LinkedIn sourcing)
  GET    /api/v1/stats                          pipeline counts per role/status
  POST   /api/v1/maintenance/purge-expired      GDPR retention purge (for hosts without cron)
"""
import io
from datetime import datetime
from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from . import clickup
from .models import SOURCES, STATUSES, Applicant, Role, db, log_event, utcnow
from .storage import get_storage

bp = Blueprint("api", __name__)


def require_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key or key != current_app.config["API_KEY"]:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return wrapper


def _applicant(public_id):
    a = Applicant.query.filter_by(public_id=public_id).first()
    if not a or a.deleted_at:
        abort(404)
    return a


@bp.errorhandler(404)
def nf(_):
    return jsonify(error="not found"), 404


@bp.errorhandler(400)
def br(e):
    return jsonify(error=str(getattr(e, "description", "bad request"))), 400


# ---------- roles ----------

@bp.get("/roles")
@require_key
def roles():
    q = Role.query
    if request.args.get("open") == "1":
        q = q.filter_by(is_open=True)
    return jsonify(roles=[r.to_dict(include_counts=True) for r in q.order_by(Role.created_at.desc())])


@bp.get("/roles/<slug>")
@require_key
def role(slug):
    r = Role.query.filter_by(slug=slug).first_or_404()
    return jsonify(role=r.to_dict(include_counts=True))


@bp.patch("/roles/<slug>")
@require_key
def role_patch(slug):
    r = Role.query.filter_by(slug=slug).first_or_404()
    body = request.get_json(silent=True) or {}
    for field in ("title", "code", "department", "location", "employment_type", "description",
                  "requirements", "test_questions", "test_answer_key", "clickup_list_id", "drive_folder_id"):
        if field in body:
            setattr(r, field, body[field])
    if "is_open" in body:
        r.is_open = bool(body["is_open"])
    db.session.commit()
    return jsonify(role=r.to_dict())


# ---------- applicants ----------

@bp.get("/applicants")
@require_key
def applicants():
    q = Applicant.query.filter(Applicant.deleted_at.is_(None))
    if request.args.get("role"):
        r = Role.query.filter_by(slug=request.args["role"]).first_or_404()
        q = q.filter_by(role_id=r.id)
    if request.args.get("status"):
        wanted = [s for s in request.args["status"].split(",") if s in STATUSES]
        q = q.filter(Applicant.status.in_(wanted))
    if request.args.get("since"):
        try:
            since = datetime.fromisoformat(request.args["since"].replace("Z", ""))
        except ValueError:
            abort(400, "since must be ISO-8601")
        q = q.filter(Applicant.created_at >= since)
    if request.args.get("unscored") == "1":
        q = q.filter(Applicant.score.is_(None))
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    include_text = request.args.get("include_cv_text") == "1"
    rows = q.order_by(Applicant.created_at.asc()).offset(offset).limit(limit).all()
    return jsonify(applicants=[a.to_dict(include_cv_text=include_text) for a in rows],
                   count=len(rows), offset=offset, limit=limit)


@bp.get("/applicants/<public_id>")
@require_key
def applicant(public_id):
    a = _applicant(public_id)
    return jsonify(applicant=a.to_dict(include_cv_text=True, include_events=True))


@bp.get("/applicants/<public_id>/cv")
@require_key
def applicant_cv(public_id):
    a = _applicant(public_id)
    if not a.cv_key:
        abort(404)
    data = get_storage().get(a.cv_key)
    return send_file(io.BytesIO(data), mimetype=a.cv_mime or "application/octet-stream",
                     download_name=a.cv_filename)


@bp.patch("/applicants/<public_id>")
@require_key
def applicant_patch(public_id):
    a = _applicant(public_id)
    body = request.get_json(silent=True) or {}
    actor = body.get("actor", "agent")
    changed = []

    # 1) score / summary / notes first, so a task created by the status change below carries them
    if "score" in body:
        score = body["score"]
        if score is not None and not (isinstance(score, int) and 0 <= score <= 100):
            abort(400, "score must be an integer 0-100 or null")
        a.score = score
        changed.append("score")
    if "ai_summary" in body:
        a.ai_summary = body["ai_summary"]
        changed.append("ai_summary")
    if "notes" in body:
        a.notes = body["notes"]
        changed.append("notes")
    if "score" in changed or "ai_summary" in changed:
        log_event(a, "scored", f"score={a.score}" + (f" — {a.ai_summary[:200]}" if a.ai_summary else ""), actor=actor)

    # 2) status -> ClickUp (creates the task on first board status, else updates it)
    had_task = bool(a.clickup_task_id)
    if "status" in body:
        if body["status"] not in STATUSES:
            abort(400, f"status must be one of {STATUSES}")
        if body["status"] != a.status:
            old = a.status
            a.status = body["status"]
            log_event(a, "status_changed", f"{old} -> {a.status}", actor=actor)
            if body.get("sync_clickup", True):
                clickup.sync_status(a)
            changed.append("status")

    # 3) screening comment on an already-existing task (a freshly created task gets it in create_task)
    if had_task and a.ai_summary and ("score" in changed or "ai_summary" in changed) and body.get("sync_clickup", True):
        clickup.post_comment(a, f"Screening update ({actor}) — score {a.score}/100\n\n{a.ai_summary}", mentions=False)
    db.session.commit()
    return jsonify(applicant=a.to_dict(), changed=changed)


@bp.post("/applicants/<public_id>/process")
@require_key
def applicant_process(public_id):
    """Re-run the automation for one applicant (summary/status/task/comment, and test grading if submitted)."""
    from . import pipeline
    a = _applicant(public_id)
    done = pipeline.process_application(a.id, background=False)
    a = _applicant(public_id)
    if a.test_submitted_at and a.test_evaluation is None:
        done += pipeline.process_test_submission(a.id, background=False)
    return jsonify(ok=True, steps=done, applicant=_applicant(public_id).to_dict())


@bp.post("/applicants/<public_id>/events")
@require_key
def applicant_event(public_id):
    a = _applicant(public_id)
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if not kind:
        abort(400, "kind is required (e.g. email_sent, interview_scheduled, note)")
    log_event(a, kind, body.get("message"), actor=body.get("actor", "agent"))
    db.session.commit()
    return jsonify(ok=True, events=[e.to_dict() for e in a.events.limit(20)])


@bp.delete("/applicants/<public_id>")
@require_key
def applicant_delete(public_id):
    from . import purge_applicant
    a = _applicant(public_id)
    purge_applicant(a, get_storage())
    db.session.commit()
    return jsonify(ok=True)


@bp.post("/applicants")
@require_key
def applicant_create():
    """Register a candidate the agent sourced (e.g. LinkedIn) before they upload a CV.
    Returns the personalised apply link to send them."""
    body = request.get_json(silent=True) or {}
    r = Role.query.filter_by(slug=body.get("role")).first()
    if not r:
        abort(400, "role slug is required")
    if not body.get("full_name") or not body.get("email"):
        abort(400, "full_name and email are required")
    email = body["email"].strip().lower()
    a = Applicant.query.filter_by(role_id=r.id, email=email).filter(Applicant.deleted_at.is_(None)).first()
    created = a is None
    if created:
        a = Applicant(role=r, email=email)
        db.session.add(a)
    a.full_name = body["full_name"].strip()
    a.phone = body.get("phone") or a.phone
    a.linkedin_url = body.get("linkedin_url") or a.linkedin_url
    a.location = body.get("location") or a.location
    a.source = body.get("source") if body.get("source") in SOURCES else "linkedin"
    a.status = body.get("status", a.status or "new")
    a.set_retention(current_app.config["RETENTION_MONTHS"])
    db.session.flush()
    if created:
        log_event(a, "sourced", body.get("message") or f"Sourced via {a.source}", actor=body.get("actor", "agent"))
        if body.get("create_clickup_task", True):
            clickup.create_task(a)
    db.session.commit()
    apply_url = f"{current_app.config['PUBLIC_BASE_URL']}/apply/{r.slug}?src={a.source}"
    return jsonify(applicant=a.to_dict(), created=created, apply_url=apply_url), (201 if created else 200)


@bp.post("/maintenance/purge-expired")
@require_key
def maintenance_purge():
    """Same as `flask purge-expired`; for hosts without cron. Safe to call any time."""
    from . import purge_applicant
    now = utcnow()
    rows = Applicant.query.filter(Applicant.retention_until <= now, Applicant.deleted_at.is_(None)).all()
    storage = get_storage()
    for a in rows:
        purge_applicant(a, storage)
    db.session.commit()
    return jsonify(purged=len(rows), at=now.isoformat() + "Z")


@bp.get("/stats")
@require_key
def stats():
    out = []
    for r in Role.query.order_by(Role.title):
        base = r.applicants.filter(Applicant.deleted_at.is_(None))
        out.append({
            "role": r.slug, "title": r.title, "is_open": r.is_open,
            "total": base.count(),
            "by_status": {s: base.filter_by(status=s).count() for s in STATUSES},
            "unscored": base.filter(Applicant.score.is_(None)).count(),
        })
    return jsonify(roles=out, generated_at=utcnow().isoformat() + "Z")
