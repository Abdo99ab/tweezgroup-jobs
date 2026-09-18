"""GitLab adapter — official public REST API (docs.gitlab.com/ee/api). Unauthenticated
requests only see public data; an optional GITLAB_TOKEN raises rate limits, nothing more."""
from flask import current_app

from .base import CONFIRMED, LIKELY, SourceAdapter, candidate, evidence, polite_get


def _headers():
    tok = current_app.config.get("GITLAB_TOKEN")
    return {"PRIVATE-TOKEN": tok} if tok else {}


class GitLabAdapter(SourceAdapter):
    name = "gitlab"
    label = "GitLab (browser)"
    reliability = "high"
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        base = current_app.config["GITLAB_API_BASE"]
        found, seen = 0, set()
        # GitLab's public users search matches name/username; project search finds maintainers.
        for q in queries.get("gitlab", queries.get("github", []))[:3]:
            if found >= cap:
                break
            projects, skip = polite_get(f"{base}/projects", expect_json=True, headers=_headers(),
                                        params={"search": q, "order_by": "star_count", "per_page": 10,
                                                "visibility": "public"})
            if skip:
                self.skip(skip)
                break
            for p in (projects or []):
                if found >= cap:
                    break
                ns = p.get("namespace") or {}
                if ns.get("kind") != "user" or ns.get("id") in seen:
                    continue
                seen.add(ns.get("id"))
                c = self._user(base, ns.get("id"), p, q)
                if c:
                    found += 1
                    yield c

    def _user(self, base, uid, project, query):
        if not uid:
            return None
        user, skip = polite_get(f"{base}/users/{uid}", expect_json=True, headers=_headers())
        if skip or not user or user.get("state") != "active":
            self.skip(skip)
            return None
        url = user.get("web_url") or f"https://gitlab.com/{user.get('username')}"
        ev = [
            evidence(f"Owns public GitLab project “{project.get('name')}”"
                     + (f" ({project.get('star_count')}★)" if project.get("star_count") else "")
                     + f" matching “{query}”", CONFIRMED, project.get("web_url") or url),
        ]
        if user.get("bio"):
            ev.append(evidence(f"Bio: “{user['bio'][:150]}”", CONFIRMED, url))
        if user.get("location"):
            ev.append(evidence(f"Location (self-reported): {user['location']}", LIKELY, url))
        return candidate(
            self.name, url, user.get("name") or user.get("username"),
            headline=(user.get("bio") or user.get("job_title") or "").strip() or None,
            location=user.get("location"), company=user.get("organization"),
            email=user.get("public_email") or None,       # explicitly the *public* email field
            website=user.get("website_url") or None,
            projects=[{"name": project.get("name"), "url": project.get("web_url"),
                       "stars": project.get("star_count", 0),
                       "desc": (project.get("description") or "")[:200]}],
            evidence_items=ev,
            raw={"username": user.get("username")},
        )
