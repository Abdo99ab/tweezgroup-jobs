"""Web-pages adapter — personal portfolios, company team/about pages and public directories
(Humetric or any similar directory the recruiter has access to as a normal visitor).

The recruiter pastes URLs (or another adapter discovers company sites); each page is fetched
once with the identifying bot UA, only if robots.txt allows it, and people are extracted from
what the page *publishes*: JSON-LD Person/schema.org markup, Open Graph tags, and — when the
Claude key is set — a careful extraction pass over the visible text that is told to never
invent and to leave unknown fields empty.
"""
import json
import re

from .base import CONFIRMED, LIKELY, SourceAdapter, candidate, evidence, polite_get
from .llm import claude_json

TAG_STRIP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
TAGS = re.compile(r"<[^>]+>")
JSONLD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
OG = re.compile(r'<meta[^>]+property=["\']og:(\w+)["\'][^>]+content=["\']([^"\']+)["\']', re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

EXTRACT_PROMPT = """You are helping a recruiter read a PUBLIC web page (a personal portfolio,
a company team page, or a people directory). Extract only people the page itself presents,
with only the professional information the page actually states.

STRICT RULES: never invent or guess a value — leave it empty instead. Only include emails the
page displays. Skip anyone who is clearly not presented professionally.

PAGE URL: {url}
PAGE TEXT (truncated):
\"\"\"{text}\"\"\"

Return ONLY a JSON object: {{"people": [{{"full_name": "...", "title": "...", "company": "...",
"location": "...", "skills": ["..."], "languages": ["..."], "email": "", "profile_url": "",
"quote": "the exact page sentence that presents this person (max 160 chars)"}}]}} (max 12 people).
"profile_url" only if the page links a personal page for them; otherwise empty."""


def _visible_text(html):
    body = TAG_STRIP.sub(" ", html)
    body = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", body, flags=re.I)
    return re.sub(r"[ \t]+", " ", TAGS.sub(" ", body)).strip()


def _jsonld_people(html):
    people = []
    for m in JSONLD.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            for v in list(node.values()):
                if isinstance(v, list):
                    stack.extend(x for x in v if isinstance(x, dict))
            if node.get("@type") in ("Person", ["Person"]):
                people.append(node)
    return people


class WebPagesAdapter(SourceAdapter):
    name = "webpages"
    label = "Portfolios & company pages (browser)"
    reliability = "medium"
    needs_seeds = True
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        found = 0
        for url in list(dict.fromkeys(seeds))[:15]:
            if found >= cap:
                break
            if " " in url or any(b in url for b in ("greenhouse.io", "lever.co", "ashbyhq.com")):
                continue    # job-board seeds belong to the boards adapter
            for c in self.extract(url, source=self.name):
                if found >= cap:
                    break
                found += 1
                yield c

    def extract(self, url, source=None):
        """Fetch one public page (robots-checked) and yield the people it presents."""
        source = source or self.name
        r, skip = polite_get(url, check_robots=True)
        if skip:
            self.skip(skip)
            return
        html = r.text[:400_000]
        og = dict(OG.findall(html))
        yielded = set()

        # 1) structured data the page publishes on purpose
        for p in _jsonld_people(html):
            name = (p.get("name") or "").strip()
            if not name or name.lower() in yielded:
                continue
            yielded.add(name.lower())
            purl = p.get("url") or p.get("sameAs") or url
            if isinstance(purl, list):
                purl = purl[0] if purl else url
            ev = [evidence(f"Listed as a Person in the page's own structured data (schema.org)",
                           CONFIRMED, url)]
            if p.get("jobTitle"):
                ev.append(evidence(f"Title on the page: {p['jobTitle']}", CONFIRMED, url))
            c = candidate(source, purl, name,
                          headline=p.get("jobTitle") or p.get("description"),
                          location=(p.get("address") or {}).get("addressLocality")
                          if isinstance(p.get("address"), dict) else None,
                          company=(p.get("worksFor") or {}).get("name")
                          if isinstance(p.get("worksFor"), dict) else None,
                          email=(p.get("email") or "").removeprefix("mailto:") or None,
                          website=purl if purl != url else None,
                          skills=p.get("knowsAbout") if isinstance(p.get("knowsAbout"), list) else None,
                          evidence_items=ev, raw={"page": url, "via": "json-ld"})
            if c:
                yield c

        # 2) Claude extraction over the visible text (single person portfolio or a team grid)
        text = _visible_text(html)[:12_000]
        data = claude_json(EXTRACT_PROMPT.format(url=url, text=text)) if len(text) > 200 else None
        for p in (data or {}).get("people", []):
            name = (p.get("full_name") or "").strip()
            if not name or name.lower() in yielded:
                continue
            yielded.add(name.lower())
            page_email = (p.get("email") or "").strip()
            if page_email and page_email not in html:     # guard: only emails the page really shows
                page_email = ""
            ev = [evidence(p.get("quote") or f"Presented on {url}", CONFIRMED, url)]
            if p.get("title"):
                ev.append(evidence(f"Title on the page: {p['title']}", CONFIRMED, url))
            if p.get("location"):
                ev.append(evidence(f"Location on the page: {p['location']}", LIKELY, url))
            c = candidate(source, p.get("profile_url") or url, name,
                          headline=p.get("title"), company=p.get("company"),
                          location=p.get("location"), email=page_email or None,
                          website=p.get("profile_url") or None,
                          skills=p.get("skills"), languages=p.get("languages"),
                          evidence_items=ev, raw={"page": url, "via": "claude-extraction"})
            if c:
                yield c

        # 3) fallback: a single-person portfolio described only by OG tags
        if not yielded and og.get("type") in ("profile", "website") and og.get("title"):
            title = og["title"].split("|")[0].split("—")[0].strip()
            if 2 <= len(title.split()) <= 5:
                email_m = EMAIL_RE.search(html)
                c = candidate(source, url, title,
                              headline=og.get("description"),
                              email=email_m.group(0) if email_m else None, website=url,
                              evidence_items=[evidence("Personal site Open Graph metadata", LIKELY, url)],
                              raw={"page": url, "via": "og"})
                if c:
                    yield c
