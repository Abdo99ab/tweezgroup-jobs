import re
import secrets
from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Internal status slugs. Everything after "new" mirrors the ClickUp recruiting list 1:1
# (see clickup.STATUS_MAP). "new" = applied but not yet screened; it has no ClickUp column.
STATUSES = [
    "new",               # applied, not yet screened by the agent / recruiter
    "filtered",          # FILTRED APPLICATION  – passed screening
    "selected",          # SELECTED/ IN PROGRESS
    "test_sent",         # TEST SENT
    "test_returned",     # TEST RETURNED
    "interview_done",    # INTERVIEW DONE
    "interview2_done",   # 2ND INTERVIEW DONE
    "contract_sent",     # CONTRACT SENT
    "rejected",          # REJECTED
    "hired",             # HIRED
]

STATUS_LABELS = {
    "new": "New",
    "filtered": "Filtered application",
    "selected": "Selected / in progress",
    "test_sent": "Test sent",
    "test_returned": "Test returned",
    "interview_done": "Interview done",
    "interview2_done": "2nd interview done",
    "contract_sent": "Contract sent",
    "rejected": "Rejected",
    "hired": "Hired",
}

SOURCES = ["form", "linkedin", "referral", "email", "other"]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text or "role"


def public_id():
    return secrets.token_urlsafe(9)


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    code = db.Column(db.String(8))              # short code used in CV file names, e.g. HM -> HM31082026
    department = db.Column(db.String(120))
    location = db.Column(db.String(200))
    employment_type = db.Column(db.String(60))  # full-time, contract, internship...
    description = db.Column(db.Text)            # public job description (markdown/plain)
    requirements = db.Column(db.Text)           # KNOWLEDGE BASE: what we look for; the agent screens against this
    is_open = db.Column(db.Boolean, default=True, nullable=False)
    clickup_list_id = db.Column(db.String(60))  # overrides the default pipeline list
    drive_folder_id = db.Column(db.String(120))  # Google Drive subfolder for this role's CVs (auto-created if empty)
    test_questions = db.Column(db.Text)          # written technical test sent to candidates scoring > SELECT_ABOVE
    test_answer_key = db.Column(db.Text)         # private answer key Claude grades against (never shown to candidates)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    applicants = db.relationship("Applicant", backref="role", lazy="dynamic")

    def to_dict(self, include_counts=False):
        d = {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "code": self.code,
            "department": self.department,
            "location": self.location,
            "employment_type": self.employment_type,
            "description": self.description,
            "requirements": self.requirements,
            "is_open": self.is_open,
            "clickup_list_id": self.clickup_list_id,
            "drive_folder_id": self.drive_folder_id,
            "has_test": bool(self.test_questions and self.test_questions.strip()),
            "test_questions": self.test_questions,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
        }
        if include_counts:
            d["applicant_count"] = self.applicants.filter(Applicant.deleted_at.is_(None)).count()
        return d


class Applicant(db.Model):
    __tablename__ = "applicants"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(24), unique=True, default=public_id, nullable=False, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False, index=True)

    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False, index=True)
    phone = db.Column(db.String(60))
    linkedin_url = db.Column(db.String(300))
    location = db.Column(db.String(200))
    years_experience = db.Column(db.Integer)   # years of experience in this field (asked on the form)
    cover_note = db.Column(db.Text)
    source = db.Column(db.String(30), default="form", nullable=False)

    cv_key = db.Column(db.String(400))       # storage key (Google Drive file id when STORAGE_BACKEND=gdrive)
    cv_url = db.Column(db.String(400))       # human link to the file (Drive "open" link)
    cv_filename = db.Column(db.String(300))
    cv_mime = db.Column(db.String(100))
    cv_size = db.Column(db.Integer)
    cv_text = db.Column(db.Text)             # extracted text for the agent

    status = db.Column(db.String(30), default="new", nullable=False, index=True)
    score = db.Column(db.Integer)            # 0-100, set by the agent
    ai_summary = db.Column(db.Text)          # agent's screening summary
    notes = db.Column(db.Text)               # recruiter notes
    clickup_task_id = db.Column(db.String(60), index=True)
    clickup_task_url = db.Column(db.String(300))

    test_token = db.Column(db.String(48), unique=True)   # secret link for the candidate's online test
    test_sent_at = db.Column(db.DateTime)
    test_submitted_at = db.Column(db.DateTime)
    test_answers = db.Column(db.Text)
    test_score = db.Column(db.Integer)                   # 0-100, graded by Claude against the answer key
    test_evaluation = db.Column(db.Text)                 # per-question feedback

    consent_at = db.Column(db.DateTime)
    retention_until = db.Column(db.DateTime, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    events = db.relationship("Event", backref="applicant", lazy="dynamic",
                             cascade="all, delete-orphan", order_by="Event.created_at.desc()")

    def set_retention(self, months):
        self.retention_until = utcnow() + timedelta(days=round(months * 365.25 / 12))

    def to_dict(self, include_cv_text=False, include_events=False):
        d = {
            "id": self.public_id,
            "role": {"id": self.role.id, "slug": self.role.slug, "title": self.role.title},
            "full_name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "linkedin_url": self.linkedin_url,
            "location": self.location,
            "years_experience": self.years_experience,
            "cover_note": self.cover_note,
            "source": self.source,
            "cv": {
                "filename": self.cv_filename,
                "mime": self.cv_mime,
                "size": self.cv_size,
                "url": self.cv_url,
                "has_text": bool(self.cv_text),
            } if self.cv_key else None,
            "status": self.status,
            "score": self.score,
            "ai_summary": self.ai_summary,
            "notes": self.notes,
            "clickup_task_id": self.clickup_task_id,
            "clickup_task_url": self.clickup_task_url,
            "test": {
                "sent_at": self.test_sent_at.isoformat() + "Z" if self.test_sent_at else None,
                "submitted_at": self.test_submitted_at.isoformat() + "Z" if self.test_submitted_at else None,
                "score": self.test_score,
            } if self.test_sent_at else None,
            "consent_at": self.consent_at.isoformat() + "Z" if self.consent_at else None,
            "retention_until": self.retention_until.isoformat() + "Z" if self.retention_until else None,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
        }
        if include_cv_text:
            d["cv_text"] = self.cv_text
        if include_events:
            d["events"] = [e.to_dict() for e in self.events]
        return d


class Setting(db.Model):
    """Small key/value store for state the app manages itself (e.g. the ClickUp webhook secret)."""
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row else default

    @staticmethod
    def put(key, value):
        row = db.session.get(Setting, key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value
        db.session.commit()


class Event(db.Model):
    """Audit trail per applicant: status changes, ClickUp sync, agent actions, emails sent."""
    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicants.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)   # applied, status_changed, clickup_synced, scored, note, email_sent, error
    actor = db.Column(db.String(60), default="system")  # system, admin, agent
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self):
        return {
            "kind": self.kind,
            "actor": self.actor,
            "message": self.message,
            "created_at": self.created_at.isoformat() + "Z",
        }


def log_event(applicant, kind, message=None, actor="system"):
    ev = Event(applicant=applicant, kind=kind, message=message, actor=actor)
    db.session.add(ev)
    return ev
