"""LinkedIn profile links: normalise, verify and find them, so outreach always opens the right person.

Why this exists: sourced records were sometimes saved under a URL that is not the person's
LinkedIn profile (a portfolio/GitHub link chosen over the LinkedIn one, a mutual-connection link
scraped from a search card, a search-results URL). Every reach-out screen now shows a check:

  ok       canonical https://www.linkedin.com/in/<slug>, and the slug contains the person's name
  check    a real /in/ link, but it can't be tied to the name (internal ACoA… id, or the slug
           names someone else — typical of the mutual-connection bug). Open it and confirm.
  invalid  a linkedin.com link that is not a profile (search, company, feed, pub/dir…)
  missing  no LinkedIn link at all — a people-search link is offered instead
"""
import re
import unicodedata
from urllib.parse import quote_plus, unquote, urlparse

_PROFILE_PREFIXES = ("in",)               # /in/<slug> — the only personal-profile directory
# LinkedIn decorations scraped along with the name: "• 2nd", "· 3rd+", "(He/Him)", "1st degree"…
_NAME_JUNK = re.compile(
    r"\s*[•·|\-–]\s*(1st|2nd|3rd\+?|3e\+?|2e|1er)\b.*$|\s*\b(1st|2nd|3rd\+?)\s*degree.*$|\s*\((?:he|she|they)/[^)]*\)",
    re.I)


def clean_name(name):
    """'Asma ADJOURI • 2nd' -> 'Asma ADJOURI'. Leaves ordinary names untouched."""
    n = _NAME_JUNK.sub("", (name or "").strip())
    n = re.sub(r"\s+", " ", n).strip(" •·|-–")
    return n or (name or "").strip()


def _fold(text):
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in t if not unicodedata.combining(ch)).lower()


def _with_scheme(url):
    raw = (url or "").strip()
    if raw and not re.match(r"^[a-z]+://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    return raw


def is_linkedin(url):
    host = (urlparse(_with_scheme(url)).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in"


def normalize(url):
    """Canonical profile URL 'https://www.linkedin.com/in/<slug>' or None when it isn't a profile.

    Accepts http/https, country/mobile sub-domains (fr., m.), query strings, trailing sections
    (/details/experience, /recent-activity/…, /overlay/…) and missing scheme."""
    raw = _with_scheme(url)
    if not raw:
        return None
    u = urlparse(raw)
    if not is_linkedin(raw) or (u.hostname or "").lower() == "lnkd.in":
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() not in _PROFILE_PREFIXES:
        return None
    slug = parts[1].strip()
    if not slug or slug.lower() in ("me", "edit"):
        return None
    # Case is kept: vanity slugs are case-insensitive, but internal ids (ACoA…) are not.
    return f"https://www.linkedin.com/in/{slug}"


def slug_of(url):
    n = normalize(url)
    return n.rsplit("/", 1)[-1] if n else None


def check(url, full_name=None):
    """Verdict for a (possibly missing) LinkedIn URL. Returns a dict for templates:
    {state, url, slug, reason, search_url}."""
    name = clean_name(full_name or "")
    search_url = people_search_url(name)
    if not url or not is_linkedin(url):
        return {"state": "missing", "url": None, "slug": None, "search_url": search_url,
                "reason": "No LinkedIn profile link on record — find the person with the search link, "
                          "then paste their /in/ URL."}
    canon = normalize(url)
    if not canon:
        return {"state": "invalid", "url": None, "slug": None, "search_url": search_url,
                "reason": "This LinkedIn link is not a personal profile (search, company or feed page)."}
    slug = canon.rsplit("/", 1)[-1]
    folded_slug = _fold(unquote(slug))
    if re.fullmatch(r"acoa[a-z0-9_-]{10,}", folded_slug):
        return {"state": "check", "url": canon, "slug": slug, "search_url": search_url,
                "reason": "Internal LinkedIn id (from search results) — it opens the profile, but the name "
                          "can't be verified from the link. Open it once and confirm."}
    tokens = [t for t in re.split(r"[^a-z0-9]+", _fold(name)) if len(t) >= 3]
    if not tokens:
        return {"state": "check", "url": canon, "slug": slug, "search_url": search_url,
                "reason": "No usable name to compare with the link."}
    compact = re.sub(r"[^a-z0-9]", "", folded_slug)
    if any(t in compact for t in tokens):
        return {"state": "ok", "url": canon, "slug": slug, "search_url": search_url,
                "reason": "Profile link matches the candidate's name."}
    return {"state": "check", "url": canon, "slug": slug, "search_url": search_url,
            "reason": "The link doesn't contain the candidate's name — it may belong to someone else "
                      "(e.g. a mutual connection on the search card). Open it and confirm."}


def people_search_url(name, extra=None):
    q = " ".join(x for x in (clean_name(name or ""), extra or "") if x).strip()
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(q)}" if q else None


def find_in(*urls):
    """First LinkedIn profile URL among the given URLs / lists of {'url': ...} dicts."""
    for u in urls:
        if not u:
            continue
        if isinstance(u, (list, tuple)):
            for item in u:
                got = find_in(item.get("url") if isinstance(item, dict) else item)
                if got:
                    return got
            continue
        got = normalize(u)
        if got:
            return got
    return None
