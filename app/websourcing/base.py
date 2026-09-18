"""Shared plumbing for the public-web sourcing adapters.

Ground rules, enforced here so no adapter can break them:
  * GET requests only, no cookies, no sessions, no authentication walls — public pages and
    official public APIs only.
  * An honest, identifying User-Agent (who we are + a contact), never a browser impersonation.
  * robots.txt is checked (and cached) before any HTML page fetch; disallowed URLs are skipped
    and counted, never worked around.
  * A per-domain minimum delay between requests; HTTP 429/403 marks the whole domain as
    backing off for the rest of the run — we never retry through a rate limit.
  * Every adapter returns the same normalized candidate dict (see `candidate()`), and every
    claim about a person carries an evidence item labelled Confirmed / Likely / Unknown with
    the public URL it came from. Missing information stays missing.
"""
import logging
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from flask import current_app

log = logging.getLogger(__name__)

CONFIRMED, LIKELY, UNKNOWN = "Confirmed", "Likely", "Unknown"

# ------------------------------------------------------------------ polite HTTP client

_lock = threading.Lock()
_last_hit = {}        # domain -> monotonic time of last request
_backoff = {}         # domain -> reason ("429 rate limited", "403 forbidden")
_robots = {}          # domain -> RobotFileParser or None (no robots.txt)

# Official public API hosts: JSON endpoints meant for programmatic access — the API's own
# rate-limit headers govern them, robots.txt does not apply.
API_HOSTS = ("api.github.com", "gitlab.com", "api.stackexchange.com", "hn.algolia.com",
             "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "yc-oss.github.io")


def reset_state():
    """Fresh per-run state (also keeps tests independent)."""
    with _lock:
        _last_hit.clear()
        _backoff.clear()
        _robots.clear()


def _ua():
    return current_app.config["WEBSOURCING_BOT_UA"]


def _robots_allows(url):
    """True if robots.txt allows us to fetch this URL (API hosts are exempt, see above)."""
    parts = urlparse(url)
    domain = (parts.hostname or "").lower()
    if any(domain == h or domain.endswith("." + h) for h in API_HOSTS):
        return True
    if domain.startswith(("127.", "localhost")):  # dev / mock server
        return True
    with _lock:
        rp = _robots.get(domain, "unread")
    if rp == "unread":
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(f"{parts.scheme}://{parts.netloc}/robots.txt",
                             headers={"User-Agent": _ua()}, timeout=10)
            if r.status_code >= 500:
                rp = None            # can't read it -> be conservative below? No: absent == allowed
            elif r.status_code >= 400:
                rp = None            # no robots.txt -> everything allowed
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None
        with _lock:
            _robots[domain] = rp
    if rp is None:
        return True
    return rp.can_fetch(_ua(), url) or rp.can_fetch("*", url)


def polite_get(url, params=None, headers=None, timeout=20, expect_json=False, check_robots=None):
    """GET with identification, robots.txt respect, per-domain pacing and rate-limit backoff.

    Returns (response_or_parsed_json, skip_reason). Exactly one of the two is None.
    A skip is a *policy* outcome (robots disallow, domain backing off), not an error.
    """
    cfg = current_app.config
    parts = urlparse(url)
    domain = (parts.hostname or "").lower()

    with _lock:
        reason = _backoff.get(domain)
    if reason:
        return None, f"{domain}: skipped ({reason})"

    is_api = any(domain == h or domain.endswith("." + h) for h in API_HOSTS)
    if check_robots is None:
        check_robots = not is_api
    if check_robots and not _robots_allows(url):
        return None, f"{domain}: disallowed by robots.txt"

    # pace: one request per WEBSOURCING_FETCH_DELAY seconds per domain (APIs: half of that)
    delay = cfg["WEBSOURCING_FETCH_DELAY"] * (0.5 if is_api else 1.0)
    with _lock:
        wait = max(0.0, _last_hit.get(domain, 0) + delay - time.monotonic())
    if wait:
        time.sleep(wait)
    with _lock:
        _last_hit[domain] = time.monotonic()

    h = {"User-Agent": _ua(), "Accept": "application/json" if expect_json else
         "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, params=params, headers=h, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return None, f"{domain}: {type(exc).__name__}"
    if r.status_code in (429, 403):
        why = "rate limited (429)" if r.status_code == 429 else "access refused (403)"
        with _lock:
            _backoff[domain] = why
        return None, f"{domain}: {why} — backing off for the rest of the run"
    if r.status_code >= 400:
        return None, f"{domain}: HTTP {r.status_code}"
    if expect_json:
        try:
            return r.json(), None
        except ValueError:
            return None, f"{domain}: invalid JSON"
    return r, None


# ------------------------------------------------------------------ normalized candidate

def clean_url(url):
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    u = urlparse(url)
    if not u.hostname:
        return None
    return f"https://{u.hostname}{u.path}".rstrip("/")[:400]


def dedupe_key(profile_url, email=None):
    if email:
        return "email:" + email.strip().lower()
    u = urlparse(profile_url or "")
    host = (u.hostname or "").lower().removeprefix("www.")
    return f"{host}{u.path}".rstrip("/").lower()[:400]


def evidence(claim, label, url):
    return {"claim": str(claim)[:300], "label": label, "url": (url or "")[:400]}


def candidate(source, profile_url, full_name, *, headline=None, location=None, company=None,
              email=None, website=None, skills=None, languages=None, projects=None,
              evidence_items=None, raw=None):
    """The one normalized structure every adapter returns."""
    profile_url = clean_url(profile_url)
    if not profile_url or not (full_name or "").strip():
        return None
    return {
        "source": source,
        "profile_url": profile_url,
        "full_name": full_name.strip()[:200],
        "headline": (headline or "").strip()[:400] or None,
        "location": (location or "").strip()[:200] or None,
        "company": (company or "").strip()[:200] or None,
        "email": (email or "").strip().lower()[:200] or None,
        "website": clean_url(website),
        "skills": sorted({s.strip().lower() for s in (skills or []) if s and s.strip()})[:40],
        "languages": [l.strip() for l in (languages or []) if l and l.strip()][:10],
        "projects": (projects or [])[:8],
        "evidence": (evidence_items or [])[:25],
        "raw": raw or {},
    }


class SourceAdapter:
    """Base class: one independent adapter per public source.

    name         short key used in config/UI
    label        human name
    reliability  how much structured, verifiable data this source gives (high/medium/low);
                 feeds the confidence rating, never the score itself
    needs_seeds  True when the adapter only works on recruiter-supplied URLs
    """
    name = "base"
    label = "Base"
    reliability = "medium"
    needs_seeds = False

    def search(self, role, req, queries, seeds, cap):
        """Yield normalized candidate dicts (see `candidate()`). Must never raise for
        policy skips — report them through self.skips instead."""
        raise NotImplementedError

    def __init__(self):
        self.skips = []   # human-readable reasons: robots.txt, rate limits, missing config

    def skip(self, reason):
        if reason and reason not in self.skips:
            self.skips.append(reason)
            log.info("websourcing[%s]: %s", self.name, reason)
