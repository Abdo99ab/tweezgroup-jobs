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

SOURCES = ["form", "linkedin", "torre", "github", "facebook", "slack", "referral", "email",
           "indeed", "wttj", "google_jobs", "jobboard", "behance", "artstation", "kaggle", "contra", "other"]

SOURCE_LABELS = {
    "form": "Form", "linkedin": "LinkedIn", "torre": "Torre", "github": "GitHub", "facebook": "Facebook",
    "slack": "Slack", "referral": "Referral", "email": "Email", "indeed": "Indeed",
    "wttj": "Welcome to the Jungle", "google_jobs": "Google Jobs", "jobboard": "Other job board",
    "behance": "Behance", "artstation": "ArtStation", "kaggle": "Kaggle", "contra": "Contra", "other": "Other",
}

# Platforms offered on the public apply form ("Which platform did you apply from?")
FORM_PLATFORMS = ["linkedin", "indeed", "wttj", "google_jobs", "torre", "github", "behance", "artstation",
                  "kaggle", "contra", "facebook", "slack", "referral", "jobboard", "other"]

# Sourcing (Milestone 1): where a sourced profile came from. "linkedin" is worked by the browser
# worker; "github" is searched through the official API; the others share the same
# shortlist -> outreach -> link flow but people are found/imported by URL and messaged by hand or email.
CHANNELS = ["linkedin", "github", "behance", "artstation", "kaggle", "contra", "torre", "apollo", "upwork",
            "referral", "manual"]
CHANNEL_LABELS = {"linkedin": "LinkedIn", "github": "GitHub", "behance": "Behance", "artstation": "ArtStation",
                  "kaggle": "Kaggle", "contra": "Contra", "torre": "Torre", "apollo": "Apollo", "upwork": "Upwork",
                  "referral": "Referral", "manual": "Manual"}
# How each channel finds people: worker (browser), api (app runs the search itself) or import (paste URLs)
CHANNEL_SEARCH = {"linkedin": "worker", "github": "api", "behance": "import", "artstation": "import",
                  "kaggle": "import", "contra": "import", "torre": "import", "apollo": "import", "upwork": "import",
                  "referral": "import", "manual": "import"}
DECISIONS = ["pending", "approved", "rejected"]
# Outreach steps for a profile, in order. next_action names the step the worker must do next.
ACTIONS = ["connect", "check_accept", "message", "reminder"]


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
    # --- sourcing / outreach (Milestone 1). Placeholders: {first_name} {role} {company} {apply_url} {icebreaker}
    msg_connection = db.Column(db.Text)          # LinkedIn connection note (<= 300 chars after rendering)
    msg_message = db.Column(db.Text)             # message sent once connected, carries {apply_url}
    msg_reminder = db.Column(db.Text)            # one reminder if the link is not opened
    msg_referral = db.Column(db.Text)            # email sent to a referred candidate
    sourcing_autosend = db.Column(db.Boolean, default=False, nullable=False)  # False = human confirms each send
    sourcing_brief = db.Column(db.Text)          # optional: hand-written search brief (titles, keywords, locations)
    target_regions = db.Column(db.String(400))   # default regions for searches, comma-separated keys (see regions.py)
    target_custom = db.Column(db.String(400))    # extra free-text locations, comma-separated
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
            "sourcing_autosend": bool(self.sourcing_autosend),
            "sourcing_brief": self.sourcing_brief,
            "target_regions": self.target_regions,
            "target_custom": self.target_custom,
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

    portfolio_key = db.Column(db.String(400))     # optional Portfolio / Projects attachment
    portfolio_url = db.Column(db.String(400))
    portfolio_filename = db.Column(db.String(300))

    status = db.Column(db.String(30), default="new", nullable=False, index=True)
    score = db.Column(db.Integer)            # 0-100, set by the agent
    ai_summary = db.Column(db.Text)          # agent's screening summary
    notes = db.Column(db.Text)               # recruiter notes
    clickup_task_id = db.Column(db.String(60), index=True)
    clickup_task_url = db.Column(db.String(300))

    test_token = db.Column(db.String(48), unique=True)   # secret link for the candidate's online test
    test_due_at = db.Column(db.DateTime)                 # scheduled send time (TEST_SEND_DELAY_MINUTES after selection)
    test_sent_at = db.Column(db.DateTime)
    test_submitted_at = db.Column(db.DateTime)
    test_answers = db.Column(db.Text)
    test_doc_key = db.Column(db.String(400))      # optional "returned document" uploaded with the test
    test_doc_url = db.Column(db.String(400))
    test_doc_filename = db.Column(db.String(300))
    test_score = db.Column(db.Integer)                   # 0-100, graded by Claude against the answer key
    test_evaluation = db.Column(db.Text)                 # per-question feedback

    referred_by = db.Column(db.String(200))   # team member who referred the candidate (sourcing channel "referral")

    consent_at = db.Column(db.DateTime)
    retention_until = db.Column(db.DateTime, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    events = db.relationship("Event", backref="applicant", lazy="dynamic",
                             cascade="all, delete-orphan", order_by="Event.created_at.desc()")
    sourced_profile = db.relationship("SourcedProfile", backref="applicant", uselist=False)

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
            "portfolio": {"filename": self.portfolio_filename, "url": self.portfolio_url}
            if self.portfolio_key else None,
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
                "returned_document": {"filename": self.test_doc_filename, "url": self.test_doc_url}
                if self.test_doc_key else None,
            } if self.test_sent_at else None,
            "referred_by": self.referred_by,
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


# ====================================================================================== sourcing (M1)

def apply_token():
    return secrets.token_urlsafe(12)


class SourcingSearch(db.Model):
    """One search the worker runs on a channel (LinkedIn people search, Torre, ...)."""
    __tablename__ = "sourcing_searches"

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False, index=True)
    channel = db.Column(db.String(20), default="linkedin", nullable=False)
    keywords = db.Column("query", db.String(400), nullable=False)  # what is typed into the search box (JSON key "query")
    location = db.Column(db.String(200))                   # location filter, free text (one per search)
    region = db.Column(db.String(40))                      # preset key the location came from (regions.py), if any
    max_results = db.Column(db.Integer, default=50, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)  # pending, running, done, error
    results_count = db.Column(db.Integer, default=0, nullable=False)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    role = db.relationship("Role", backref=db.backref("searches", lazy="dynamic"))

    def to_dict(self):
        return {
            "id": self.id, "role": self.role.slug, "role_title": self.role.title, "channel": self.channel,
            "query": self.keywords, "location": self.location, "region": self.region, "max_results": self.max_results,
            "status": self.status, "results_count": self.results_count, "error": self.error,
            "created_at": self.created_at.isoformat() + "Z",
        }


class SourcedProfile(db.Model):
    """A person found by a search (or referred). Lives before the Applicant record: it becomes one
    when the candidate uploads a CV through their personalised apply link."""
    __tablename__ = "sourced_profiles"
    __table_args__ = (db.UniqueConstraint("role_id", "profile_url", name="uq_profile_role_url"),)

    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False, index=True)
    search_id = db.Column(db.Integer, db.ForeignKey("sourcing_searches.id"), index=True)
    channel = db.Column(db.String(20), default="linkedin", nullable=False, index=True)
    profile_url = db.Column(db.String(400), nullable=False)
    full_name = db.Column(db.String(200), nullable=False)
    headline = db.Column(db.String(400))
    location = db.Column(db.String(200))
    company = db.Column(db.String(200))
    about = db.Column(db.Text)                 # extra scraped text (about section / experience snippets)
    email = db.Column(db.String(200))          # known for referrals / API channels
    referred_by = db.Column(db.String(200))

    pre_score = db.Column(db.Integer)          # Claude's fit estimate from the public profile, 0-100
    pre_reason = db.Column(db.Text)
    icebreaker = db.Column(db.String(300))     # one personal line Claude wrote for the message
    decision = db.Column(db.String(12), default="pending", nullable=False, index=True)  # pending/approved/rejected
    decided_at = db.Column(db.DateTime)

    # outreach state machine
    next_action = db.Column(db.String(20), index=True)   # connect / check_accept / message / reminder / None
    next_due_at = db.Column(db.DateTime)
    released = db.Column(db.Boolean, default=False, nullable=False)  # human clicked Send (or role autosend)
    draft_note = db.Column(db.String(400))     # connection note as it will be sent
    draft_message = db.Column(db.Text)         # message with the apply link
    draft_reminder = db.Column(db.Text)
    connect_sent_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    message_sent_at = db.Column(db.DateTime)
    reminder_sent_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)         # outreach finished (applied, expired, declined, rejected)
    close_reason = db.Column(db.String(60))
    reply_text = db.Column(db.Text)
    reply_class = db.Column(db.String(20))     # interested / question / not_now / negative
    reply_at = db.Column(db.DateTime)
    lock_at = db.Column(db.DateTime)           # claimed by a worker (avoid double execution)

    apply_token = db.Column(db.String(32), unique=True, default=apply_token, nullable=False)
    link_opened_at = db.Column(db.DateTime)
    applicant_id = db.Column(db.Integer, db.ForeignKey("applicants.id"), index=True)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    role = db.relationship("Role", backref=db.backref("sourced_profiles", lazy="dynamic"))
    search = db.relationship("SourcingSearch", backref=db.backref("profiles", lazy="dynamic"))
    actions = db.relationship("SourcingAction", backref="profile", lazy="dynamic",
                              cascade="all, delete-orphan", order_by="SourcingAction.created_at.desc()")

    @property
    def first_name(self):
        return (self.full_name or "").strip().split(" ")[0]

    @property
    def stage(self):
        """Human-readable funnel stage."""
        if self.applicant_id:
            return "applied"
        if self.link_opened_at:
            return "link opened"
        if self.message_sent_at:
            return "link sent"
        if self.accepted_at:
            return "accepted"
        if self.connect_sent_at:
            return "contacted"
        if self.decision == "approved":
            return "approved"
        return self.decision

    def to_dict(self, include_drafts=False):
        d = {
            "id": self.id, "role": self.role.slug, "channel": self.channel, "profile_url": self.profile_url,
            "full_name": self.full_name, "headline": self.headline, "location": self.location,
            "company": self.company, "email": self.email, "pre_score": self.pre_score, "pre_reason": self.pre_reason,
            "decision": self.decision, "stage": self.stage, "next_action": self.next_action,
            "next_due_at": self.next_due_at.isoformat() + "Z" if self.next_due_at else None,
            "released": self.released, "reply_class": self.reply_class,
            "applicant_id": self.applicant.public_id if self.applicant_id and self.applicant else None,
            "created_at": self.created_at.isoformat() + "Z",
        }
        if include_drafts:
            d.update(draft_note=self.draft_note, draft_message=self.draft_message, draft_reminder=self.draft_reminder,
                     icebreaker=self.icebreaker)
        return d


class SourcingAction(db.Model):
    """Every browser action the worker performed (or failed) — the audit trail and the daily-cap counter."""
    __tablename__ = "sourcing_actions"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("sourced_profiles.id"), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False, index=True)  # connect / check_accept / message / reminder / view
    ok = db.Column(db.Boolean, default=True, nullable=False)
    detail = db.Column(db.Text)
    actor = db.Column(db.String(30), default="worker")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
