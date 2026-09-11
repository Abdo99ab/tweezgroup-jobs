"""Regression tests for Milestone 1 (sourcing & outreach) — run: python -m pytest tests -q
No network: Claude pre-scoring, ClickUp and mail use the log/off backends."""
import io
import os
import tempfile

import pytest

os.environ["DISABLE_BACKGROUND_SYNC"] = "1"

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402


class TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
    SQLALCHEMY_ENGINE_OPTIONS = {}
    TESTING = True
    STORAGE_BACKEND = "local"
    LOCAL_UPLOAD_DIR = __import__("pathlib").Path(tempfile.mkdtemp())
    CLICKUP_ENABLED = False
    SUMMARY_ENABLED = False
    SOURCING_PRESCORE_ENABLED = False
    MAIL_BACKEND = "log"
    PROCESS_ASYNC = False
    API_KEY = "k"
    ADMIN_PASSWORD = "pw"
    PUBLIC_BASE_URL = "https://jobs.example.com"
    SOURCING_SENDER_NAME = "Mehdi"
    SOURCING_WARMUP_DAYS = 0


class Cfg(TestConfig):
    SOURCING_CAP_CONNECTS = 2
    SOURCING_CAP_MESSAGES = 4
    SOURCING_CAP_VIEWS = 10

from app.models import Role, SourcedProfile, SourcingSearch, db  # noqa: E402

H = {"X-API-Key": "k"}


@pytest.fixture(scope="module")
def app():
    app = create_app(Cfg)
    with app.app_context():
        r = Role(slug="b2b-manager", title="B2B Manager", code="BB", is_open=True,
                 requirements="MUST: 3y B2B sales, French+English", location="Remote / France")
        db.session.add(r)
        db.session.commit()
    return app


@pytest.fixture
def c(app):
    return app.test_client()


def admin(c):
    c.post("/admin/login", data={"username": "admin", "password": "pw"})
    return c


def test_search_and_results_flow(app, c):
    # admin queues a search
    admin(c)
    r = c.post("/admin/sourcing/b2b-manager", data={"action": "search", "query": '"B2B Manager" ecommerce',
                                                    "location": "France", "max_results": "20"})
    assert r.status_code == 302
    # worker picks it up (status flips to running)
    r = c.get("/api/v1/sourcing/searches", headers=H)
    s = r.json["searches"]
    assert len(s) == 1 and s[0]["status"] == "running" and s[0]["query"].startswith('"B2B')
    sid = s[0]["id"]
    # second poll returns nothing (already running)
    assert c.get("/api/v1/sourcing/searches", headers=H).json["searches"] == []
    # worker posts results
    r = c.post(f"/api/v1/sourcing/searches/{sid}/results", headers=H, json={"done": True, "profiles": [
        {"profile_url": "https://www.linkedin.com/in/sara-b/?trk=x", "full_name": "Sara Benali",
         "headline": "B2B Sales Manager @ Shop", "location": "Paris", "company": "Shop"},
        {"profile_url": "https://www.linkedin.com/in/karim-m", "full_name": "Karim M", "headline": "Account exec"},
        {"profile_url": "https://www.linkedin.com/in/sara-b", "full_name": "Sara Benali"},  # duplicate
    ]})
    assert r.json["new_profiles"] == 2 and r.json["search"]["status"] == "done"
    with app.app_context():
        role = Role.query.filter_by(slug="b2b-manager").one()
        assert role.sourced_profiles.count() == 2
        p = SourcedProfile.query.filter_by(full_name="Sara Benali").one()
        assert p.profile_url == "https://www.linkedin.com/in/sara-b"   # cleaned
        assert p.decision == "pending" and p.apply_token


def test_approve_queue_and_state_machine(app, c):
    admin(c)
    with app.app_context():
        role = Role.query.filter_by(slug="b2b-manager").one()
        ids = [p.id for p in role.sourced_profiles.order_by(SourcedProfile.id).all()]
    # approve both via the admin batch form
    r = c.post("/admin/sourcing/b2b-manager", data={"action": "decide", "decision": "approve", "ids": ids})
    assert r.status_code == 302
    with app.app_context():
        p = db.session.get(SourcedProfile, ids[0])
        assert p.decision == "approved" and p.next_action == "connect" and p.released is False
        assert "Sara" in p.draft_note and len(p.draft_note) <= 300
        assert f"https://jobs.example.com/r/{p.apply_token}" in p.draft_message
        assert p.draft_message.rstrip().endswith("Mehdi")
    # human-confirm: nothing in the queue until released
    assert c.get("/api/v1/sourcing/queue", headers=H).json["actions"] == []
    c.post("/admin/sourcing/b2b-manager", data={"action": "decide", "decision": "release", "ids": ids})
    q = c.get("/api/v1/sourcing/queue", headers=H).json
    assert [a["action"] for a in q["actions"]] == ["connect", "connect"]
    assert q["actions"][0]["text"]
    # locked: a second poll returns nothing
    assert c.get("/api/v1/sourcing/queue", headers=H).json["actions"] == []
    # worker reports both connects sent
    for a in q["actions"]:
        r = c.post(f"/api/v1/sourcing/profiles/{a['profile_id']}/outcome", headers=H,
                   json={"action": "connect", "ok": True})
        assert r.json["profile"]["next_action"] == "check_accept"
    # caps: 2 connects used out of 2
    st = c.get("/api/v1/sourcing/status", headers=H).json
    assert st["caps"]["used"]["connect"] == 2 and st["caps"]["left"]["connect"] == 0
    # accepted -> message step needs a human Send again (autosend off)
    r = c.post(f"/api/v1/sourcing/profiles/{ids[0]}/outcome", headers=H,
               json={"action": "check_accept", "ok": True, "accepted": True})
    assert r.json["profile"]["next_action"] == "message" and r.json["profile"]["released"] is False
    c.post(f"/admin/sourcing/profile/{ids[0]}", data={"action": "release"})
    # due now -> in queue with the link text
    with app.app_context():
        p = db.session.get(SourcedProfile, ids[0])
        p.next_due_at = None
        db.session.commit()
    q = c.get("/api/v1/sourcing/queue", headers=H).json["actions"]
    assert len(q) == 1 and q[0]["action"] == "message" and "/r/" in q[0]["text"]
    r = c.post(f"/api/v1/sourcing/profiles/{ids[0]}/outcome", headers=H, json={"action": "message", "ok": True})
    assert r.json["profile"]["next_action"] == "reminder" and r.json["profile"]["stage"] == "link sent"


def test_link_opened_prefills_and_apply_attaches(app, c):
    with app.app_context():
        p = SourcedProfile.query.filter_by(full_name="Sara Benali").one()
        token, pid = p.apply_token, p.id
    r = c.get(f"/r/{token}")
    assert r.status_code == 302 and f"/apply/b2b-manager?t={token}" in r.headers["Location"]
    r = c.get(f"/apply/b2b-manager?t={token}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'value="Sara Benali"' in html and "linkedin.com/in/sara-b" in html and f'name="t" value="{token}"' in html
    assert '<option value="linkedin" selected>' in html
    assert '"@type": "JobPosting"' in html  # Google for Jobs schema
    with app.app_context():
        p = db.session.get(SourcedProfile, pid)
        assert p.link_opened_at and p.next_action is None and p.close_reason == "link opened"  # reminder cancelled
    # candidate applies
    r = c.post("/apply/b2b-manager", data={
        "full_name": "Sara Benali", "email": "sara@example.com", "years_experience": "4", "source": "linkedin",
        "consent": "on", "t": token, "cv": (io.BytesIO(b"%PDF-1.4 fake"), "cv.pdf")},
        content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)
    with app.app_context():
        p = db.session.get(SourcedProfile, pid)
        assert p.applicant_id and p.applicant.email == "sara@example.com" and p.applicant.source == "linkedin"
        assert p.close_reason == "applied"
    f = c.get("/api/v1/sourcing/funnel?role=b2b-manager", headers=H).json["funnel"]["linkedin"]
    assert f["found"] == 2 and f["link_sent"] == 1 and f["link_opened"] == 1 and f["applied"] == 1


def test_reply_and_pause(app, c):
    with app.app_context():
        p = SourcedProfile.query.filter_by(full_name="Karim M").one()
        url, pid = p.profile_url, p.id
    r = c.post("/api/v1/sourcing/inbox", headers=H, json={"messages": [{"profile_url": url, "text": "Not interested, thanks."}]})
    assert r.json["matched"] == 1
    with app.app_context():
        p = db.session.get(SourcedProfile, pid)
        assert p.reply_text.startswith("Not interested") and p.reply_class == "negative"
    # warning from the worker pauses everything
    c.post(f"/api/v1/sourcing/profiles/{pid}/outcome", headers=H,
           json={"action": "check_accept", "ok": False, "warning": "captcha"})
    st = c.get("/api/v1/sourcing/status", headers=H).json
    assert st["paused_until"] and "captcha" in st["paused_reason"]
    assert c.get("/api/v1/sourcing/queue", headers=H).json["actions"] == []
    c.post("/api/v1/sourcing/resume", headers=H)
    assert c.get("/api/v1/sourcing/status", headers=H).json["paused_until"] is None


def test_referral_emails_link(app, c):
    admin(c)
    r = c.post("/admin/sourcing/refer", data={"role": "b2b-manager", "full_name": "Nadia R",
                                              "email": "nadia@example.com", "referred_by": "Amine"})
    assert r.status_code == 302
    with app.app_context():
        p = SourcedProfile.query.filter_by(full_name="Nadia R").one()
        assert p.channel == "referral" and p.message_sent_at and p.decision == "approved"
        sent = app.extensions["_sent_mail"][-1]
        assert sent["to"] == "nadia@example.com" and f"/r/{p.apply_token}" in sent["body"] and "Amine" in sent["body"]
    # API channel add (e.g. Torre) with a LinkedIn URL and no email -> queue
    r = c.post("/api/v1/sourcing/profiles", headers=H, json={"role": "b2b-manager", "full_name": "Ali T",
                                                             "profile_url": "https://www.linkedin.com/in/ali-t",
                                                             "channel": "linkedin"})
    assert r.status_code == 201 and r.json["profile"]["next_action"] == "connect" and "/r/" in r.json["apply_url"]


def test_inbound_feeds(c):
    r = c.get("/sitemap.xml")
    assert r.status_code == 200 and "/apply/b2b-manager" in r.get_data(as_text=True)
    r = c.get("/jobs/feed.xml")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "<job>" in body and "src=indeed" in body and "B2B Manager" in body


def test_admin_pages_render(app, c):
    admin(c)
    assert c.get("/admin/sourcing").status_code == 200
    for tab in ("shortlist", "queue", "contacted", "searches"):
        assert c.get(f"/admin/sourcing/b2b-manager?tab={tab}").status_code == 200
    with app.app_context():
        pid = SourcedProfile.query.first().id
    assert c.get(f"/admin/sourcing/profile/{pid}").status_code == 200
    r = c.get("/admin/roles/1/edit")
    assert r.status_code == 200 and "msg_connection" in r.get_data(as_text=True)
