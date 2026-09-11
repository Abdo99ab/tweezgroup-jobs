"""Recruiter admin: login, roles, applicants, status changes, CV download."""
import csv
import io
import re
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, send_file, session, url_for)

from . import clickup, cvbank, mailer, regions, sourcing
from .models import (CHANNEL_LABELS, CHANNEL_SEARCH, CHANNELS, SOURCE_LABELS, STATUSES, STATUS_LABELS, Applicant, Role, SourcedProfile,
                     SourcingAction, SourcingSearch, db, log_event, slugify)
from .naming import code_for_title
from .storage import get_storage

bp = Blueprint("admin", __name__)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin.login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ok = (request.form.get("username") == current_app.config["ADMIN_USERNAME"]
              and request.form.get("password") == current_app.config["ADMIN_PASSWORD"])
        if ok:
            session["admin"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("admin.dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("admin/login.html")


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@bp.get("/")
@login_required
def dashboard():
    roles = Role.query.order_by(Role.is_open.desc(), Role.created_at.desc()).all()
    base = Applicant.query.filter(Applicant.deleted_at.is_(None))
    counts = {s: base.filter_by(status=s).count() for s in STATUSES}
    recent = base.order_by(Applicant.created_at.desc()).limit(10).all()
    from .models import Setting
    mail_state, mail_msg = mailer.status()
    webhook_ok = bool(current_app.config["CLICKUP_WEBHOOK_SECRET"] or Setting.get("clickup_webhook_secret"))
    return render_template("admin/dashboard.html", roles=roles, counts=counts, recent=recent,
                           total=base.count(), statuses=STATUSES,
                           mail_state=mail_state, mail_msg=mail_msg, webhook_ok=webhook_ok,
                           poll_minutes=current_app.config["CLICKUP_POLL_MINUTES"])


# ---------- roles ----------

@bp.route("/roles/new", methods=["GET", "POST"])
@login_required
def role_new():
    if request.method == "POST":
        return _save_role(Role())
    return render_template("admin/role_form.html", role=None)


@bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@login_required
def role_edit(role_id):
    role = Role.query.get_or_404(role_id)
    if request.method == "POST":
        return _save_role(role)
    return render_template("admin/role_form.html", role=role, regions=regions)


def _save_role(role):
    f = request.form
    role.title = f.get("title", "").strip()
    if not role.title:
        flash("Title is required.", "error")
        return render_template("admin/role_form.html", role=role, regions=regions), 400
    slug = slugify(f.get("slug") or role.title)
    clash = Role.query.filter(Role.slug == slug, Role.id != role.id).first()
    if clash:
        flash("That URL slug is already used by another role.", "error")
        return render_template("admin/role_form.html", role=role, regions=regions), 400
    role.slug = slug
    role.code = (f.get("code", "").strip().upper() or code_for_title(role.title))[:8]
    role.department = f.get("department", "").strip() or None
    role.location = f.get("location", "").strip() or None
    role.employment_type = f.get("employment_type", "").strip() or None
    role.description = f.get("description", "").strip() or None
    role.requirements = f.get("requirements", "").strip() or None
    role.test_questions = f.get("test_questions", "").strip() or None
    role.test_answer_key = f.get("test_answer_key", "").strip() or None
    role.clickup_list_id = f.get("clickup_list_id", "").strip() or None
    role.drive_folder_id = f.get("drive_folder_id", "").strip() or None
    role.is_open = f.get("is_open") == "on"
    role.msg_connection = f.get("msg_connection", "").strip() or None
    role.msg_message = f.get("msg_message", "").strip() or None
    role.msg_reminder = f.get("msg_reminder", "").strip() or None
    role.msg_referral = f.get("msg_referral", "").strip() or None
    role.sourcing_brief = f.get("sourcing_brief", "").strip() or None
    role.target_regions = ",".join(regions.parse(f.getlist("target_regions"))) or None
    role.target_custom = f.get("target_custom", "").strip()[:400] or None
    role.sourcing_autosend = f.get("sourcing_autosend") == "on"
    if role.id is None:
        db.session.add(role)
    db.session.commit()
    flash("Role saved.", "ok")
    return redirect(url_for("admin.applicants", role=role.slug))


@bp.post("/mail-test")
@login_required
def mail_test():
    """Send a test email to the admin's chosen address to verify the mail configuration."""
    to = request.form.get("to", "").strip()
    if not to:
        flash("Enter an address to send the test email to.", "error")
    else:
        sent, err = mailer.send(to, f"{current_app.config['COMPANY_NAME']} recruiting — mail test",
                                "This is a test email from the applicant system. Mail is configured correctly.")
        if sent:
            flash(f"Test email sent to {to} — check the inbox.", "ok")
        else:
            flash(f"Sending failed: {err}", "error")
    return redirect(url_for("admin.dashboard"))


@bp.post("/roles/<int:role_id>/toggle")
@login_required
def role_toggle(role_id):
    """One-click Active/Paused switch from the dashboard: paused roles disappear from /jobs
    and their direct apply link shows the 'paused' page. Existing applicants are untouched."""
    role = Role.query.get_or_404(role_id)
    role.is_open = not role.is_open
    db.session.commit()
    flash(f"{role.title} is now {'ACTIVE — visible to candidates' if role.is_open else 'PAUSED — hidden from candidates'}.", "ok")
    return redirect(request.referrer or url_for("admin.dashboard"))


@bp.post("/roles/<int:role_id>/delete")
@login_required
def role_delete(role_id):
    """Remove a job from the site and ATS. Drive files and ClickUp tasks are left in place."""
    role = Role.query.get_or_404(role_id)
    title = role.title
    # Sourcing rows first (FK to role and optionally to applicants).
    for p in role.sourced_profiles.all():
        db.session.delete(p)
    for s in role.searches.all():
        db.session.delete(s)
    applicants = role.applicants.all()
    n = sum(1 for a in applicants if not a.deleted_at)
    for a in applicants:
        db.session.delete(a)
    db.session.delete(role)
    db.session.commit()
    extra = f" and {n} applicant record{'s' if n != 1 else ''}" if n else ""
    flash(f'Deleted “{title}”{extra}. Files on Google Drive and ClickUp tasks were not removed.', "ok")
    return redirect(url_for("admin.dashboard"))


# ---------- applicants ----------

@bp.get("/applicants")
@login_required
def applicants():
    q = Applicant.query.filter(Applicant.deleted_at.is_(None))
    role_slug = request.args.get("role")
    status = request.args.get("status")
    search = request.args.get("q", "").strip()
    if role_slug:
        role = Role.query.filter_by(slug=role_slug).first_or_404()
        q = q.filter_by(role_id=role.id)
    else:
        role = None
    if status in STATUSES:
        q = q.filter_by(status=status)
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(Applicant.full_name.ilike(like), Applicant.email.ilike(like),
                            Applicant.cv_text.ilike(like)))
    sort = request.args.get("sort", "newest")
    if sort == "score":
        q = q.order_by(Applicant.score.desc().nullslast(), Applicant.created_at.desc())
    else:
        q = q.order_by(Applicant.created_at.desc())
    rows = q.limit(500).all()
    roles = Role.query.order_by(Role.title).all()
    return render_template("admin/applicants.html", rows=rows, roles=roles, role=role, status=status,
                           search=search, sort=sort, statuses=STATUSES)


@bp.get("/applicants/export.csv")
@login_required
def export_csv():
    q = Applicant.query.filter(Applicant.deleted_at.is_(None)).order_by(Applicant.created_at.desc())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "role", "name", "email", "phone", "linkedin", "location", "years_experience", "source",
                "status", "score", "applied_at", "cv_url", "clickup_task_url"])
    for a in q:
        w.writerow([a.public_id, a.role.title, a.full_name, a.email, a.phone or "", a.linkedin_url or "",
                    a.location or "", a.years_experience if a.years_experience is not None else "", a.source,
                    a.status, a.score if a.score is not None else "", a.created_at.isoformat(),
                    a.cv_url or "", a.clickup_task_url or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=applicants.csv"})


@bp.route("/applicants/<public_id>", methods=["GET", "POST"])
@login_required
def applicant_detail(public_id):
    a = Applicant.query.filter_by(public_id=public_id).first_or_404()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "status":
            new = request.form.get("status")
            if new in STATUSES and new != a.status:
                old = a.status
                a.status = new
                log_event(a, "status_changed", f"{old} -> {new}", actor="admin")
                clickup.sync_status(a)
                flash(f"Status set to {STATUS_LABELS[new]}.", "ok")
        elif action == "notes":
            a.notes = request.form.get("notes", "").strip() or None
            log_event(a, "note", "Recruiter notes updated", actor="admin")
            flash("Notes saved.", "ok")
        elif action == "send_test":
            from . import pipeline
            if not (a.role.test_questions and a.role.test_questions.strip()):
                flash("This role has no test defined — add one in the role form first.", "error")
            else:
                sent, url, err = pipeline.resend_test(a)
                if sent:
                    flash(f"Test email sent to {a.email}.", "ok")
                else:
                    flash(f"Email failed: {err}. Send this link manually: {url}", "error")
            return redirect(url_for("admin.applicant_detail", public_id=public_id))
        elif action == "process":
            from . import pipeline
            db.session.commit()
            result = pipeline.process_application(a.id, background=False)
            flash("Processed: " + ", ".join(result) if result else "Nothing to do.", "ok")
            return redirect(url_for("admin.applicant_detail", public_id=public_id))
        elif action == "delete":
            from . import purge_applicant
            purge_applicant(a, get_storage())
            flash("Applicant data deleted.", "ok")
            db.session.commit()
            return redirect(url_for("admin.applicants"))
        db.session.commit()
        return redirect(url_for("admin.applicant_detail", public_id=public_id))
    cv_url = get_storage().url(a.cv_key) if a.cv_key else None
    return render_template("admin/applicant_detail.html", a=a, statuses=STATUSES, cv_url=cv_url)


@bp.get("/applicants/<public_id>/cv")
@login_required
def applicant_cv(public_id):
    a = Applicant.query.filter_by(public_id=public_id).first_or_404()
    if not a.cv_key:
        abort(404)
    data = get_storage().get(a.cv_key)
    return send_file(io.BytesIO(data), mimetype=a.cv_mime or "application/octet-stream",
                     as_attachment=False, download_name=a.cv_filename)


# ---------- TweezCVBank ----------

@bp.get("/cv-bank")
@login_required
def cv_bank():
    index = cvbank.cached_index()
    q = (request.args.get("q") or "").strip()
    hits = cvbank.search(index, q) if (index and q) else None
    return render_template("admin/cvbank.html", index=index, stats=cvbank.stats(index), q=q, hits=hits,
                           fmt_size=cvbank.fmt_size, drive_live=cvbank._drive() is not None)


@bp.get("/cv-bank/f/<folder_id>")
@login_required
def cv_bank_folder(folder_id):
    index = cvbank.cached_index()
    folder = cvbank.find_folder(index or {}, folder_id)
    if not folder:
        flash("That folder is not in the index yet — rescan the Drive.", "error")
        return redirect(url_for("admin.cv_bank"))
    return render_template("admin/cvbank_folder.html", folder=folder, index=index, fmt_size=cvbank.fmt_size)


@bp.post("/cv-bank/refresh")
@login_required
def cv_bank_refresh():
    try:
        index = cvbank.scan()
        flash(f"Scanned the CV bank: {index['total']} files in {len(index['folders'])} role folders.", "ok")
    except Exception as exc:
        flash(f"Scan failed: {exc}", "error")
    return redirect(url_for("admin.cv_bank"))


@bp.route("/cv-bank/organize", methods=["GET", "POST"])
@login_required
def cv_bank_organize():
    apply = request.method == "POST" and request.form.get("confirm") == "1"
    try:
        result = cvbank.organize(apply=apply)
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.cv_bank"))
    if apply:
        flash(f"Renamed {result['applied']} file(s) to the naming convention.", "ok")
        try:
            cvbank.scan()
        except Exception:
            pass
        return redirect(url_for("admin.cv_bank"))
    return render_template("admin/cvbank_organize.html", renames=result["renames"])


# ---------- Sourcing (Milestone 1) ----------

@bp.get("/sourcing")
@login_required
def sourcing_home():
    roles = Role.query.order_by(Role.is_open.desc(), Role.title).all()
    per_role = {}
    for r in roles:
        f = sourcing.funnel(r)
        tot = {s: sum(ch[s] for ch in f.values()) for s in sourcing.FUNNEL_STAGES}
        pending = r.sourced_profiles.filter_by(decision="pending").count()
        waiting = r.sourced_profiles.filter(SourcedProfile.decision == "approved", SourcedProfile.closed_at.is_(None),
                                            SourcedProfile.next_action.isnot(None),
                                            SourcedProfile.released.is_(False)).count()
        per_role[r.id] = {"funnel": tot, "pending": pending, "waiting": waiting,
                          "searches": r.searches.filter(SourcingSearch.status.in_(("pending", "running"))).count()}
    status = sourcing.worker_status()
    last = SourcingAction.query.filter_by(actor="worker").order_by(SourcingAction.created_at.desc()).first()
    return render_template("admin/sourcing.html", roles=roles, per_role=per_role, status=status, last=last,
                           channels=CHANNELS, funnel_all=sourcing.funnel(None), stages=sourcing.FUNNEL_STAGES)


@bp.post("/sourcing/pause")
@login_required
def sourcing_pause():
    if request.form.get("resume") == "1":
        sourcing.resume()
        flash("Sourcing resumed.", "ok")
    else:
        until = sourcing.pause(reason="paused by recruiter")
        flash(f"Sourcing paused until {until:%d %b %H:%M} UTC.", "ok")
    return redirect(url_for("admin.sourcing_home"))


@bp.post("/sourcing/refer")
@login_required
def sourcing_refer():
    role = Role.query.filter_by(slug=request.form.get("role")).first()
    name = request.form.get("full_name", "").strip()
    if not role or len(name) < 2:
        flash("Pick a role and enter the candidate's name.", "error")
        return redirect(url_for("admin.sourcing_home"))
    try:
        p, sent = sourcing.add_manual(role, name, profile_url=request.form.get("profile_url"),
                                     email=request.form.get("email"),
                                     channel="referral" if request.form.get("referred_by") else "manual",
                                     referred_by=request.form.get("referred_by", "").strip() or None)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.sourcing_home"))
    link = sourcing.apply_url(p)
    if sent:
        flash(f"Link emailed to {p.email}: {link}", "ok")
    elif sent is False:
        flash(f"Candidate added but the email failed — send them this link yourself: {link}", "error")
    else:
        flash(f"Candidate added. Personal link: {link}", "ok")
    return redirect(url_for("admin.sourcing_role", slug=role.slug))


@bp.route("/sourcing/<slug>", methods=["GET", "POST"])
@login_required
def sourcing_role(slug):
    role = Role.query.filter_by(slug=slug).first_or_404()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "search":
            q = request.form.get("query", "").strip()
            if not q:
                flash("Type a search query.", "error")
            else:
                channel = request.form.get("channel", "linkedin")
                created = sourcing.create_searches(
                    role, q, channel=channel, region_keys=request.form.getlist("regions"),
                    custom=request.form.get("custom", ""), with_cities=request.form.get("with_cities") == "on",
                    max_results=max(5, min(200, int(request.form.get("max_results") or 50))))
                db.session.commit()
                started = sum(1 for s in created if sourcing.start_search(s))
                where = ", ".join(s.location for s in created if s.location) or "no location filter"
                if started:
                    flash(f"{started} {channel} search(es) running ({where}) — results appear in “To review” shortly.", "ok")
                else:
                    flash(f"{len(created)} search(es) queued for the worker ({where}).", "ok")
        elif action == "import":
            urls = [u for u in re.split(r"[\s,;]+", request.form.get("urls", "")) if u.strip()]
            if not urls:
                flash("Paste at least one profile URL.", "error")
            else:
                s, n = sourcing.import_profiles(role, urls[:200], request.form.get("channel") or "auto")
                db.session.commit()
                flash(f"{n} new profile(s) imported and sent for pre-scoring ({len(urls)} URL(s) pasted).", "ok")
                return redirect(url_for("admin.sourcing_role", slug=slug, tab="shortlist"))
        elif action == "suggest":
            try:
                ideas = sourcing.suggest_searches(role)
            except Exception as exc:
                ideas = []
                flash(f"Claude could not suggest searches: {exc}", "error")
            for i in ideas:
                if i.get("query"):
                    db.session.add(SourcingSearch(role=role, channel="linkedin", keywords=i["query"][:400],
                                                  location=(i.get("location") or "")[:200] or None, max_results=50,
                                                  status="draft", error=i.get("why")))
            db.session.commit()
            if ideas:
                flash(f"{len(ideas)} search ideas added as drafts — activate the ones you like.", "ok")
        elif action == "search_state":
            s = db.session.get(SourcingSearch, int(request.form.get("search_id", 0)))
            if s and s.role_id == role.id:
                new = request.form.get("state")
                if new in ("pending", "draft"):
                    s.status = new
                elif new == "delete":
                    db.session.delete(s)
                db.session.commit()
        elif action == "decide":
            ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
            decision = request.form.get("decision")
            n = 0
            for p in SourcedProfile.query.filter(SourcedProfile.id.in_(ids), SourcedProfile.role_id == role.id):
                if decision == "approve":
                    sourcing.approve(p)
                    n += 1
                elif decision == "reject":
                    sourcing.reject(p)
                    n += 1
                elif decision == "release":
                    sourcing.release(p)
                    n += 1
            db.session.commit()
            flash(f"{n} profile(s) {decision}d." if n else "Nothing selected.", "ok" if n else "error")
        return redirect(url_for("admin.sourcing_role", slug=slug, tab=request.form.get("tab", "shortlist")))

    base = role.sourced_profiles
    shortlist = base.filter_by(decision="pending").order_by(SourcedProfile.pre_score.desc().nullslast(),
                                                             SourcedProfile.created_at.desc()).limit(300).all()
    queue = base.filter(SourcedProfile.decision == "approved", SourcedProfile.closed_at.is_(None),
                        SourcedProfile.next_action.isnot(None)).order_by(
        SourcedProfile.released.asc(), SourcedProfile.next_due_at.asc().nullsfirst()).all()
    contacted = base.filter(SourcedProfile.decision == "approved").order_by(
        SourcedProfile.updated_at.desc()).limit(300).all()
    searches = role.searches.order_by(SourcingSearch.created_at.desc()).limit(30).all()
    return render_template("admin/sourcing_role.html", role=role, shortlist=shortlist, queue=queue,
                           contacted=contacted, searches=searches, funnel=sourcing.funnel(role),
                           stages=sourcing.FUNNEL_STAGES, tab=request.args.get("tab", "shortlist"),
                           status=sourcing.worker_status(), channels=CHANNELS, channel_labels=CHANNEL_LABELS,
                           channel_search=CHANNEL_SEARCH, apply_url=sourcing.apply_url, regions=regions,
                           role_regions=regions.parse(role.target_regions))


@bp.route("/sourcing/profile/<int:pid>", methods=["GET", "POST"])
@login_required
def sourcing_profile(pid):
    p = db.session.get(SourcedProfile, pid) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            p.draft_note = request.form.get("draft_note", "").strip()[:300] or None
            p.draft_message = request.form.get("draft_message", "").strip() or None
            p.draft_reminder = request.form.get("draft_reminder", "").strip() or None
            flash("Drafts saved.", "ok")
        elif action == "regenerate":
            sourcing.prepare_drafts(p, force=True)
            flash("Drafts regenerated from the role templates.", "ok")
        elif action == "approve":
            sourcing.approve(p)
            flash("Approved — added to the outreach queue.", "ok")
        elif action == "reject":
            sourcing.reject(p)
            flash("Rejected.", "ok")
        elif action == "release":
            sourcing.release(p)
            flash(f"Send confirmed — the worker will {p.next_action} on its next pass.", "ok")
        elif action == "hold":
            sourcing.hold(p)
            flash("On hold — the worker will not send until you confirm.", "ok")
        elif action == "manual":
            # recruiter did the step by hand (or wants to mark acceptance seen in LinkedIn)
            step = request.form.get("step")
            if step in sourcing.ACTIONS:
                sourcing.record_outcome(p, step, ok=True, detail="done manually by recruiter",
                                        accepted=(step == "check_accept"), actor="admin")
                flash(f"Marked '{step}' as done.", "ok")
        elif action == "reply":
            cls = sourcing.ingest_reply(p, request.form.get("reply_text"))
            flash(f"Reply saved ({cls}).", "ok")
        elif action == "email":
            ok, err = sourcing.email_link(p)
            flash(f"Link emailed to {p.email}." if ok else f"Email failed: {err}", "ok" if ok else "error")
        elif action == "prescore":
            try:
                sourcing.prescore(p)
                flash(f"Pre-score: {p.pre_score}.", "ok")
            except Exception as exc:
                flash(f"Pre-score failed: {exc}", "error")
        db.session.commit()
        return redirect(url_for("admin.sourcing_profile", pid=pid))
    return render_template("admin/sourcing_profile.html", p=p, apply_url=sourcing.apply_url(p),
                           actions=p.actions.limit(50).all())
