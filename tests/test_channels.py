"""API/import channels (GitHub, Behance, ArtStation, Kaggle, Contra) — network mocked."""
import os
import tempfile

import pytest

os.environ["DISABLE_BACKGROUND_SYNC"] = "1"

from app import channels, create_app  # noqa: E402
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

from app.models import Role, SourcedProfile, db  # noqa: E402

H = {"X-API-Key": "k"}


class R:
    def __init__(self, status=200, json=None, text=""):
        self.status_code, self._json, self.text = status, json, text

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


PAGES = {
    "https://api.github.com/search/users": R(json={"items": [{"login": "sara", "html_url": "https://github.com/sara"},
                                                             {"login": "karim", "html_url": "https://github.com/karim"}]}),
    "https://api.github.com/users/sara": R(json={"login": "sara", "name": "Sara Benali", "html_url": "https://github.com/sara",
                                                 "bio": "ML engineer", "location": "Algiers", "company": "@yassir",
                                                 "public_repos": 12, "followers": 40, "email": None, "blog": "sara.dev"}),
    "https://api.github.com/users/karim": R(json={"login": "karim", "name": None, "html_url": "https://github.com/karim",
                                                  "bio": None, "public_repos": 3, "followers": 5}),
    "https://api.github.com/users/sara/repos": R(json=[{"language": "Python"}, {"language": "Python"}, {"language": "SQL"}]),
    "https://api.github.com/users/karim/repos": R(json=[]),
    "https://www.artstation.com/karim_m": R(text='<html><head><title>Karim M - Senior 3D Artist | ArtStation</title>'
                                                 '<meta property="og:description" content="3D environment artist, Unreal, Blender. Lyon"></head></html>'),
    "https://www.behance.net/sarab": R(text='<html><head><meta property="og:title" content="Sara B on Behance">'
                                            '<script type="application/ld+json">{"@type":"Person","name":"Sara Benali",'
                                            '"jobTitle":"Brand designer","address":{"addressLocality":"Paris"}}</script></head></html>'),
    "https://www.kaggle.com/ineshaddad": R(text='<html><head><title>Inès Haddad | Kaggle</title>'
                                                '<meta name="description" content="Inès Haddad is a Kaggle Expert. Data scientist at Yassir."></head></html>'),
    "https://contra.com/thomas_g": R(status=403, text="blocked"),
}


@pytest.fixture(autouse=True)
def fake_requests(monkeypatch):
    def get(url, params=None, headers=None, timeout=None, allow_redirects=True):
        return PAGES.get(url, R(status=404, text=""))
    monkeypatch.setattr(channels.requests, "get", get)


@pytest.fixture(scope="module")
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.session.add(Role(slug="ds", title="Data Scientist", code="DA", requirements="Python, ML"))
        db.session.commit()
    return app


def test_channel_detection():
    assert channels.channel_for_url("https://www.behance.net/x") == "behance"
    assert channels.channel_for_url("https://www.artstation.com/x") == "artstation"
    assert channels.channel_for_url("https://www.kaggle.com/x") == "kaggle"
    assert channels.channel_for_url("https://contra.com/x") == "contra"
    assert channels.channel_for_url("https://github.com/x") == "github"
    assert channels.clean_profile_url("www.kaggle.com/abc/?ref=1") == "https://www.kaggle.com/abc"


def test_github_search_via_api(app):
    with app.app_context():
        out = channels.github_search("language:python", "Algeria", 10)
    assert [p["full_name"] for p in out] == ["Sara Benali", "karim"]
    assert out[0]["location"] == "Algiers" and "Python" in out[0]["headline"] and "40 followers" in out[0]["headline"]


def test_enrich_urls(app):
    with app.app_context():
        a = channels.enrich_url("https://www.artstation.com/karim_m")
        b = channels.enrich_url("https://www.behance.net/sarab")
        k = channels.enrich_url("https://www.kaggle.com/ineshaddad")
        c = channels.enrich_url("https://contra.com/thomas_g")
    assert a["channel"] == "artstation" and a["full_name"] == "Karim M" and "3D Artist" in a["headline"]
    assert b["channel"] == "behance" and b["full_name"] == "Sara Benali" and b["headline"] == "Brand designer" and b["location"] == "Paris"
    assert k["channel"] == "kaggle" and k["full_name"] == "Inès Haddad" and "Data scientist" in k["headline"]
    assert c["channel"] == "contra" and c["full_name"] == "Thomas G"   # blocked page -> name from the slug


def test_import_api_and_admin(app):
    c = app.test_client()
    r = c.post("/api/v1/sourcing/import", headers=H, json={"role": "ds", "urls": [
        "https://www.kaggle.com/ineshaddad", "https://github.com/sara", "https://www.kaggle.com/ineshaddad/"]})
    assert r.status_code == 201 and r.json["new_profiles"] == 2
    chans = sorted(p["channel"] for p in r.json["profiles"])
    assert chans == ["github", "kaggle"]
    # GitHub API search from the admin runs immediately (TESTING -> inline)
    c.post("/admin/login", data={"username": "admin", "password": "pw"})
    r = c.post("/admin/sourcing/ds", data={"action": "search", "query": "language:python", "location": "Algeria",
                                          "max_results": "10", "channel": "github"})
    assert r.status_code == 302
    with app.app_context():
        role = Role.query.filter_by(slug="ds").one()
        s = role.searches.filter_by(channel="github").order_by(db.desc("id")).first()
        assert s.status == "done" and s.results_count >= 1
        names = {p.full_name for p in role.sourced_profiles}
        assert {"Sara Benali", "karim", "Inès Haddad"} <= names
    # import form in the admin
    r = c.post("/admin/sourcing/ds", data={"action": "import", "channel": "auto",
                                          "urls": "https://www.artstation.com/karim_m\nhttps://www.behance.net/sarab"})
    assert r.status_code == 302 and "tab=shortlist" in r.headers["Location"]
    assert c.get("/admin/sourcing/ds?tab=searches").status_code == 200
    assert c.get("/admin/sourcing/ds?tab=queue").status_code == 200


def test_manual_channel_flow_and_email(app):
    c = app.test_client()
    c.post("/admin/login", data={"username": "admin", "password": "pw"})
    with app.app_context():
        p = SourcedProfile.query.filter_by(profile_url="https://www.artstation.com/karim_m").one()
        p.email = "karim@example.com"
        db.session.commit()
        pid = p.id
    c.post(f"/admin/sourcing/profile/{pid}", data={"action": "approve"})
    with app.app_context():
        p = db.session.get(SourcedProfile, pid)
        assert p.next_action == "message" and p.released is False
    # manual channels never enter the worker queue
    assert all(a["profile_id"] != pid for a in c.get("/api/v1/sourcing/queue", headers=H).json["actions"])
    r = c.post(f"/api/v1/sourcing/profiles/{pid}/email", headers=H)
    assert r.json["ok"] is True and r.json["profile"]["stage"] == "link sent"
    sent = app.extensions["_sent_mail"][-1]
    assert sent["to"] == "karim@example.com" and "/r/" in sent["body"]
    assert c.get(f"/admin/sourcing/profile/{pid}").status_code == 200


def test_regions_expand_and_search_fanout(app):
    from app import regions
    assert regions.parse("france, maghreb, nope") == ["france", "maghreb"]
    assert regions.expand(["maghreb"], "linkedin") == ["Algeria", "Morocco", "Tunisia"]
    assert regions.expand(["france"], "github", custom="Lyon, Oran") == ["Lyon", "Oran", "France"]
    assert regions.expand(["remote"], "linkedin") == [] and regions.is_remote("remote")
    assert "Algiers" in regions.expand(["algeria"], "linkedin", with_cities=True)
    assert regions.countries(["maghreb", "france"]) == ["France", "Algeria", "Morocco", "Tunisia"]
    c = app.test_client()
    # API: one search per country, GitHub ones run immediately
    r = c.post("/api/v1/sourcing/searches", headers=H, json={"role": "ds", "query": "language:python",
                                                             "channel": "github", "regions": ["maghreb"]})
    assert r.status_code == 201
    locs = [s["location"] for s in r.json["searches"]]
    assert locs == ["Algeria", "Morocco", "Tunisia"]
    assert all(s["status"] == "done" for s in r.json["searches"])
    assert r.json["searches"][0]["region"] == "maghreb"
    assert len(c.get("/api/v1/sourcing/regions", headers=H).json["regions"]) == len(regions.ORDER)
    # admin form with chips + custom, LinkedIn -> queued for the worker
    c.post("/admin/login", data={"username": "admin", "password": "pw"})
    r = c.post("/admin/sourcing/ds", data={"action": "search", "query": "data scientist", "channel": "linkedin",
                                          "regions": ["france", "remote"], "custom": "Oran", "max_results": "20"})
    assert r.status_code == 302
    with app.app_context():
        role = Role.query.filter_by(slug="ds").one()
        li = role.searches.filter_by(channel="linkedin", status="pending").all()
        assert sorted(s.location for s in li) == ["France", "Oran"]
        # role defaults + schema countries
        role.target_regions = "france,algeria"
        db.session.commit()
    html = c.get("/admin/sourcing/ds?tab=searches").get_data(as_text=True)
    assert 'value="france" checked' in html and 'value="algeria" checked' in html
    html = c.get("/admin/roles/1/edit").get_data(as_text=True)
    assert 'name="target_regions"' in html
    page = c.get("/apply/ds").get_data(as_text=True)
    assert "applicantLocationRequirements" in page or "jobLocation" in page
