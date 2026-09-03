import logging

import click
from dotenv import load_dotenv
from flask import Flask, request, session

load_dotenv()

from .config import Config  # noqa: E402
from .models import db, Role, Applicant, utcnow, log_event, STATUS_LABELS  # noqa: E402


def create_app(config_object=Config):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config_object)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_app(app)

    from .public import bp as public_bp
    from .admin import bp as admin_bp
    from .api import bp as api_bp
    from .webhooks import bp as webhooks_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(webhooks_bp, url_prefix="/webhooks")

    @app.context_processor
    def inject_globals():
        return {"company": app.config["COMPANY_NAME"], "labels": STATUS_LABELS,
                "embed": session.get("embed", False)}

    @app.before_request
    def remember_embed():
        # /apply/<role>?embed=1 -> compact layout (no header/footer) for iframes on company sites.
        # Remembered in the session so the POST + confirmation page stay compact too.
        if request.args.get("embed") == "1":
            session["embed"] = True
        elif request.args.get("embed") == "0":
            session.pop("embed", None)

    @app.after_request
    def frame_policy(resp):
        ancestors = app.config["FRAME_ANCESTORS"].strip()
        if request.blueprint == "public" and ancestors:
            resp.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {ancestors}"
        elif request.blueprint != "public" or not ancestors:
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    @app.errorhandler(413)
    def too_large(_):
        mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return f"File too large. Maximum size is {mb} MB.", 413

    with app.app_context():
        db.create_all()
        _auto_migrate()

    register_cli(app)
    return app


def _auto_migrate():
    """Additive migrations: add any model column missing from an existing table (SQLite/Postgres/MySQL).
    Safe to run on every startup; never drops or alters existing columns."""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ctype = col.type.compile(db.engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'))
                logging.getLogger(__name__).info("auto-migrate: added %s.%s %s", table.name, col.name, ctype)


def register_cli(app):
    @app.cli.command("seed")
    def seed():
        """Create a sample role so the apply page works immediately."""
        if Role.query.filter_by(slug="head-of-marketing").first():
            click.echo("Sample role already exists.")
            return
        role = Role(
            slug="head-of-marketing",
            title="Head of Marketing",
            code="HM",
            drive_folder_id="1q5duBJpVTuDR2OFCBVyBifQ6jX77XPuz",
            department="Marketing",
            location="France / Algiers (hybrid)",
            employment_type="Full-time",
            description=(
                "Lead marketing across our e-commerce brands (bedding, cosmetics, furniture). "
                "Own paid acquisition, CRM/email, content and brand across FR/EU and US markets."
            ),
            requirements=(
                "MUST HAVE\n"
                "- 5+ years in DTC / e-commerce marketing, at least 2 leading a team\n"
                "- Hands-on with Meta Ads and Google Ads at meaningful budgets\n"
                "- Fluent French and English\n"
                "- Based in France or Algeria, or able to work on CET hours\n\n"
                "NICE TO HAVE\n"
                "- Shopify + Klaviyo experience\n"
                "- Amazon / marketplace experience\n"
                "- Arabic\n\n"
                "RED FLAGS\n"
                "- Agency-only background with no in-house P&L ownership\n"
                "- No measurable results on the CV"
            ),
        )
        db.session.add(role)
        db.session.commit()
        click.echo(f"Created role '{role.title}' -> /apply/{role.slug}")

    @app.cli.command("seed-roles")
    def seed_roles():
        """Create one role per existing TWEEZ-CV-BANK subfolder (closed by default) with its code + pinned folder."""
        from .naming import KNOWN_FOLDERS, code_for_title
        from .models import slugify
        titles = {  # folder name -> role title as it should appear to candidates
            "Head of Marketing CVs": "Head of Marketing", "B2B Prospector": "B2B Prospector",
            "TikTok Shop Manager CV's": "TikTok Shop Manager", "B2B Manager CVs": "B2B Manager",
            "Sourcing Manager CVs": "Sourcing Manager", "Bookkeeper CVs": "Bookkeeper",
            "Chief Opertation Officer (COO) Cvs": "Chief Operating Officer (COO)",
            "Graphic Designers CVs": "Graphic Designer", "TikTok AI Video Creators": "TikTok AI Video Creator",
            "Amazon Account Manager": "Amazon Account Manager", "TikTok LiveShopping": "TikTok Live Shopping Host",
            "Data Scientists CVs": "Data Scientist", "Customer Support CVs": "Customer Support",
            "Brand Manager CVs": "Brand Manager", "Head Acountant CVs": "Head of Accounting",
        }
        created = 0
        for folder_id, folder_name in KNOWN_FOLDERS.items():
            title = titles.get(folder_name, folder_name)
            slug = slugify(title)
            if Role.query.filter_by(slug=slug).first():
                continue
            db.session.add(Role(slug=slug, title=title, code=code_for_title(title), drive_folder_id=folder_id,
                                is_open=False))
            created += 1
        if not Role.query.filter_by(slug="content-creator").first():
            db.session.add(Role(slug="content-creator", title="Content Creator", code="CC", is_open=False))
            created += 1
        db.session.commit()
        click.echo(f"{created} role(s) created (closed). Open the ones you are hiring for in /admin.")
        for r in Role.query.order_by(Role.title):
            click.echo(f"  {r.code:5s} {r.title:35s} /apply/{r.slug}  folder={r.drive_folder_id or 'auto'}")

    @app.cli.command("clickup-webhook-setup")
    def clickup_webhook_setup():
        """Register the ClickUp -> app webhook (two-way status sync). Run once after deployment;
        copy the printed secret into the CLICKUP_WEBHOOK_SECRET environment variable."""
        import requests as rq
        cfg = app.config
        endpoint = f"{cfg['PUBLIC_BASE_URL']}/webhooks/clickup"
        api = __import__("os").environ.get("CLICKUP_API_BASE", "https://api.clickup.com/api/v2")
        headers = {"Authorization": cfg["CLICKUP_API_TOKEN"], "Content-Type": "application/json"}
        r = rq.get(f"{api}/team/{cfg['CLICKUP_TEAM_ID']}/webhook", headers=headers, timeout=15)
        r.raise_for_status()
        for wh in r.json().get("webhooks", []):
            if wh.get("endpoint") == endpoint:
                click.echo(f"Webhook already registered: {wh['id']} -> {endpoint}")
                click.echo(f"CLICKUP_WEBHOOK_SECRET={wh.get('secret', '(not returned — delete and re-run to get a new one)')}")
                return
        r = rq.post(f"{api}/team/{cfg['CLICKUP_TEAM_ID']}/webhook",
                    json={"endpoint": endpoint, "events": ["taskStatusUpdated"]}, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        wh = data.get("webhook", data)
        click.echo(f"Webhook registered: id={data.get('id') or wh.get('id')} -> {endpoint}")
        click.echo("Add this to the environment and redeploy:")
        click.echo(f"CLICKUP_WEBHOOK_SECRET={wh.get('secret', '')}")

    @app.cli.command("sync-codes")
    def sync_codes():
        """Update every role's code to the agreed list (DS, VC, PROS, ...) based on its title.
        Run once after deploying a code-mapping change; roles with a hand-edited code that
        already matches are left alone."""
        from .naming import code_for_title
        changed = 0
        for r in Role.query.order_by(Role.title):
            want = code_for_title(r.title)
            if r.code != want:
                click.echo(f"  {r.title}: {r.code or '-'} -> {want}")
                r.code = want
                changed += 1
        db.session.commit()
        click.echo(f"{changed} role code(s) updated." if changed else "All role codes already correct.")

    @app.cli.command("purge-expired")
    @click.option("--dry-run", is_flag=True, help="List what would be deleted without deleting.")
    def purge_expired(dry_run):
        """GDPR retention: delete applicants (and their CV files) past retention_until."""
        from .storage import get_storage

        now = utcnow()
        q = Applicant.query.filter(Applicant.retention_until <= now, Applicant.deleted_at.is_(None))
        rows = q.all()
        click.echo(f"{len(rows)} applicant(s) past retention.")
        if dry_run:
            for a in rows:
                click.echo(f"  would delete {a.public_id} {a.email} (retention_until {a.retention_until:%Y-%m-%d})")
            return
        storage = get_storage()
        for a in rows:
            purge_applicant(a, storage)
        db.session.commit()
        click.echo("Done.")

    @app.cli.command("process-pending")
    def process_pending():
        """Retry the auto-summary / ClickUp task / summary comment for applicants where it is missing."""
        from .pipeline import pending_query, process_application, process_test_submission
        rows = pending_query().all()
        click.echo(f"{len(rows)} applicant(s) pending.")
        for a in rows:
            aid, label = a.id, f"{a.public_id} {a.full_name}"
            done = process_application(aid, background=False)
            a = db.session.get(Applicant, aid)
            if a and a.test_submitted_at and a.test_evaluation is None:
                done += process_test_submission(aid, background=False)
            click.echo(f"  {label}: {', '.join(done) or 'nothing changed'}")

    @app.cli.command("rotate-keys")
    def rotate_keys():
        """Print fresh random values for SECRET_KEY and API_KEY."""
        import secrets
        click.echo(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        click.echo(f"API_KEY={secrets.token_urlsafe(32)}")


def purge_applicant(a, storage):
    """Hard-anonymise an applicant: remove file and personal data, keep an anonymous row for stats."""
    if a.cv_key:
        try:
            storage.delete(a.cv_key)
        except Exception:
            pass
    a.full_name = "[deleted]"
    a.email = f"deleted-{a.public_id}@invalid"
    a.phone = None
    a.linkedin_url = None
    a.location = None
    a.cover_note = None
    a.cv_key = None
    a.cv_filename = None
    a.cv_text = None
    a.ai_summary = None
    a.notes = None
    a.deleted_at = utcnow()
    log_event(a, "deleted", "Personal data purged (retention expired or deletion requested)")
