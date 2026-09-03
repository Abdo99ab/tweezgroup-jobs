"""Recruiter admin: login, roles, applicants, status changes, CV download."""
import csv
import io
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, send_file, session, url_for)

from . import clickup, mailer
from .models import STATUSES, STATUS_LABELS, Applicant, Role, db, log_event, slugify
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
    return render_template("admin/role_form.html", role=role)


def _save_role(role):
    f = request.form
    role.title = f.get("title", "").strip()
    if not role.title:
        flash("Title is required.", "error")
        return render_template("admin/role_form.html", role=role), 400
    slug = slugify(f.get("slug") or role.title)
    clash = Role.query.filter(Role.slug == slug, Role.id != role.id).first()
    if clash:
        flash("That URL slug is already used by another role.", "error")
        return render_template("admin/role_form.html", role=role), 400
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
    elif mailer.send(to, f"{current_app.config['COMPANY_NAME']} recruiting — mail test",
                     "This is a test email from the applicant system. Mail is configured correctly."):
        flash(f"Test email sent to {to} — check the inbox.", "ok")
    else:
        flash("Sending failed — check the mail settings (see the banner) and the server logs.", "error")
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
                sent, url = pipeline.resend_test(a)
                flash(f"Test email sent to {a.email}." if sent
                      else f"Email failed — send the link manually: {url}", "ok" if sent else "error")
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
