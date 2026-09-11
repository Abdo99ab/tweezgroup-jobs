"""Worker loop against the real app (Flask test client) with a fake LinkedIn — no browser, no network."""
import os
import tempfile
from datetime import datetime

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
    SOURCING_CAP_CONNECTS = 50
    SOURCING_CAP_MESSAGES = 50
    SOURCING_CAP_VIEWS = 200

from app.models import Role, SourcedProfile, SourcingSearch, db  # noqa: E402
from worker.client import Api  # noqa: E402
from worker.linkedin import Warning_  # noqa: E402
from worker.main import Worker, within_hours  # noqa: E402


class TestClientApi(Api):
    """Same methods as Api, but through Flask's test client."""
    def __init__(self, client):
        self.c = client

    def _r(self, method, path, **kw):
        r = self.c.open("/api/v1" + path, method=method, headers={"X-API-Key": "k"},
                        json=kw.get("json"), query_string=kw.get("params"))
        assert r.status_code < 400, r.get_data(as_text=True)
        return r.json


class FakeLinkedIn:
    def __init__(self):
        self.log = []
        self.min_delay = self.max_delay = 0
        self.connected = set()
        self.warn_on_message = False

    def between_actions(self):
        pass

    def search_people(self, query, location=None, max_results=50):
        self.log.append(("search", query))
        return [{"profile_url": f"https://www.linkedin.com/in/p{i}", "full_name": f"Person {i}",
                 "headline": "B2B sales", "location": "Paris"} for i in range(3)]

    def connect(self, url, note):
        self.log.append(("connect", url, note))
        return True, "invitation sent"

    def is_connected(self, url):
        return url in self.connected

    def message(self, url, text):
        if self.warn_on_message:
            raise Warning_("checkpoint")
        self.log.append(("message", url, text))
        return True, "sent"

    def read_inbox(self, max_threads=15):
        return [{"profile_url": "https://www.linkedin.com/in/p0", "text": "Sounds interesting, tell me more"}]


@pytest.fixture(scope="module")
def app():
    app = create_app(Cfg)
    with app.app_context():
        r = Role(slug="sm", title="Sourcing Manager", code="SM", sourcing_autosend=True)
        db.session.add(r)
        db.session.add(SourcingSearch(role=r, keywords="sourcing manager", max_results=10))
        db.session.commit()
    return app


def test_within_hours():
    st = {"hours": "09:00-18:00", "tz": "Europe/Paris"}
    assert within_hours(st, datetime(2026, 9, 8, 10, 0))       # Tuesday 10:00
    assert not within_hours(st, datetime(2026, 9, 8, 19, 0))
    assert not within_hours(st, datetime(2026, 9, 12, 10, 0))  # Saturday


def test_full_pass(app, monkeypatch):
    monkeypatch.setattr("worker.main.within_hours", lambda *a, **k: True)
    api = TestClientApi(app.test_client())
    li = FakeLinkedIn()
    w = Worker(api, li)
    assert w.pass_once() == "ok"
    assert ("search", "sourcing manager") in li.log
    with app.app_context():
        role = Role.query.filter_by(slug="sm").one()
        assert role.searches.first().status == "done"
        assert role.sourced_profiles.count() == 3
        ids = [p.id for p in role.sourced_profiles.order_by(SourcedProfile.id).all()]
    # approve two -> autosend role, so connect happens on the next pass
    for pid in ids[:2]:
        api._r("PATCH", f"/sourcing/profiles/{pid}", json={"decision": "approved"})
    li.log.clear()
    assert w.pass_once() == "ok"
    assert [x[0] for x in li.log] == ["connect", "connect"]
    assert "Hi Person," in li.log[0][2]
    with app.app_context():
        p = db.session.get(SourcedProfile, ids[0])
        assert p.next_action == "check_accept" and p.connect_sent_at
        p.next_due_at = None            # make the check due now
        db.session.get(SourcedProfile, ids[1]).next_due_at = None
        db.session.commit()
    # one accepted, one not
    li.connected.add("https://www.linkedin.com/in/p0")
    li.log.clear()
    w.last_inbox = datetime(2000, 1, 1)
    assert w.pass_once() == "ok"
    with app.app_context():
        p0 = db.session.get(SourcedProfile, ids[0])
        p1 = db.session.get(SourcedProfile, ids[1])
        assert p0.accepted_at and p1.accepted_at is None and p1.next_action == "check_accept"
        # autosend: the link message went out in the same pass? No — message is due immediately but the
        # queue was fetched before check_accept ran; it goes on the next pass.
        assert p0.next_action == "message" and p0.released is True
        # inbox reply matched p0
        assert p0.reply_text.startswith("Sounds interesting")
    li.log.clear()
    assert w.pass_once() == "ok"
    assert li.log and li.log[0][0] == "message" and "/r/" in li.log[0][2]


def test_warning_pauses(app, monkeypatch):
    monkeypatch.setattr("worker.main.within_hours", lambda *a, **k: True)
    api = TestClientApi(app.test_client())
    li = FakeLinkedIn()
    li.warn_on_message = True
    w = Worker(api, li)
    with app.app_context():
        p = SourcedProfile.query.filter_by(profile_url="https://www.linkedin.com/in/p2").one()
        pid = p.id
    api._r("PATCH", f"/sourcing/profiles/{pid}", json={"decision": "approved"})
    with app.app_context():
        p = db.session.get(SourcedProfile, pid)
        p.next_action, p.released, p.next_due_at = "message", True, None
        p.draft_message = "hi"
        db.session.commit()
    assert w.pass_once() == "warning"
    st = api.status()
    assert st["paused_until"] and "checkpoint" in st["paused_reason"]
    assert w.pass_once() == "paused"
