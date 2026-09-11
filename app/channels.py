"""Non-LinkedIn sourcing channels.

  github      official REST search API — the app runs the search itself (no browser)
  behance, artstation, kaggle, contra, torre, upwork …
              no public people-search API: the recruiter searches on the platform, pastes profile URLs
              in the Searches tab, and the app enriches each one from the public page (name, headline,
              location from Open Graph / JSON-LD / the GitHub API), pre-scores it and generates the link.

Messaging on these platforms is done by hand (the profile page shows the text to copy + "Link sent"),
or by email when an address is known ("Email the link" button).
"""
import html as _html
import json
import logging
import re
from urllib.parse import urlparse

import requests
from flask import current_app

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "en,fr;q=0.8"}

DOMAINS = {
    "linkedin.com": "linkedin", "github.com": "github", "behance.net": "behance", "artstation.com": "artstation",
    "kaggle.com": "kaggle", "contra.com": "contra", "torre.ai": "torre", "torre.co": "torre", "upwork.com": "upwork",
    "dribbble.com": "manual", "malt.fr": "manual", "malt.com": "manual",
}


def channel_for_url(url):
    host = (urlparse(url).hostname or "").lower()
    for d, ch in DOMAINS.items():
        if host == d or host.endswith("." + d):
            return ch
    return "manual"


def clean_profile_url(url):
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    u = urlparse(url)
    return f"https://{u.hostname}{u.path}".rstrip("/")[:400]


# ------------------------------------------------------------------------------------ GitHub (API)

GH = "https://api.github.com"


def _gh_headers():
    h = {"Accept": "application/vnd.github+json", **UA}
    tok = current_app.config.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def github_search(query, location=None, max_results=50):
    """GitHub user search. `query` uses the official qualifiers, e.g.
    'language:python followers:>20' or 'data scientist in:bio'. Returns profile dicts."""
    q = query.strip()
    if location and "location:" not in q:
        q += f' location:"{location}"'
    if "type:" not in q:
        q += " type:user"
    out, seen, page = [], set(), 1
    while len(out) < max_results and page <= 10:
        per_page = min(50, max_results - len(out))
        r = requests.get(f"{GH}/search/users", params={"q": q, "per_page": per_page, "page": page, "sort": "followers"},
                         headers=_gh_headers(), timeout=30)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise RuntimeError("GitHub rate limit reached — set GITHUB_TOKEN or retry later")
        r.raise_for_status()
        items = [it for it in r.json().get("items", []) if it.get("login") not in seen]
        if not items:
            break
        for it in items:
            seen.add(it["login"])
            out.append(github_profile(it["login"]) or {"profile_url": it["html_url"], "full_name": it["login"]})
            if len(out) >= max_results:
                break
        if len(r.json().get("items", [])) < per_page:
            break
        page += 1
    return out


def github_profile(login):
    r = requests.get(f"{GH}/users/{login}", headers=_gh_headers(), timeout=30)
    if r.status_code != 200:
        return None
    u = r.json()
    langs = _github_top_languages(login)
    headline = " · ".join(x for x in [u.get("bio"), f"{u.get('public_repos', 0)} repos",
                                     f"{u.get('followers', 0)} followers", langs] if x)
    return {"profile_url": u.get("html_url"), "full_name": (u.get("name") or u.get("login") or "")[:200],
            "headline": headline[:400], "location": (u.get("location") or "")[:200] or None,
            "company": (u.get("company") or "")[:200] or None, "email": u.get("email"),
            "about": " | ".join(x for x in [u.get("blog"), u.get("twitter_username") and f"@{u['twitter_username']}"] if x)
            or None}


def _github_top_languages(login):
    try:
        r = requests.get(f"{GH}/users/{login}/repos", params={"per_page": 30, "sort": "pushed"},
                         headers=_gh_headers(), timeout=30)
        if r.status_code != 200:
            return ""
        counts = {}
        for repo in r.json():
            if repo.get("language"):
                counts[repo["language"]] = counts.get(repo["language"], 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        return ", ".join(k for k, _ in top)
    except Exception:
        return ""


# ------------------------------------------------------------------------------------ URL enrichment

META_RE = re.compile(r'<meta\s+[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']', re.I)
META_RE2 = re.compile(r'<meta\s+[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)


def enrich_url(url):
    """Best-effort public-profile facts for any URL: {profile_url, channel, full_name, headline, location, about}."""
    url = clean_profile_url(url)
    if not url:
        return None
    ch = channel_for_url(url)
    if ch == "github":
        m = re.match(r"https://github\.com/([^/]+)/?$", url)
        if m:
            p = github_profile(m.group(1))
            if p:
                p["channel"] = "github"
                return p
    info = {"profile_url": url, "channel": ch, "full_name": None, "headline": None, "location": None, "about": None}
    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        page = r.text if r.status_code < 400 else ""
    except Exception as exc:
        log.info("enrich %s failed: %s", url, exc)
        page = ""
    meta = {}
    for k, v in META_RE.findall(page):
        meta.setdefault(k.lower(), _html.unescape(v))
    for v, k in META_RE2.findall(page):
        meta.setdefault(k.lower(), _html.unescape(v))
    title = meta.get("og:title") or meta.get("twitter:title") or ""
    if not title:
        m = TITLE_RE.search(page)
        title = _html.unescape(m.group(1).strip()) if m else ""
    desc = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""
    # JSON-LD Person, when present (Behance / some portfolios)
    for block in LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and obj.get("@type") in ("Person", "ProfilePage"):
                person = obj.get("mainEntity", obj)
                info["full_name"] = person.get("name") or info["full_name"]
                info["headline"] = person.get("jobTitle") or person.get("description") or info["headline"]
                addr = person.get("address") or person.get("homeLocation")
                if isinstance(addr, dict):
                    info["location"] = addr.get("addressLocality") or addr.get("name")
                elif isinstance(addr, str):
                    info["location"] = addr
    name, headline, location = _split_title(title, ch)
    info["full_name"] = info["full_name"] or name
    info["headline"] = (info["headline"] or headline or desc or "")[:400] or None
    info["location"] = (info["location"] or location or "")[:200] or None
    info["about"] = (desc[:2000] if desc and desc != info["headline"] else None)
    if not info["full_name"]:
        slug = url.rstrip("/").split("/")[-1]
        info["full_name"] = re.sub(r"[-_.]+", " ", slug).title()[:200]
    return info


def _split_title(title, ch):
    """'Sara Benali - Senior 3D Artist | ArtStation' -> (name, headline, location)."""
    t = re.sub(r"\s*[|·•]\s*(Behance|ArtStation|Kaggle|Contra|Torre|Upwork|Dribbble|GitHub)\s*$", "", title, flags=re.I)
    t = re.sub(r"^(User|Profile)\s*[:\-]\s*", "", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+|\s+on\s+(?=Behance|ArtStation|Contra|Kaggle)", t) if p and p.strip()]
    if not parts:
        return None, None, None
    name = parts[0][:200]
    headline = " — ".join(parts[1:])[:400] if len(parts) > 1 else None
    location = None
    if ch == "kaggle" and headline:
        headline = headline.replace("Kaggle", "").strip(" —") or None
    return name, headline, location


# ------------------------------------------------------------------------------------ dispatcher

def run_search(search):
    """Execute an API-channel search inline. Returns profiles or raises."""
    if search.channel == "github":
        return github_search(search.keywords, search.location, search.max_results)
    raise RuntimeError(f"{search.channel}: no search API — use “Import profile URLs” for this channel")


def import_urls(urls):
    """Enrich a list of pasted URLs (deduplicated, in order)."""
    seen, out = set(), []
    for raw in urls:
        u = clean_profile_url(raw)
        if not u or u in seen:
            continue
        seen.add(u)
        info = enrich_url(u)
        if info:
            out.append(info)
    return out
