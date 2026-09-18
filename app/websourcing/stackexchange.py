"""Stack Exchange adapter — the official public API (api.stackexchange.com), which exists
precisely for this kind of read access. Finds top answerers for the role's key tags."""
import html
import re

from flask import current_app

from .base import CONFIRMED, LIKELY, SourceAdapter, candidate, evidence, polite_get

TAG_RE = re.compile(r"[^a-z0-9+#.\-]")


def to_tag(skill):
    t = TAG_RE.sub("-", (skill or "").lower().strip()).strip("-")
    return {"c#": "c%23", "node.js": "node.js"}.get(t, t)


class StackExchangeAdapter(SourceAdapter):
    name = "stackexchange"
    label = "Stack Overflow (browser)"
    reliability = "medium"     # great skill evidence, but no location/contact most of the time
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        cfg = current_app.config
        base = cfg["STACKEXCHANGE_API_BASE"]
        params_extra = {"key": cfg["STACKEXCHANGE_KEY"]} if cfg.get("STACKEXCHANGE_KEY") else {}
        tags = [to_tag(s) for s in (req.get("skills") or [])[:4] if to_tag(s)]
        found, seen = 0, set()
        for tag in tags:
            if found >= cap:
                break
            data, skip = polite_get(f"{base}/tags/{tag}/top-answerers/all_time", expect_json=True,
                                    params={"site": "stackoverflow", "pagesize": 10, **params_extra})
            if skip:
                self.skip(skip)
                break
            for row in (data or {}).get("items", []):
                if found >= cap:
                    break
                u = row.get("user") or {}
                uid = u.get("user_id")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                c = self._user(base, uid, u, tag, row, params_extra)
                if c:
                    found += 1
                    yield c

    def _user(self, base, uid, brief, tag, row, params_extra):
        data, skip = polite_get(f"{base}/users/{uid}", expect_json=True,
                                params={"site": "stackoverflow", **params_extra})
        if skip:
            self.skip(skip)
        u = ((data or {}).get("items") or [brief])[0]
        url = u.get("link") or f"https://stackoverflow.com/users/{uid}"
        name = html.unescape(u.get("display_name") or "")
        ev = [
            evidence(f"Top all-time answerer for [{tag}] on Stack Overflow "
                     f"({row.get('score', 0)} answer score, {row.get('post_count', 0)} answers)",
                     CONFIRMED, url),
            evidence(f"Reputation {u.get('reputation', 0):,}".replace(",", " "), CONFIRMED, url),
        ]
        if u.get("location"):
            ev.append(evidence(f"Location (self-reported): {u['location']}", LIKELY, url))
        about = re.sub(r"<[^>]+>", " ", html.unescape(u.get("about_me") or "")).strip()
        if about:
            ev.append(evidence(f"About: “{about[:150]}”", CONFIRMED, url))
        return candidate(
            self.name, url, name,
            headline=about[:400] or f"Top [{tag}] answerer on Stack Overflow",
            location=u.get("location"), website=u.get("website_url") or None,
            skills=[tag.replace("%23", "#")],
            evidence_items=ev,
            raw={"user_id": uid, "reputation": u.get("reputation"),
                 "badges": (u.get("badge_counts") or {})},
        )
