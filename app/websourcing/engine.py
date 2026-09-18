"""The public-web sourcing engine.

    JD -> extract requirements -> generate queries per source -> run the enabled adapters
       -> normalize -> dedupe -> match & score vs the JD (evidence-labelled, nothing invented)
       -> persist ranked WebCandidates for the recruiter to review.

Outreach never happens here: a recruiter shortlists, then explicitly moves a candidate to
the existing outreach pipeline (or emails them) — every contact is a human decision.
"""
import logging
import threading

from flask import current_app

from ..models import Role, SourcedProfile, SourcingSearch, WebCandidate, WebSourcingRun, _json_set, db, utcnow
from . import base
from .base import LIKELY, candidate, dedupe_key, evidence
from .boards import BoardsAdapter
from .github import GitHubAdapter
from .gitlab import GitLabAdapter
from .hackernews import HackerNewsAdapter
from .linkedin import LinkedInAdapter
from .llm import claude_json
from .stackexchange import StackExchangeAdapter
from .webpages import WebPagesAdapter
from .ycombinator import YCombinatorAdapter

log = logging.getLogger(__name__)

ADAPTERS = {a.name: a for a in (LinkedInAdapter, GitHubAdapter, GitLabAdapter,
                                StackExchangeAdapter, HackerNewsAdapter, BoardsAdapter,
                                YCombinatorAdapter, WebPagesAdapter)}

REQ_PROMPT = """Read this job description and extract its requirements for candidate sourcing.

ROLE TITLE: {title}
LOCATION / SETUP: {location}
DESCRIPTION AND REQUIREMENTS:
\"\"\"{jd}\"\"\"
RECRUITER'S TARGETING FOR THIS RUN (wins over the JD wherever they differ; fold these into
the extracted fields and the search queries):
{criteria}

Return ONLY a JSON object:
  "skills": up to 8 required hard skills/tools, most important first (lowercase)
  "titles": up to 4 job-title variants for this role (lowercase)
  "seniority": one of "junior", "mid", "senior", "lead", "any"
  "min_years": minimum years of experience as an integer (0 if not stated)
  "languages": spoken languages required, e.g. ["english", "french"] (empty if not stated)
  "location": one line: where the person must be / remote policy ("" if not stated)
  "industries": up to 4 industry keywords (e.g. "e-commerce", "logistics")
  "queries": {{"github": [2-3 GitHub user-search queries, e.g. "language:python location:france"],
              "gitlab": [1-2 short project-search keywords]}}
Use only what the description states — do not invent requirements."""

SCORE_PROMPT = """You are screening a candidate DISCOVERED ON THE PUBLIC WEB for this role.
Score strictly from the evidence below. NEVER assume or invent anything: a requirement with
no evidence goes to "missing", it does not lower honesty elsewhere. Be fair — public
profiles are partial by nature.

ROLE: {title} at {company}
REQUIREMENTS (from the JD and the recruiter's targeting): {req}

CANDIDATE (public data, each line carries its evidence label):
Name: {name}
Headline: {headline}
Location: {location} | Company: {cand_company}
Skills seen: {skills}
Evidence:
{evidence}

Return ONLY a JSON object:
  "score": 0-100 fit (60 = worth a recruiter's look, 80+ = strong match on the evidence)
  "confidence": "high"|"medium"|"low" — how much evidence backs the score
  "matched": list of short strings, each a requirement met WITH its evidence label,
             e.g. "Python — 6 public repos (Confirmed)"
  "missing": list of requirements that have no evidence (label each "(Unknown)")
  "reason": one factual sentence: strongest signal + main gap."""


# The public websites each lane covers — everything here is LinkedIn-free.
# Engine adapters work them automatically; "browser lane" sites are folded into the
# Launch-in-Claude worker job (a Claude session browsing them as a normal visitor).
SITES = {
    "linkedin": ["linkedin.com people search — headed Chromium worker"],
    "github": ["github.com/search (users) — same Chromium window"],
    "gitlab": ["gitlab.com user search — same Chromium window"],
    "stackexchange": ["stackoverflow.com tag top-users — same Chromium window"],
    "hackernews": ["news.ycombinator.com “Who wants to be hired?” — same Chromium window"],
    "boards": ["boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com"],
    "ycombinator": ["ycombinator.com/companies — same Chromium window"],
    "webpages": ["personal portfolio sites", "company /team and /about pages"],
}
BROWSER_SITES = [
    "humetric.com and similar public people directories",
    "behance.net and dribbble.com (public design portfolios)",
    "artstation.com (public art portfolios)", "kaggle.com (public data-science profiles)",
    "dev.to and medium.com (author profiles on relevant articles)",
    "torre.ai and contra.com public profiles",
    "github.com / gitlab.com / stackoverflow.com (deeper than the API search)",
    "company team pages, conference speaker lists, meetup organiser pages",
]


def enabled_sources(cfg):
    names = [s.strip() for s in cfg["WEBSOURCING_SOURCES"].split(",") if s.strip()]
    return [n for n in names if n in ADAPTERS]


# ------------------------------------------------------------------ per-role pause + run stop

def paused_role_ids():
    from ..models import Setting, _json_get
    return set(_json_get(Setting.get("websourcing_paused"), []))


def is_paused(role):
    return role.id in paused_role_ids()


def set_paused(role, paused):
    """Pause blocks new runs for the role AND asks any active run to stop."""
    from ..models import Setting
    import json as _json
    ids = paused_role_ids()
    (ids.add if paused else ids.discard)(role.id)
    Setting.put("websourcing_paused", _json.dumps(sorted(ids)))
    if paused:
        stop_role_runs(role)


def stop_role_runs(role):
    """Ask the role's pending/running discovery runs to stop (cooperative — the worker
    thread checks between sources and every few candidates). Returns how many were asked."""
    active = (WebSourcingRun.query.filter(WebSourcingRun.role_id == role.id,
                                          WebSourcingRun.status.in_(("pending", "running")))
              .all())
    n = 0
    ids = []
    for run in active:
        run.status = "stopping"
        n += 1
        ids.extend(((run.stats or {}).get("linkedin") or {}).get("search_ids") or [])
    if ids:
        (SourcingSearch.query.filter(SourcingSearch.id.in_(ids),
                                     SourcingSearch.status == "pending")
         .update({"status": "error", "error": "stopped by recruiter",
                  "finished_at": utcnow()}, synchronize_session=False))
    db.session.commit()
    return n


def _stop_requested(run_id):
    return db.session.execute(
        db.select(WebSourcingRun.status).filter_by(id=run_id)).scalar() == "stopping"


# ------------------------------------------------------------------ requirements & queries

def _split_list(text):
    import re
    return [t.strip().lower() for t in re.split(r"[,;/|\n]", text or "") if t.strip()]


def apply_criteria(req, crit):
    """Overlay the recruiter's launch filters on the extracted requirements — they always win.
    Applied even when Claude already saw them, so the no-API-key path behaves the same."""
    if not crit:
        return req
    if crit.get("profile"):
        p = crit["profile"].strip().lower()
        req["titles"] = [p] + [t for t in (req.get("titles") or []) if t != p]
    if crit.get("tools"):
        tools = _split_list(crit["tools"])
        req["skills"] = (tools + [s for s in (req.get("skills") or []) if s not in tools])[:10]
    if crit.get("location"):
        req["location"] = crit["location"].strip()
    if crit.get("languages"):
        req["languages"] = _split_list(crit["languages"])
    if crit.get("min_years"):
        req["min_years"] = int(crit["min_years"])
    req["recruiter_criteria"] = crit
    return req


def extract_requirements(role, crit=None):
    """Claude reads the JD (+ the recruiter's targeting); a keyword fallback keeps
    dev/mock environments working. apply_criteria() runs on the result either way."""
    jd = "\n\n".join(x for x in (role.description, role.requirements, role.sourcing_brief) if x)[:8000]
    data = claude_json(REQ_PROMPT.format(title=role.title, location=role.location or "-", jd=jd or "-",
                                         criteria=crit or "(none — use the JD alone)"))
    if data and isinstance(data.get("skills"), list):
        data.setdefault("queries", {})
        data.setdefault("titles", [role.title.lower()])
        return data
    # fallback: crude keyword pull so the feature still runs without an API key
    import re
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.]{2,}", (jd or role.title).lower())
    common = [w for w in dict.fromkeys(words) if w not in
              ("the", "and", "with", "for", "you", "our", "are", "will", "have", "this", "that",
               "from", "who", "your", "about", "work", "team", "years", "experience")][:8]
    return {"skills": common, "titles": [role.title.lower()], "seniority": "any",
            "languages": [], "location": role.location or "", "industries": [],
            "queries": {}}


def build_queries(role, req):
    q = dict(req.get("queries") or {})
    skills = req.get("skills") or []
    title = (req.get("titles") or [role.title])[0]
    # GitHub location qualifier from the targeted location (skip vague/remote phrasings)
    loc = (req.get("location") or "").split(",")[0].split("(")[0].strip()
    loc_q = f' location:"{loc}"' if loc and len(loc.split()) <= 3 and "remote" not in loc.lower() else ""
    if not q.get("github"):
        q["github"] = [(" ".join(skills[:2]) + (f' "{title}" in:bio' if title else "") + loc_q)[:100],
                       (" ".join(skills[:3]) + loc_q)[:100]]
        q["github"] = [x for x in q["github"] if x.strip()]
    elif loc_q:  # recruiter targeted a location: make sure every GitHub query respects it
        q["github"] = [x if "location:" in x else (x + loc_q)[:100] for x in q["github"]]
    if not q.get("gitlab"):
        q["gitlab"] = skills[:2] or [title]
    return q


# ------------------------------------------------------------------ scoring

def _score_heuristic(req, cand):
    """No-API-key fallback: transparent keyword overlap, labelled honestly."""
    want = [s.lower() for s in (req.get("skills") or [])]
    have = set(cand["skills"]) | {w for e in cand["evidence"] for w in e["claim"].lower().split()}
    matched = [f"{s} (Confirmed)" for s in want if s in have]
    missing = [f"{s} (Unknown)" for s in want if s not in have]
    score = int(30 + 60 * (len(matched) / max(1, len(want)))) if want else 50
    return {"score": score, "confidence": "low",
            "matched": matched, "missing": missing,
            "reason": f"Keyword overlap only ({len(matched)}/{len(want)} required skills evidenced); "
                      "no AI screening (API key not set)."}


def score_candidate(role, req, cand):
    ev_lines = "\n".join(f"- [{e['label']}] {e['claim']} ({e['url']})" for e in cand["evidence"][:20])
    data = claude_json(SCORE_PROMPT.format(
        title=role.title, company=current_app.config["COMPANY_NAME"],
        req={k: req.get(k) for k in ("skills", "titles", "seniority", "min_years", "languages", "location")
             if req.get(k)},
        name=cand["full_name"], headline=cand["headline"] or "-",
        location=cand["location"] or "(unknown)", cand_company=cand["company"] or "(unknown)",
        skills=", ".join(cand["skills"]) or "-", evidence=ev_lines or "- (none)"),
        max_tokens=800)
    if not data or "score" not in data:
        data = _score_heuristic(req, cand)
    data["score"] = max(0, min(100, int(data.get("score", 0))))
    if data.get("confidence") not in ("high", "medium", "low"):
        data["confidence"] = "low"
    return data


# ------------------------------------------------------------------ the run

# A run with no heartbeat for this long is assumed dead (Flask restart, hung DNS, etc.).
_STALE_AFTER_SEC = 3 * 60


def reap_stale_runs():
    """Mark discovery runs whose background thread vanished as error, so the UI
    does not sit on RUNNING forever after a Flask restart."""
    now = utcnow()
    dirty = False
    for run in WebSourcingRun.query.filter(
            WebSourcingRun.status.in_(("pending", "running", "stopping"))):
        ids = ((run.stats or {}).get("linkedin") or {}).get("search_ids") or []
        if ids and SourcingSearch.query.filter(
                SourcingSearch.id.in_(ids),
                SourcingSearch.status.in_(("pending", "running"))).count():
            continue  # LinkedIn browser worker is still supposed to pick this up
        if any(isinstance(v, dict) and v.get("worker") in ("pending", "running")
               for v in (run.stats or {}).values()):
            continue  # Chromium still has public sources to visit
        beat = (run.stats or {}).get("_progress", {}).get("at")
        ref = None
        if beat:
            try:
                from datetime import datetime
                ref = datetime.fromisoformat(str(beat))
                if getattr(ref, "tzinfo", None) is not None:
                    ref = ref.replace(tzinfo=None)
            except (TypeError, ValueError):
                ref = None
        ref = ref or run.created_at
        if ref and (now - ref).total_seconds() > _STALE_AFTER_SEC:
            run.status = "error"
            run.error = ("Discovery worker stopped — the Flask process likely restarted, "
                         "or a source hung waiting for the network. Start the run again.")[:1000]
            run.finished_at = now
            dirty = True
            log.warning("websourcing run %s reaped as stale (last beat %s)", run.id, ref)
    if dirty:
        db.session.commit()


def start_run(role, sources=None, seed_urls="", criteria=None):
    """Create the run row and execute it in a background thread. Returns the run.
    criteria: the recruiter's launch filters {location, min_years, profile, tools, languages}.

    LinkedIn is queued for the headed Playwright worker (a real browser). Public API
    adapters run inside this process."""
    cfg = current_app.config
    if is_paused(role):
        raise RuntimeError(f"Sourcing for “{role.title}” is paused — resume it first.")
    reap_stale_runs()
    sources = [s for s in (sources or enabled_sources(cfg)) if s in ADAPTERS]
    crit = {k: v for k, v in (criteria or {}).items() if v}
    run = WebSourcingRun(role=role, status="pending", sources=",".join(sources),
                         seed_urls=seed_urls.strip() or None,
                         criteria_json=_json_set(crit))
    db.session.add(run)
    db.session.flush()
    stats = {}
    if "linkedin" in sources:
        searches, query = _queue_linkedin(role, crit, cfg["WEBSOURCING_MAX_PER_SOURCE"])
        stats["linkedin"] = {
            "found": 0,
            "notes": ["queued for the LinkedIn browser worker — run `python -m worker run` "
                      "so Chromium opens and searches"],
            "search_ids": [s.id for s in searches],
            "query": query,
        }
        run.stats_json = _json_set(stats)
    db.session.commit()
    app = current_app._get_current_object()
    threading.Thread(target=_execute, args=(app, run.id), daemon=True,
                     name=f"websourcing-run-{run.id}").start()
    return run


def _execute(app, run_id):
    with app.app_context():
        run = db.session.get(WebSourcingRun, run_id)
        if run is None:
            log.error("websourcing run %s vanished before the worker started", run_id)
            return
        try:
            log.info("websourcing run %s starting", run_id)
            _run(run)
            log.info("websourcing run %s finished status=%s", run_id, run.status)
        except Exception as exc:
            log.exception("websourcing run %s failed", run_id)
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"[:1000]
            run.finished_at = utcnow()
            db.session.commit()
        finally:
            db.session.remove()


def _heartbeat(run, stats, **progress):
    progress["at"] = utcnow().isoformat()
    stats["_progress"] = progress
    run.stats_json = _json_set(stats)
    db.session.commit()


def _score_into(run, role, req, cand, scored_keys):
    row = _upsert(run, role, cand)
    if row is None:
        return False
    key = dedupe_key(cand["profile_url"], cand["email"])
    if key in scored_keys:
        return True
    verdict = score_candidate(role, req, cand)
    row.match_score = verdict["score"]
    row.confidence = verdict["confidence"]
    row.matched_json = _json_set(verdict.get("matched") or [])
    row.missing_json = _json_set(verdict.get("missing") or [])
    row.match_reason = (verdict.get("reason") or "")[:1000]
    scored_keys.add(key)
    return True


def _run(run):
    role = run.role
    base.reset_state()
    # don't clobber a stop that arrived while the run was still pending
    claimed = (WebSourcingRun.query.filter_by(id=run.id, status="pending")
               .update({"status": "running"}, synchronize_session=False))
    db.session.commit()
    db.session.refresh(run)
    if not claimed and run.status != "running":
        run.status = "stopped"
        run.finished_at = utcnow()
        db.session.commit()
        return

    stats = dict(run.stats or {})
    _heartbeat(run, stats, phase="reading the job description")

    crit = run.criteria
    req = apply_criteria(extract_requirements(role, crit), crit)
    queries = build_queries(role, req)
    run.requirements_json = _json_set(req)
    run.queries_json = _json_set(queries)
    _heartbeat(run, stats, phase="searching public sources")

    seeds = [u.strip() for u in (run.seed_urls or "").replace(",", "\n").splitlines() if u.strip()]
    stopped = False

    for name in (run.sources or "").split(","):
        cls = ADAPTERS.get(name)
        if not cls or name == "linkedin":
            continue
        if getattr(cls, "needs_seeds", False) and not seeds:
            stats[name] = {"found": 0, "notes": ["needs pasted URLs — none given"], "worker": "skipped"}
            continue
        stats[name] = {"found": 0, "notes": ["queued for the Chromium worker — it will open this site"],
                       "worker": "pending"}
    _heartbeat(run, stats, phase="waiting for the browser worker")

    if not stopped:
        stopped = _wait_worker_sources(run, stats) or stopped
        run = db.session.get(WebSourcingRun, run.id)
        stats = dict(run.stats or {})
    if not stopped and ((stats.get("linkedin") or {}).get("search_ids")):
        stopped = _wait_linkedin(run, stats) or stopped
        run = db.session.get(WebSourcingRun, run.id)
        stats = dict(run.stats or {})

    li_found = int((stats.get("linkedin") or {}).get("found") or 0)
    web_found = sum(int((stats.get(n) or {}).get("found") or 0)
                    for n in (run.sources or "").split(",") if n and n != "linkedin")
    stats.pop("_progress", None)
    stats["_total"] = {"found": web_found + li_found, "unique": web_found + li_found,
                       "scored": web_found + li_found}
    run.stats_json = _json_set(stats)
    run.status = "stopped" if stopped else "done"
    run.finished_at = utcnow()
    db.session.commit()


def _linkedin_query(role, crit):
    title = ((crit or {}).get("profile") or role.title or "").strip()
    tools = ((crit or {}).get("tools") or "").strip()
    q = f'"{title}"' if " " in title else title
    if tools:
        q = f"{q} {tools}"
    return q[:400]


def _queue_linkedin(role, crit, cap):
    from .. import sourcing
    query = _linkedin_query(role, crit)
    loc = ((crit or {}).get("location") or "").strip() or None
    created = sourcing.create_searches(
        role, query, channel="linkedin", region_keys=None,
        custom=loc, max_results=int(cap or 25), status="pending")
    log.info("queued %s LinkedIn search(es) for role %s query=%r loc=%r",
             len(created), role.slug, query, loc)
    return created, query


def _wait_linkedin(run, stats, timeout_sec=15 * 60):
    """Keep the run alive until the browser worker finishes (or the recruiter hits Stop)."""
    import time
    ids = (stats.get("linkedin") or {}).get("search_ids") or []
    if not ids:
        return False
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _stop_requested(run.id):
            return True
        rows = SourcingSearch.query.filter(SourcingSearch.id.in_(ids)).all()
        found = sum(s.results_count or 0 for s in rows)
        pending = [s for s in rows if s.status in ("pending", "running")]
        notes = []
        if any(s.status == "pending" for s in rows):
            notes.append("waiting for LinkedIn browser worker — start `python -m worker run` "
                         "if Chromium is not open")
        elif any(s.status == "running" for s in rows):
            notes.append("Chromium is searching LinkedIn…")
        for s in rows:
            if s.error:
                notes.append(s.error)
        stats["linkedin"] = {**(stats.get("linkedin") or {}), "found": found, "notes": notes[:6]}
        _heartbeat(run, stats, phase="waiting for LinkedIn browser", source="linkedin", found=found)
        if not pending:
            return False
        time.sleep(8)
    stats["linkedin"] = {**(stats.get("linkedin") or {}),
                         "notes": ["browser worker did not pick up in time — searches stay queued; "
                                   "start `python -m worker run` and they will still fill this list"]}
    _heartbeat(run, stats, phase="LinkedIn still queued", source="linkedin")
    return False


def _wait_worker_sources(run, stats, timeout_sec=20 * 60):
    """Wait until the Chromium worker marks every public source done (or Stop)."""
    import time
    names = [k for k, v in stats.items()
             if isinstance(v, dict) and v.get("worker") in ("pending", "running")]
    if not names:
        return False
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _stop_requested(run.id):
            return True
        db.session.refresh(run)
        stats.clear()
        stats.update(run.stats or {})
        pending = [k for k, v in stats.items()
                   if isinstance(v, dict) and v.get("worker") in ("pending", "running")]
        found = sum(int((stats.get(n) or {}).get("found") or 0) for n in names)
        phase = ("waiting for Chromium to open public sources"
                 if any((stats.get(n) or {}).get("worker") == "pending" for n in names)
                 else "Chromium is browsing public sources")
        _heartbeat(run, stats, phase=phase, found=found)
        if not pending:
            return False
        time.sleep(8)
    for n in names:
        row = stats.get(n) or {}
        if row.get("worker") in ("pending", "running"):
            row["notes"] = list(row.get("notes") or [])[:4] + [
                "browser worker did not finish this source — start `python -m worker run`"]
            row["worker"] = "timeout"
            stats[n] = row
    _heartbeat(run, stats, phase="public sources still queued")
    return False


def claim_browser_job():
    """Hand one unfinished public source to the Chromium worker.

    When several roles are discovering at once they share one browser, so we
    round-robin: pick the active run that has finished the fewest sources so far.
    """
    cfg = current_app.config
    rows = (WebSourcingRun.query.filter(WebSourcingRun.status.in_(("pending", "running")))
            .order_by(WebSourcingRun.created_at.asc()).all())
    choices = []
    for run in rows:
        if not run.queries_json or is_paused(run.role):
            continue
        stats = dict(run.stats or {})
        pending = [k for k, v in stats.items()
                   if isinstance(v, dict) and v.get("worker") == "pending"]
        if not pending:
            continue
        done = sum(1 for v in stats.values() if isinstance(v, dict) and v.get("worker") == "done")
        choices.append((done, run.created_at, run, pending[0], stats))
    if not choices:
        return None
    _done, _created, run, k, stats = min(choices, key=lambda x: (x[0], x[1], x[2].id))
    stats[k] = {**stats[k], "worker": "running",
                "notes": ["Chromium is opening this source…"]}
    run.stats_json = _json_set(stats)
    if run.status == "pending":
        run.status = "running"
    db.session.commit()
    seeds = [u.strip() for u in (run.seed_urls or "").replace(",", "\n").splitlines() if u.strip()]
    return {
        "id": run.id,
        "role": run.role.slug,
        "title": run.role.title,
        "sources": [k],
        "queries": run.queries,
        "requirements": run.requirements,
        "criteria": run.criteria,
        "seeds": seeds,
        "cap": cfg["WEBSOURCING_MAX_PER_SOURCE"],
    }


def report_browser_source(run_id, source, candidates, notes=None, done=True):
    run = db.session.get(WebSourcingRun, run_id)
    if run is None:
        return 0
    n = ingest_worker_profiles(run.role, candidates, channel=source or "web")
    db.session.refresh(run)
    stats = dict(run.stats or {})
    row = dict(stats.get(source) or {"found": 0, "notes": []})
    if notes:
        row["notes"] = notes[:6]
    if done:
        row["worker"] = "done"
        if not row.get("found") and not row.get("notes"):
            row["notes"] = ["opened in Chromium — no public profiles parsed"]
    stats[source] = row
    run.stats_json = _json_set(stats)
    db.session.commit()
    return n


def ingest_worker_profiles(role, profiles, channel="linkedin"):
    """Copy worker-found people onto the Web Sourcing review list and AI-score them.

    Called from the LinkedIn worker's results POST so names appear on the Discover page
    as Chromium reports them. Scoring runs in a background thread so the worker is not blocked.
    """
    if not profiles:
        return 0
    run = (WebSourcingRun.query.filter_by(role_id=role.id)
           .filter(WebSourcingRun.status.in_(("pending", "running", "stopping")))
           .order_by(WebSourcingRun.created_at.desc()).first())
    if run is None:
        run = (WebSourcingRun.query.filter_by(role_id=role.id)
               .order_by(WebSourcingRun.created_at.desc()).first())
    if run is None:
        run = WebSourcingRun(role=role, status="running", sources=channel)
        db.session.add(run)
        db.session.flush()
    crit = run.criteria or {}
    req = run.requirements or apply_criteria({
        "skills": _split_list(crit.get("tools")),
        "titles": [crit.get("profile") or role.title],
        "seniority": "any", "languages": _split_list(crit.get("languages")),
        "location": crit.get("location") or role.location or "",
        "industries": [], "queries": {}, "min_years": crit.get("min_years") or 0,
    }, crit)
    if not run.requirements_json:
        run.requirements_json = _json_set(req)
    new_ids = []
    for p in profiles:
        ev = [evidence(
            f"LinkedIn people-search result"
            + (f": {(p.get('headline') or '')[:120]}" if p.get("headline") else ""),
            LIKELY, p.get("profile_url"))]
        cand = candidate(
            channel, p.get("profile_url"), p.get("full_name") or "",
            headline=p.get("headline"), location=p.get("location"), company=p.get("company"),
            evidence_items=ev, raw={"via": "linkedin-worker"})
        if not cand:
            continue
        row = _upsert(run, role, cand)
        if row is None:
            continue
        if row.match_score is None:
            new_ids.append(row.id)
    stats = dict(run.stats or {})
    li = dict(stats.get(channel) or {"found": 0, "notes": []})
    li["found"] = int(li.get("found") or 0) + len(new_ids)
    li["notes"] = ["Chromium reported profiles — AI is scoring them"]
    stats[channel] = li
    run.stats_json = _json_set(stats)
    db.session.commit()
    if new_ids:
        app = current_app._get_current_object()
        if current_app.config.get("PROCESS_ASYNC", True) and not current_app.config.get("TESTING"):
            threading.Thread(target=_score_web_ids, args=(app, new_ids, req), daemon=True,
                             name="websourcing-score-linkedin").start()
        else:
            _score_web_ids(app, new_ids, req)
    return len(new_ids)


def _score_web_ids(app, ids, req):
    with app.app_context():
        for rid in ids:
            row = db.session.get(WebCandidate, rid)
            if not row or row.match_score is not None:
                continue
            cand = {
                "source": (row.sources[0]["source"] if row.sources else "linkedin"),
                "profile_url": row.profile_url, "full_name": row.full_name,
                "headline": row.headline, "location": row.location, "company": row.company,
                "email": row.email, "website": row.website, "skills": row.skills,
                "languages": row.languages, "projects": row.projects, "evidence": row.evidence,
            }
            try:
                verdict = score_candidate(row.role, req, cand)
            except Exception as exc:
                log.warning("web-candidate score failed for %s: %s", row.profile_url, exc)
                continue
            row.match_score = verdict["score"]
            row.confidence = verdict["confidence"]
            row.matched_json = _json_set(verdict.get("matched") or [])
            row.missing_json = _json_set(verdict.get("missing") or [])
            row.match_reason = (verdict.get("reason") or "")[:1000]
            db.session.commit()


def _dedupe(cands):
    """Merge the same person seen on several sources (URL, then public email, then name+location)."""
    by_key, order = {}, []
    for c in cands:
        keys = [dedupe_key(c["profile_url"])]
        if c["email"]:
            keys.append("email:" + c["email"])
        nm = c["full_name"].lower().strip()
        if len(nm.split()) >= 2:
            keys.append("name:" + nm + "|" + (c["location"] or "").lower().split(",")[0].strip())
        target = next((by_key[k] for k in keys if k in by_key), None)
        if target is None:
            c["_keys"] = keys
            order.append(c)
            for k in keys:
                by_key[k] = c
            continue
        # merge: keep first-seen primary URL, union everything else
        target["skills"] = sorted(set(target["skills"]) | set(c["skills"]))[:40]
        target["languages"] = list(dict.fromkeys(target["languages"] + c["languages"]))[:10]
        target["projects"] = (target["projects"] + c["projects"])[:8]
        target["evidence"] = (target["evidence"] + c["evidence"])[:25]
        for f in ("headline", "location", "company", "email", "website"):
            target[f] = target[f] or c[f]
        target.setdefault("also", []).append({"source": c["source"], "url": c["profile_url"]})
        for k in keys:
            by_key.setdefault(k, target)
    return order


def _upsert(run, role, cand):
    key = dedupe_key(cand["profile_url"], cand["email"])
    row = WebCandidate.query.filter_by(role_id=role.id, dedupe_key=key).first()
    if row and row.status in ("dismissed", "moved"):
        return None                       # the recruiter already decided on this person
    if row is None:
        row = WebCandidate(role=role, run=run, dedupe_key=key, full_name=cand["full_name"],
                           profile_url=cand["profile_url"])
        db.session.add(row)
    row.run = run
    row.full_name = cand["full_name"]
    row.headline = cand["headline"]
    row.location = cand["location"]
    row.company = cand["company"]
    row.email = cand["email"]
    row.website = cand["website"]
    sources, seen = [], set()
    for s in [{"source": cand["source"], "url": cand["profile_url"]}] + cand.get("also", []):
        if (s["source"], s["url"]) not in seen:
            seen.add((s["source"], s["url"]))
            sources.append(s)
    row.sources_json = _json_set(sources)
    row.skills_json = _json_set(cand["skills"])
    row.languages_json = _json_set(cand["languages"])
    row.projects_json = _json_set(cand["projects"])
    row.evidence_json = _json_set(cand["evidence"])
    row.raw_json = _json_set(cand["raw"])
    db.session.flush()
    return row


# ------------------------------------------------------------------ external ingestion (Claude browser sessions)

def ingest_external(role, raw_candidates, source="claude-browser", criteria=None):
    """Accept candidates found by an external agent (a Claude session with a real browser,
    launched from the Launch-in-Claude button). The app stays the referee: it normalizes,
    dedupes against everything already discovered, and scores with the same evidence rules.
    Returns (run, received, unique, stored)."""
    from .base import CONFIRMED, LIKELY, UNKNOWN, candidate as make_candidate
    cands = []
    for p in raw_candidates[:200]:
        if not isinstance(p, dict):
            continue
        ev = []
        for e in (p.get("evidence") or [])[:25]:
            if isinstance(e, dict) and e.get("claim"):
                label = str(e.get("label", "")).capitalize()
                ev.append({"claim": str(e["claim"])[:300],
                           "label": label if label in (CONFIRMED, LIKELY, UNKNOWN) else UNKNOWN,
                           "url": str(e.get("url") or "")[:400]})
        c = make_candidate(source, p.get("profile_url") or p.get("website"), p.get("full_name") or "",
                           headline=p.get("headline") or p.get("title"), location=p.get("location"),
                           company=p.get("company"), email=p.get("email"), website=p.get("website"),
                           skills=p.get("skills") if isinstance(p.get("skills"), list) else None,
                           languages=p.get("languages") if isinstance(p.get("languages"), list) else None,
                           projects=p.get("projects") if isinstance(p.get("projects"), list) else None,
                           evidence_items=ev, raw={"via": source})
        if c:
            cands.append(c)
    run = WebSourcingRun(role=role, status="running", sources=source,
                         criteria_json=_json_set({k: v for k, v in (criteria or {}).items() if v}))
    db.session.add(run)
    db.session.commit()
    crit = run.criteria or _last_criteria(role)
    req = apply_criteria(extract_requirements(role, crit), crit)
    run.requirements_json = _json_set(req)
    merged = _dedupe(cands)
    stored = 0
    for cand in merged:
        row = _upsert(run, role, cand)
        if row is None:
            continue
        verdict = score_candidate(role, req, cand)
        row.match_score = verdict["score"]
        row.confidence = verdict["confidence"]
        row.matched_json = _json_set(verdict.get("matched") or [])
        row.missing_json = _json_set(verdict.get("missing") or [])
        row.match_reason = (verdict.get("reason") or "")[:1000]
        stored += 1
    run.stats_json = _json_set({source: {"found": len(cands), "notes": []},
                                "_total": {"found": len(cands), "unique": len(merged), "scored": stored}})
    run.status = "done"
    run.finished_at = utcnow()
    db.session.commit()
    return run, len(raw_candidates), len(merged), stored


def _last_criteria(role):
    last = (WebSourcingRun.query.filter_by(role_id=role.id)
            .filter(WebSourcingRun.criteria_json.isnot(None))
            .order_by(WebSourcingRun.created_at.desc()).first())
    return last.criteria if last else {}


def launch_prompt(role):
    """The ready-to-run prompt for a Claude session with browser access (Launch in Claude)."""
    cfg = current_app.config
    base = cfg["PUBLIC_BASE_URL"]
    sites = "\n".join(f"   - {s}" for s in BROWSER_SITES)
    return f"""You are a candidate-sourcing assistant for Tweezgroup with access to a web browser. Goal: discover candidates for “{role.title}” on PUBLIC web pages only, and feed them into the recruiting app.

App: {base} — API key: {cfg['API_KEY']} (send it as the X-API-Key header).

1) Fetch the role brief (JD, requirements, targeting filters):
   GET {base}/api/v1/websourcing/roles/{role.slug}/brief
2) Using the browser, search for matching people on public sources — work through this site list (all LinkedIn-free), skipping any that a normal visitor cannot access:
{sites}
   STRICT RULES — never break these: do not log in to anything; do not bypass CAPTCHAs, paywalls, rate limits or anti-bot protections; skip platforms that prohibit automated access (LinkedIn included); collect only professional information the person published themselves; record an email only if the page displays it; never invent or guess a value — leave it out instead.
3) For each candidate record: full_name, headline, location, company, skills[], languages[], email, website, profile_url, and evidence[] — a list of {{"claim": "what the page says", "label": "Confirmed"|"Likely"|"Unknown", "url": "the page"}}.
4) Submit them (in batches of up to 20):
   POST {base}/api/v1/websourcing/candidates
   JSON body: {{"role": "{role.slug}", "source": "claude-browser", "candidates": [ ... ]}}
   The app deduplicates against previous discoveries and scores each candidate against the job description — you don't score.
5) Stop after ~25 good candidates or when sources run dry. Do not contact anyone — outreach is the recruiter's click, inside the app. Finish by telling the recruiter to review the results at {base}/admin/web-sourcing/{role.slug}."""


# ------------------------------------------------------------------ outreach email

EMAIL_SUBJECT = "{job_title} at TweezGroup"

EMAIL_TEMPLATE = """Hi {first_name},

I came across your profile while sourcing candidates for {job_title} at TweezGroup, and your experience in {relevant_experience} caught my attention.

TweezGroup is a fully remote eCommerce holding company building and scaling consumer brands across international markets. We’re currently looking for someone with your background to join us as {job_title}.

Based on your experience with {specific_skill_or_achievement}, I believe there could be a strong fit.

You can take a look at the position and apply here:
{jobs_url}

If the opportunity sounds interesting, I’d be happy to connect and discuss it further.

Best regards,
{sender}"""

PERSONALIZE_PROMPT = """You are writing ONE line of personalisation for a recruiting email, using only
the verified public evidence below. Never invent, exaggerate or guess — pick only what the evidence
actually shows.

ROLE: {title}
CANDIDATE: {name} — {headline}
SKILLS SEEN: {skills}
EVIDENCE:
{evidence}

Return ONLY a JSON object:
  "relevant_experience": a short noun phrase (max 8 words) naming the candidate's most relevant
      area of experience for this role, e.g. "backend development with Python and Flask"
  "specific_skill_or_achievement": one concrete, specific thing from the evidence (max 12 words),
      e.g. "your open-source Flask e-commerce backend (340 stars)" — prefer a named project,
      top-answerer status, or a concrete accomplishment over a generic skill."""


def build_email(cand):
    """Generate the recruiter outreach draft for a discovered candidate. Nothing is sent here —
    the recruiter sees, edits and explicitly sends the draft."""
    cfg = current_app.config
    role = cand.role
    ev_lines = "\n".join(f"- [{e['label']}] {e['claim']}" for e in cand.evidence[:15])
    data = claude_json(PERSONALIZE_PROMPT.format(
        title=role.title, name=cand.full_name, headline=cand.headline or "-",
        skills=", ".join(cand.skills) or "-", evidence=ev_lines or "- (none)"), max_tokens=300) or {}
    top_proj = next((p for p in cand.projects if p.get("name")), None)
    relevant = (data.get("relevant_experience") or "").strip() \
        or (", ".join(cand.skills[:3]) if cand.skills else (cand.headline or "this field"))
    specific = (data.get("specific_skill_or_achievement") or "").strip() \
        or (f"your public project “{top_proj['name']}”" if top_proj
            else (cand.skills[0] if cand.skills else "your public work"))
    body = EMAIL_TEMPLATE.format(
        first_name=cand.full_name.split()[0], job_title=role.title,
        relevant_experience=relevant, specific_skill_or_achievement=specific,
        jobs_url=f"{cfg['PUBLIC_BASE_URL']}/jobs",
        sender=cfg.get("SOURCING_SENDER_NAME") or "Mehdi")
    return EMAIL_SUBJECT.format(job_title=role.title), body


def send_email(cand, subject, body, actor="admin"):
    """Send the recruiter-approved email through the app's mail backend (Mehdi's Gmail in
    production). Returns (ok, error)."""
    from .. import mailer
    ok, err = mailer.send(cand.email, subject, body)
    if ok:
        cand.emailed_at = utcnow()
        if cand.status == "new":
            cand.status = "shortlisted"     # emailing implies the recruiter picked them
        db.session.commit()
    return ok, err


# ------------------------------------------------------------------ recruiter actions

def move_to_outreach(cand, actor="admin"):
    """Recruiter-approved: hand the candidate to the existing outreach pipeline (no email
    is sent unless the recruiter uses the outreach screens to send one)."""
    from .. import sourcing
    profile, _ = sourcing.add_manual(
        cand.role, cand.full_name, profile_url=cand.website or cand.profile_url,
        email=cand.email, channel="manual", headline=cand.headline, location=cand.location,
        actor=actor, send_email=False)
    profile.about = (profile.about or "") or cand.match_reason
    cand.status = "moved"
    cand.sourced_profile_id = profile.id
    db.session.commit()
    return profile


def filters_from_args(args):
    """Shared filter parsing for the results views."""
    return {
        "q": (args.get("q") or "").strip().lower(),
        "min_score": int(args.get("min_score") or 0),
        "source": (args.get("source") or "").strip(),
        "status": (args.get("status") or "").strip(),
        "location": (args.get("location") or "").strip().lower(),
        "skill": (args.get("skill") or "").strip().lower(),
        "confidence": (args.get("confidence") or "").strip(),
    }


def apply_filters(query_, f):
    if f["status"]:
        query_ = query_.filter(WebCandidate.status == f["status"])
    if f["min_score"]:
        query_ = query_.filter(WebCandidate.match_score >= f["min_score"])
    if f["confidence"]:
        query_ = query_.filter(WebCandidate.confidence == f["confidence"])
    if f["location"]:
        query_ = query_.filter(WebCandidate.location.ilike(f"%{f['location']}%"))
    rows = query_.order_by(WebCandidate.match_score.desc().nullslast(),
                           WebCandidate.discovered_at.desc()).limit(500).all()
    if f["source"]:
        rows = [r for r in rows if any(s["source"] == f["source"] for s in r.sources)]
    if f["skill"]:
        rows = [r for r in rows if f["skill"] in r.skills]
    if f["q"]:
        rows = [r for r in rows if f["q"] in (r.full_name or "").lower()
                or f["q"] in (r.headline or "").lower() or f["q"] in (r.company or "").lower()]
    return rows
