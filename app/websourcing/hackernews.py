"""Hacker News adapter — the public HN Algolia search API (hn.algolia.com/api).

Two angles:
  * "Ask HN: Who wants to be hired?" monthly threads — people posting *because they want
    recruiters to find them*; the friendliest possible source.
  * high-signal commenters whose text matches the role's keywords.
"""
import html
import re

from flask import current_app

from .base import CONFIRMED, LIKELY, UNKNOWN, SourceAdapter, candidate, evidence, polite_get

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+ ?(?:@|\[at\]| at ) ?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
FIELD_RE = re.compile(r"(?im)^\s*(location|remote|willing to relocate|technologies|r[ée]sum[ée]|"
                      r"resume|cv|email|website)\s*[:\-]\s*(.+)$")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _text(comment):
    t = html.unescape(comment or "")
    t = re.sub(r"<p>", "\n", t)
    return re.sub(r"<[^>]+>", " ", t).strip()


class HackerNewsAdapter(SourceAdapter):
    name = "hackernews"
    label = "Hacker News (browser)"
    reliability = "medium"
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        base = current_app.config["HN_API_BASE"]
        found = 0
        # 1) the current "who wants to be hired" thread(s)
        data, skip = polite_get(f"{base}/search", expect_json=True,
                                params={"query": "Ask HN: Who wants to be hired?",
                                        "tags": "story", "hitsPerPage": 2})
        if skip:
            self.skip(skip)
            return
        keywords = [k.lower() for k in (req.get("skills") or []) + (req.get("titles") or [])]
        for story in (data or {}).get("hits", []):
            if found >= cap:
                break
            sid = story.get("objectID")
            if not sid:
                continue
            thread, skip = polite_get(f"{base}/search", expect_json=True,
                                      params={"tags": f"comment,story_{sid}", "hitsPerPage": 60})
            if skip:
                self.skip(skip)
                break
            for hit in (thread or {}).get("hits", []):
                if found >= cap:
                    break
                c = self._from_hire_comment(hit, keywords, story)
                if c:
                    found += 1
                    yield c

    def _from_hire_comment(self, hit, keywords, story):
        text = _text(hit.get("comment_text"))
        if len(text) < 60:
            return None
        low = text.lower()
        matched = [k for k in keywords if k and k in low]
        if keywords and not matched:
            return None
        author = hit.get("author") or ""
        url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        fields = {m.group(1).lower(): m.group(2).strip() for m in FIELD_RE.finditer(text)}
        location = fields.get("location", "")[:200] or None
        email_m = EMAIL_RE.search(text)
        email = email_m.group(0).replace("[at]", "@").replace(" at ", "@").replace(" ", "") if email_m else None
        website = next((u for u in URL_RE.findall(text)
                        if "ycombinator" not in u and "news.yc" not in u), None)
        ev = [evidence(f"Posted in “{story.get('title', 'Who wants to be hired?')}” — "
                       "actively looking and asked to be contacted", CONFIRMED, url)]
        for k in matched[:6]:
            ev.append(evidence(f"Mentions “{k}” in their post", CONFIRMED, url))
        if fields.get("technologies"):
            ev.append(evidence(f"Technologies: {fields['technologies'][:150]}", CONFIRMED, url))
        if location:
            ev.append(evidence(f"Location: {location}", CONFIRMED, url))
        remote = fields.get("remote")
        if remote:
            ev.append(evidence(f"Remote: {remote[:60]}", CONFIRMED, url))
        if email:
            ev.append(evidence("Published a contact email in the post itself", CONFIRMED, url))
        else:
            ev.append(evidence("No contact info in the post (reply on HN or via their site)", UNKNOWN, url))
        skills = [t.strip().lower() for t in re.split(r"[,;/|]", fields.get("technologies", "")) if t.strip()]
        # HN usernames are pseudonyms; use the name only if the post signs one, else the handle.
        return candidate(
            self.name, url, fields.get("name", author) or author,
            headline=text.splitlines()[0][:400] if text else None,
            location=location, email=email, website=website,
            skills=skills or matched,
            evidence_items=ev,
            raw={"hn_user": author, "posted_at": hit.get("created_at"),
                 "excerpt": text[:1200]},
        )
