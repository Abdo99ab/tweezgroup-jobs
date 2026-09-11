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


# ====================================================================================== sourcing (M1)
# The browser worker (worker/) and the admin both drive sourcing through these endpoints.

from .models import SourcedProfile, SourcingSearch  # noqa: E402
from . import sourcing  # noqa: E402


@bp.get("/sourcing/status")
@require_key
def sourcing_status():
    """Worker heartbeat: paused?, caps left, active hours, how much work is waiting."""
    return jsonify(sourcing.worker_status())


@bp.post("/sourcing/pause")
@require_key
def sourcing_pause():
    """Worker saw a LinkedIn warning/captcha (or a human wants a break): stop everything for a while."""
    body = request.get_json(silent=True) or {}
    until = sourcing.pause(body.get("hours"), body.get("reason") or "paused via API")
    return jsonify(paused_until=until.isoformat() + "Z")


@bp.post("/sourcing/resume")
@require_key
def sourcing_resume():
    sourcing.resume()
    return jsonify(ok=True)


@bp.get("/sourcing/searches")
@require_key
def sourcing_searches():
    """Searches for the worker to run (status=pending by default)."""
    status = request.args.get("status", "pending")
    channel = request.args.get("channel", "linkedin")
    q = SourcingSearch.query.filter_by(channel=channel)
    if status != "all":
        q = q.filter_by(status=status)
    rows = q.order_by(SourcingSearch.created_at.asc()).limit(20).all()
    if status == "pending" and rows and not sourcing.paused_until():
        for s in rows[:1]:
            s.status = "running"
            s.started_at = utcnow()
        db.session.commit()
        rows = rows[:1]
    elif status == "pending" and sourcing.paused_until():
        rows = []
    return jsonify(searches=[s.to_dict() for s in rows])


@bp.post("/sourcing/searches")
@require_key
def sourcing_search_create():
    body = request.get_json(silent=True) or {}
    r = Role.query.filter_by(slug=body.get("role")).first()
    if not r or not body.get("query"):
        abort(400, "role slug and query are required")
    """{role, query, channel?, regions?: ["france","maghreb"], custom?: "Lyon, Oran", location?, with_cities?, max_results?}
    One search is created per target location (regions expanded per channel)."""
    created = sourcing.create_searches(
        r, body["query"], channel=body.get("channel", "linkedin"),
        region_keys=body.get("regions") or (body.get("region_keys") or []),
        custom=body.get("custom") or body.get("location") or "", with_cities=bool(body.get("with_cities")),
        max_results=int(body.get("max_results", 50)))
    db.session.commit()
    for s in created:
        sourcing.start_search(s)   # GitHub etc. run immediately inside the app
    return jsonify(searches=[s.to_dict() for s in created], search=created[0].to_dict()), 201


@bp.get("/sourcing/regions")
@require_key
def sourcing_regions():
    from . import regions
    return jsonify(regions=[{"key": k, **regions.REGIONS[k]} for k in regions.ORDER])


@bp.post("/sourcing/import")
@require_key
def sourcing_import():
    """{role, urls: [...], channel?: auto|behance|artstation|kaggle|contra|github|…} — enrich + pre-score."""
    body = request.get_json(silent=True) or {}
    r = Role.query.filter_by(slug=body.get("role")).first()
    urls = body.get("urls") or []
    if not r or not urls:
        abort(400, "role slug and a non-empty urls list are required")
    s, n = sourcing.import_profiles(r, urls[:200], body.get("channel"))
    return jsonify(search=s.to_dict(), new_profiles=n,
                   profiles=[p.to_dict() for p in s.profiles.order_by(SourcedProfile.id).all()]), 201


@bp.post("/sourcing/profiles/<int:pid>/email")
@require_key
def sourcing_email(pid):
    p = db.session.get(SourcedProfile, pid) or abort(404)
    ok, err = sourcing.email_link(p, actor="agent")
    db.session.commit()
    return jsonify(ok=ok, error=err, profile=p.to_dict())


@bp.post("/sourcing/searches/<int:search_id>/results")
@require_key
def sourcing_search_results(search_id):
    """Worker posts scraped profiles: [{profile_url, full_name, headline, location, company, about}].
    New profiles are pre-scored by Claude in the background. done=true closes the search."""
    s = db.session.get(SourcingSearch, search_id) or abort(404)
    body = request.get_json(silent=True) or {}
    n = sourcing.ingest_results(s, body.get("profiles") or [], done=bool(body.get("done")), error=body.get("error"))
    return jsonify(search=s.to_dict(), new_profiles=n)


@bp.get("/sourcing/profiles")
@require_key
def sourcing_profiles():
    q = SourcedProfile.query
    if request.args.get("role"):
        r = Role.query.filter_by(slug=request.args["role"]).first_or_404()
        q = q.filter_by(role_id=r.id)
    if request.args.get("decision"):
        q = q.filter_by(decision=request.args["decision"])
    if request.args.get("channel"):
        q = q.filter_by(channel=request.args["channel"])
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = q.order_by(SourcedProfile.pre_score.desc().nullslast(), SourcedProfile.created_at.desc()).limit(limit).all()
    return jsonify(profiles=[p.to_dict(include_drafts=request.args.get("include_drafts") == "1") for p in rows])


@bp.post("/sourcing/profiles")
@require_key
def sourcing_profile_add():
    """Add one candidate by hand or from an API channel (Torre, Apollo, GitHub, referral)."""
    body = request.get_json(silent=True) or {}
    r = Role.query.filter_by(slug=body.get("role")).first()
    if not r or not body.get("full_name"):
        abort(400, "role slug and full_name are required")
    try:
        p, sent = sourcing.add_manual(r, body["full_name"], profile_url=body.get("profile_url"), email=body.get("email"),
                                     channel=body.get("channel", "manual"), referred_by=body.get("referred_by"),
                                     headline=body.get("headline"), location=body.get("location"), actor="agent",
                                     send_email=body.get("send_email", True))
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify(profile=p.to_dict(include_drafts=True), apply_url=sourcing.apply_url(p), email_sent=sent), 201


@bp.patch("/sourcing/profiles/<int:pid>")
@require_key
def sourcing_profile_patch(pid):
    """{decision: approved|rejected, released: bool, draft_note, draft_message, draft_reminder}"""
    p = db.session.get(SourcedProfile, pid) or abort(404)
    body = request.get_json(silent=True) or {}
    actor = body.get("actor", "agent")
    if body.get("decision") == "approved" and p.decision != "approved":
        sourcing.approve(p, actor=actor)
    elif body.get("decision") == "rejected":
        sourcing.reject(p, actor=actor)
    for f in ("draft_note", "draft_message", "draft_reminder"):
        if f in body:
            setattr(p, f, body[f])
    if body.get("released") is True:
        sourcing.release(p, actor=actor)
    elif body.get("released") is False:
        sourcing.hold(p, actor=actor)
    db.session.commit()
    return jsonify(profile=p.to_dict(include_drafts=True), apply_url=sourcing.apply_url(p))


@bp.get("/sourcing/queue")
@require_key
def sourcing_queue():
    """Actions the worker must perform now: [{profile_id, action, profile_url, text}]. Empty while paused
    or when today's caps are used up. Items are locked for 2 h; report each with /outcome."""
    limit = min(int(request.args.get("limit", 10)), 50)
    return jsonify(actions=sourcing.next_queue(limit=limit, channel=request.args.get("channel", "linkedin")),
                   status=sourcing.worker_status())


@bp.post("/sourcing/profiles/<int:pid>/outcome")
@require_key
def sourcing_outcome(pid):
    """{action, ok, detail, accepted (for check_accept), reply_text, warning}"""
    p = db.session.get(SourcedProfile, pid) or abort(404)
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in sourcing.ACTIONS + ["view"]:
        abort(400, f"action must be one of {sourcing.ACTIONS}")
    sourcing.record_outcome(p, action, ok=bool(body.get("ok", True)), detail=body.get("detail"),
                            accepted=body.get("accepted"), reply_text=body.get("reply_text"),
                            warning=body.get("warning"))
    db.session.commit()
    return jsonify(profile=p.to_dict())


@bp.post("/sourcing/inbox")
@require_key
def sourcing_inbox():
    """Replies scraped from the inbox: {messages: [{profile_url, text, at}]} — only threads we started."""
    body = request.get_json(silent=True) or {}
    n = sourcing.ingest_inbox(body.get("messages") or [])
    return jsonify(matched=n)


@bp.get("/sourcing/funnel")
@require_key
def sourcing_funnel():
    role = None
    if request.args.get("role"):
        role = Role.query.filter_by(slug=request.args["role"]).first_or_404()
    return jsonify(funnel=sourcing.funnel(role))
