import os
from pathlib import Path

from sqlalchemy.pool import NullPool

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env(name, default=""):
    v = os.environ.get(name, default)
    return (default if v is None else str(v)).strip()


def _clickup_token():
    """Strip leftover .env.example placeholder if it was concatenated onto a real token."""
    token = _env("CLICKUP_API_TOKEN")
    while token.startswith("pk_xxx"):
        token = token[len("pk_xxx"):]
    return token


class Config:
    # --- core ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Tweezgroup")
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    PRIVACY_CONTACT_EMAIL = os.environ.get("PRIVACY_CONTACT_EMAIL", "hr@tweezgroup.com")

    # --- database (SQLite by default; MySQL: mysql+pymysql://user:pass@host/db) ---
    _db = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'applicants.db'}")
    # Accept any Supabase/Neon/Heroku-style URL and route it to the psycopg 3 driver
    for _prefix in ("postgres://", "postgresql://"):
        if _db.startswith(_prefix):
            _db = "postgresql+psycopg://" + _db[len(_prefix):]
            break
    SQLALCHEMY_DATABASE_URI = _db
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Supabase/Neon transaction poolers (PgBouncer) break psycopg3 prepared statements.
    # NullPool + prepare_threshold=None is the supported combination.
    if _db.startswith("postgresql"):
        SQLALCHEMY_ENGINE_OPTIONS = {
            "poolclass": NullPool,
            "connect_args": {"prepare_threshold": None},
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}

    # --- auth ---
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
    API_KEY = os.environ.get("API_KEY", "dev-api-key")

    # --- uploads ---
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
    ALLOWED_EXTENSIONS = {"pdf", "doc", "docx"}

    # --- storage: "gdrive" (TWEEZ-CV-BANK on Google Drive), "s3" (Supabase/Spaces/R2/AWS) or "local" ---
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")

    # Google Drive (STORAGE_BACKEND=gdrive)
    GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "13CFAOjtqDm8KotvrTo__ujWkYhdYM-zI")  # TWEEZ-CV-BANK
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # JSON text or file path
    GOOGLE_IMPERSONATE_USER = os.environ.get("GOOGLE_IMPERSONATE_USER", "")          # e.g. mehdi@tweezgroup.com
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
    LOCAL_UPLOAD_DIR = Path(os.environ.get("LOCAL_UPLOAD_DIR", DATA_DIR / "uploads"))
    S3_BUCKET = os.environ.get("S3_BUCKET", "")
    S3_REGION = os.environ.get("S3_REGION", "fra1")
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")  # e.g. https://fra1.digitaloceanspaces.com
    S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
    S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
    S3_PREFIX = os.environ.get("S3_PREFIX", "cvs")
    S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "path")  # path: Supabase/MinIO; virtual: AWS/Spaces
    S3_ACL = os.environ.get("S3_ACL", "")  # e.g. "private" on AWS/Spaces; leave empty for Supabase

    # --- embedding on company websites (iframe) ---
    # space-separated origins allowed to frame the apply page, e.g. "https://zenpur.fr https://www.nubiana.com"
    FRAME_ANCESTORS = os.environ.get("FRAME_ANCESTORS", "")

    # --- ClickUp ---
    CLICKUP_API_TOKEN = _clickup_token()
    CLICKUP_LIST_ID = _env("CLICKUP_LIST_ID")  # default pipeline list
    CLICKUP_ENABLED = _bool("CLICKUP_ENABLED", bool(os.environ.get("CLICKUP_API_TOKEN")))
    # "filtered": task created when the applicant passes screening (matches the board, whose first
    # column is FILTRED APPLICATION). "apply": task created for every application immediately.
    CLICKUP_CREATE_ON = os.environ.get("CLICKUP_CREATE_ON", "apply")
    CLICKUP_TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "9015802184")  # workspace id (from any ClickUp URL)
    # People, by ClickUp display name or email; resolved to user ids at runtime via GET /team
    CLICKUP_ASSIGNEE = os.environ.get("CLICKUP_ASSIGNEE", "Mehdi Mahcene")
    CLICKUP_MENTIONS = os.environ.get("CLICKUP_MENTIONS", "Ahmidou, Taoufik Mousselmal, Abderrahmane Hammia")

    # --- CV auto-summary (Claude) ---
    ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
    ANTHROPIC_API_BASE = _env("ANTHROPIC_API_BASE", "https://api.anthropic.com")
    SUMMARY_MODEL = _env("SUMMARY_MODEL", "claude-haiku-4-5")
    SUMMARY_ENABLED = _bool("SUMMARY_ENABLED", bool(os.environ.get("ANTHROPIC_API_KEY")))
    PROCESS_ASYNC = _bool("PROCESS_ASYNC", True)  # run summary + ClickUp in a background thread after apply

    # --- automatic status decisions from the CV score ---
    AUTO_STATUS_ENABLED = _bool("AUTO_STATUS_ENABLED", True)
    REJECT_BELOW = int(os.environ.get("REJECT_BELOW", "50"))   # score <  50            -> rejected
    SELECT_ABOVE = int(os.environ.get("SELECT_ABOVE", "60"))   # 50 <= score <= 60      -> filtered
                                                               # score >  60            -> selected (or test_sent if the role has a test)

    # --- candidate email (test invitations) ---
    # smtp: MAIL_USERNAME + MAIL_PASSWORD (app password) | gmail_api: reuses the GOOGLE_OAUTH_* Drive
    # credentials (re-run scripts/gdrive_auth.py once so the token also covers gmail.send) | log | off
    MAIL_BACKEND = os.environ.get(
        "MAIL_BACKEND",
        "smtp" if os.environ.get("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD")
        else ("gmail_api" if os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN") else "off"))
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USERNAME = _env("MAIL_USERNAME")            # e.g. hr@tweezgroup.com
    MAIL_PASSWORD = _env("MAIL_PASSWORD")            # Gmail App Password (Google Account -> Security -> App passwords)
    MAIL_FROM = _env("MAIL_FROM")                    # defaults to MAIL_USERNAME

    # --- ClickUp -> app two-way sync ---
    CLICKUP_WEBHOOK_SECRET = _env("CLICKUP_WEBHOOK_SECRET")        # optional; auto-registered secret is kept in the DB
    CLICKUP_WEBHOOK_AUTOREGISTER = _bool("CLICKUP_WEBHOOK_AUTOREGISTER", True)  # register the webhook at startup
    CLICKUP_POLL_MINUTES = int(os.environ.get("CLICKUP_POLL_MINUTES", "10"))    # 0 disables the polling fallback

    # --- GDPR ---
    RETENTION_MONTHS = int(os.environ.get("RETENTION_MONTHS", "12"))
