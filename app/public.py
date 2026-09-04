"""Public pages: job list, apply form, confirmation, privacy notice."""
import logging
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import pipeline
from .extract import extract_text
from .models import Applicant, Role, db, log_event, utcnow
from .naming import cv_filename
from .storage import get_storage, make_key

bp = Blueprint("public", __name__)
log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_EXTENSIONS"]


def _allowed_attachment(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in current_app.config["ATTACHMENT_EXTENSIONS"]


def _read_attachment(f, errors, what):
    """Validate an optional attachment; returns bytes or None (appending errors)."""
    if not f or not f.filename:
        return None
    if not _allowed_attachment(f.filename):
        errors.append(f"{what}: allowed formats are PDF, Word, PowerPoint, Excel, ZIP or images.")
        return None
    data = f.read()
    if len(data) > current_app.config["PER_FILE_MB"] * 1024 * 1024:
        errors.append(f"{what}: maximum size is {current_app.config['PER_FILE_MB']} MB.")
        return None
    return data


@bp.get("/")
def index():
    return redirect(url_for("public.jobs"))


@bp.get("/jobs")
def jobs():
    roles = Role.query.filter_by(is_open=True).order_by(Role.created_at.desc()).all()
    return render_template("public/jobs.html", roles=roles)


@bp.route("/apply/<slug>", methods=["GET", "POST"])
def apply(slug):
    role = Role.query.filter_by(slug=slug).first_or_404()
    if not role.is_open:
        return render_template("public/closed.html", role=role), 410

    if request.method == "GET":
        return render_template("public/apply.html", role=role, form={}, source=request.args.get("src", "form"))

    form = {k: (v or "").strip() for k, v in request.form.items()}
    errors = []

    if len(form.get("full_name", "")) < 2:
        errors.append("Please enter your full name.")
    if not EMAIL_RE.match(form.get("email", "")):
        errors.append("Please enter a valid email address.")
    if form.get("linkedin_url") and "linkedin.com" not in form["linkedin_url"].lower():
        errors.append("The LinkedIn URL doesn't look right.")
    years = form.get("years_experience", "")
    if not years.isdigit() or int(years) > 60:
        errors.append("Please tell us how many years of experience you have in this field (a whole number).")
    if form.get("consent") != "on":
        errors.append("You need to accept the privacy notice so we can process your application.")

    f = request.files.get("cv")
    if not f or not f.filename:
        errors.append("Please attach your CV.")
    elif not _allowed(f.filename):
        errors.append("CV must be a PDF, DOC or DOCX file.")
    pf = request.files.get("portfolio")
    portfolio_data = _read_attachment(pf, errors, "Portfolio / Projects")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("public/apply.html", role=role, form=form, source=form.get("source", "form")), 400

    # Duplicate guard: same email + role within retention -> update instead of a second row
    existing = Applicant.query.filter_by(role_id=role.id, email=form["email"].lower()).filter(
        Applicant.deleted_at.is_(None)).first()

    data = f.read()
    filename = secure_filename(f.filename)
    source = form.get("source") if form.get("source") in ("form", "linkedin", "referral", "email", "other") else "form"

    applicant = existing or Applicant(role=role)
    applicant.full_name = form["full_name"]
    applicant.email = form["email"].lower()
    applicant.phone = form.get("phone") or None
    applicant.linkedin_url = form.get("linkedin_url") or None
    applicant.location = form.get("location") or None
    applicant.years_experience = int(years)
    applicant.cover_note = form.get("cover_note") or None
    applicant.source = source
    applicant.consent_at = utcnow()
    applicant.set_retention(current_app.config["RETENTION_MONTHS"])
    if not existing:
        db.session.add(applicant)
        db.session.flush()  # get public_id

    storage = get_storage()
    if existing and existing.cv_key:
        try:
            storage.delete(existing.cv_key)
        except Exception:
            pass
    try:
        stored = storage.put(
            make_key(role.slug, applicant.public_id, filename),
            data,
            content_type=f.mimetype,
            display_name=cv_filename(role, applicant.full_name, filename.rsplit(".", 1)[-1]),
            role=role,
        )
    except Exception:
        log.exception("Failed to store CV for %s", applicant.email)
        db.session.rollback()
        flash("We couldn't save your CV. Please try again in a moment.", "error")
        return render_template("public/apply.html", role=role, form=form, source=form.get("source", "form")), 503
    applicant.cv_key = stored["key"]
    applicant.cv_url = stored.get("url")
    applicant.cv_filename = filename
    applicant.cv_mime = f.mimetype
    applicant.cv_size = len(data)
    applicant.cv_text = extract_text(data, filename)

    if portfolio_data is not None:
        if existing and existing.portfolio_key:
            try:
                storage.delete(existing.portfolio_key)
            except Exception:
                pass
        pname = secure_filename(pf.filename)
        try:
            stored_p = storage.put(make_key(role.slug, applicant.public_id + "-portfolio", pname), portfolio_data,
                                   content_type=pf.mimetype,
                                   display_name=cv_filename(role, applicant.full_name, pname.rsplit(".", 1)[-1],
                                                            label="Portfolio"),
                                   role=role)
            applicant.portfolio_key = stored_p["key"]
            applicant.portfolio_url = stored_p.get("url")
            applicant.portfolio_filename = pname
        except Exception:
            log.exception("Failed to store portfolio for %s", applicant.email)  # CV is in; don't fail the application

    log_event(applicant, "applied", "Re-applied: profile and CV updated" if existing else f"Applied via {source}")
    db.session.commit()

    # Auto-summary + ClickUp task/comment run after the response so the candidate isn't kept waiting
    pipeline.process_application(applicant.id, background=current_app.config["PROCESS_ASYNC"])
    return redirect(url_for("public.thanks", public_id=applicant.public_id))


@bp.get("/thanks/<public_id>")
def thanks(public_id):
    applicant = Applicant.query.filter_by(public_id=public_id).first_or_404()
    return render_template("public/thanks.html", applicant=applicant)


@bp.route("/test/<token>", methods=["GET", "POST"])
def test(token):
    """The candidate's online technical test (unique secret link from the invitation email)."""
    applicant = Applicant.query.filter_by(test_token=token).filter(Applicant.deleted_at.is_(None)).first_or_404()
    role = applicant.role
    if not (role.test_questions and role.test_questions.strip()):
        return render_template("public/closed.html", role=role), 410
    if applicant.test_submitted_at:
        return render_template("public/test_done.html", applicant=applicant, already=True)

    if request.method == "POST":
        answers = (request.form.get("answers") or "").strip()
        errors = []
        df = request.files.get("document")
        doc_data = _read_attachment(df, errors, "Returned document")
        if len(answers) < 30 and doc_data is None:
            errors.append("Please write your answers (or attach your returned document) before submitting.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("public/test.html", applicant=applicant, role=role,
                                   answers=answers), 400
        if doc_data is not None:
            dname = secure_filename(df.filename)
            try:
                stored_d = get_storage().put(make_key(role.slug, applicant.public_id + "-test", dname), doc_data,
                                             content_type=df.mimetype,
                                             display_name=cv_filename(role, applicant.full_name,
                                                                      dname.rsplit(".", 1)[-1], label="Test"),
                                             role=role)
                applicant.test_doc_key = stored_d["key"]
                applicant.test_doc_url = stored_d.get("url")
                applicant.test_doc_filename = dname
                doc_text = extract_text(doc_data, dname)
                if doc_text:
                    answers = (answers + "\n\n[Text extracted from the attached document "
                               + dname + "]\n" + doc_text).strip()
            except Exception:
                log.exception("Failed to store returned test document for %s", applicant.email)
                flash("We couldn't save your attached document. Please try again.", "error")
                return render_template("public/test.html", applicant=applicant, role=role, answers=answers), 503
        applicant.test_answers = answers
        applicant.test_submitted_at = utcnow()
        log_event(applicant, "note", "Test answers submitted by the candidate", actor="system")
        db.session.commit()
        pipeline.process_test_submission(applicant.id, background=current_app.config["PROCESS_ASYNC"])
        return render_template("public/test_done.html", applicant=applicant, already=False)

    return render_template("public/test.html", applicant=applicant, role=role, answers="")


@bp.get("/privacy")
def privacy():
    return render_template("public/privacy.html",
                           months=current_app.config["RETENTION_MONTHS"],
                           contact=current_app.config["PRIVACY_CONTACT_EMAIL"])


@bp.get("/health")
def health():
    return {"ok": True}
