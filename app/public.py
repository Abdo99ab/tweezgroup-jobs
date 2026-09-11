"""Public pages: job list, apply form, confirmation, privacy notice."""
import logging
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import pipeline, regions, sourcing
from .extract import extract_text
from .models import FORM_PLATFORMS, SOURCE_LABELS, SOURCES, Applicant, Role, SourcedProfile, db, log_event, utcnow
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


def _sourced(token):
    if not token:
        return None
    return SourcedProfile.query.filter_by(apply_token=token).first()


@bp.get("/r/<token>")
def sourced_link(token):
    """Short personalised link sent to sourced candidates: records the open, pre-fills the form."""
    p = SourcedProfile.query.filter_by(apply_token=token).first_or_404()
    sourcing.on_link_opened(p)
    return redirect(url_for("public.apply", slug=p.role.slug, t=token))


@bp.route("/apply/<slug>", methods=["GET", "POST"])
def apply(slug):
    role = Role.query.filter_by(slug=slug).first_or_404()
    if not role.is_open:
        return render_template("public/closed.html", role=role), 410

    platforms = [(v, SOURCE_LABELS[v]) for v in FORM_PLATFORMS]
    if request.method == "GET":
        sp = _sourced(request.args.get("t"))
        form = {}
        source = request.args.get("src", "form")
        if sp is not None and sp.role_id == role.id:
            if sp.link_opened_at is None:
                sourcing.on_link_opened(sp)
            form = {"full_name": sp.full_name or "", "email": sp.email or "",
                    "linkedin_url": sp.profile_url if sp.profile_url.startswith("http") else "",
                    "location": sp.location or "", "t": sp.apply_token}
            source = sp.channel if sp.channel in SOURCES else "other"
        return render_template("public/apply.html", role=role, form=form, source=source, platforms=platforms,
                               job_ld=job_posting_ld(role))

    form = {k: (v or "").strip() for k, v in request.form.items()}
    errors = []
    sp = _sourced(form.get("t"))
    if sp is not None and sp.role_id != role.id:
        sp = None

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
    # Platform the candidate applied from (required select on the form; legacy values kept for the API)
    source = form.get("source", "").strip().lower()
    if source not in SOURCES:
        errors.append("Please select which platform you applied from.")

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
        return render_template("public/apply.html", role=role, form=form, source=form.get("source", "form"),
                               platforms=platforms), 400

    # Duplicate guard: same email + role within retention -> update instead of a second row
    existing = Applicant.query.filter_by(role_id=role.id, email=form["email"].lower()).filter(
        Applicant.deleted_at.is_(None)).first()

    data = f.read()
    filename = secure_filename(f.filename)

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
        return render_template("public/apply.html", role=role, form=form, source=form.get("source", "form"),
                               platforms=platforms), 503
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
    if sp is not None and sp.applicant_id is None:
        sourcing.on_applied(sp, applicant)
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


# ---------------------------------------------------------------- inbound channels (Google for Jobs, Indeed)

def _plain(text, limit=None):
    """Job description as plain text (no markup) for feeds."""
    import html as _html
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"[ \t]{2,}", " ", _html.unescape(t)).strip()
    return t[:limit] if limit else t


def _iso(dt):
    return dt.strftime("%Y-%m-%d")


def job_posting_ld(role):
    """schema.org/JobPosting for Google for Jobs (rendered as JSON-LD on the apply page)."""
    from datetime import timedelta
    cfg = current_app.config
    base = cfg["PUBLIC_BASE_URL"]
    remote = bool(re.search(r"remote|télétravail|teletravail|anywhere", (role.location or "") + (role.employment_type or ""), re.I)) \
        or regions.is_remote(role.target_regions)
    data = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": role.title,
        "description": _description_html(role),
        "identifier": {"@type": "PropertyValue", "name": cfg["COMPANY_NAME"], "value": role.slug},
        "datePosted": _iso(role.updated_at or role.created_at),
        "validThrough": _iso((role.updated_at or role.created_at) + timedelta(days=60)),
        "employmentType": _employment_type(role.employment_type),
        "hiringOrganization": {"@type": "Organization", "name": cfg["COMPANY_NAME"], "sameAs": base},
        "directApply": True,
        "url": f"{base}/apply/{role.slug}",
    }
    if remote:
        data["jobLocationType"] = "TELECOMMUTE"
        wanted = regions.countries(role.target_regions) or _countries(role.location) or ["France"]
        data["applicantLocationRequirements"] = [{"@type": "Country", "name": c} for c in wanted[:20]]
    else:
        data["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress",
                                                             "addressLocality": (role.location or "").split("/")[0].strip() or "Paris",
                                                             "addressCountry": "FR"}}
    return data


def _description_html(role):
    from .textfmt import description_html
    try:
        return str(description_html(role.description or ""))
    except Exception:
        return _plain(role.description)


def _employment_type(text):
    t = (text or "").lower()
    if "intern" in t or "stage" in t:
        return "INTERN"
    if "part" in t or "partiel" in t:
        return "PART_TIME"
    if "contract" in t or "freelance" in t or "cdd" in t:
        return "CONTRACTOR"
    return "FULL_TIME"


def _countries(location):
    names = {"france": "France", "algeria": "Algeria", "algérie": "Algeria", "algerie": "Algeria", "spain": "Spain",
             "morocco": "Morocco", "tunisia": "Tunisia", "estonia": "Estonia", "united states": "United States",
             "usa": "United States", "uk": "United Kingdom", "europe": None}
    out = []
    for k, v in names.items():
        if k in (location or "").lower() and v:
            out.append(v)
    return out


@bp.get("/sitemap.xml")
def sitemap():
    base = current_app.config["PUBLIC_BASE_URL"]
    roles = Role.query.filter_by(is_open=True).all()
    items = [f"<url><loc>{base}/jobs</loc><changefreq>daily</changefreq></url>"]
    for r in roles:
        items.append(f"<url><loc>{base}/apply/{r.slug}</loc><lastmod>{_iso(r.updated_at or r.created_at)}</lastmod>"
                     f"<changefreq>weekly</changefreq></url>")
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(items) + "</urlset>")
    return current_app.response_class(body, mimetype="application/xml")


@bp.get("/jobs/feed.xml")
def indeed_feed():
    """Indeed XML job feed (also accepted by most aggregators). Register the URL once in the Indeed employer account."""
    from xml.sax.saxutils import escape
    cfg = current_app.config
    base = cfg["PUBLIC_BASE_URL"]
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<source>", f"<publisher>{escape(cfg['COMPANY_NAME'])}</publisher>",
             f"<publisherurl>{escape(base)}</publisherurl>",
             f"<lastBuildDate>{utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>"]
    for r in Role.query.filter_by(is_open=True).order_by(Role.created_at.desc()):
        loc = (r.location or "Remote").split("/")[0].strip()
        parts.append("<job>")
        parts.append(f"<title><![CDATA[{r.title}]]></title>")
        parts.append(f"<date><![CDATA[{(r.updated_at or r.created_at).strftime('%a, %d %b %Y %H:%M:%S GMT')}]]></date>")
        parts.append(f"<referencenumber><![CDATA[{r.slug}]]></referencenumber>")
        parts.append(f"<url><![CDATA[{base}/apply/{r.slug}?src=indeed]]></url>")
        parts.append(f"<company><![CDATA[{cfg['COMPANY_NAME']}]]></company>")
        parts.append(f"<city><![CDATA[{loc}]]></city>")
        parts.append("<country><![CDATA[FR]]></country>")
        parts.append(f"<description><![CDATA[{_description_html(r)}]]></description>")
        parts.append(f"<jobtype><![CDATA[{r.employment_type or 'Full-time'}]]></jobtype>")
        parts.append(f"<category><![CDATA[{r.department or ''}]]></category>")
        parts.append("<remotetype><![CDATA[Fully remote]]></remotetype>" if re.search(r"remote", r.location or "", re.I) else "")
        parts.append("</job>")
    parts.append("</source>")
    return current_app.response_class("\n".join(p for p in parts if p), mimetype="application/xml")
