"""GitHub adapter — official REST API (docs.github.com/rest), unauthenticated or with an
optional personal token (GITHUB_TOKEN) that only raises the rate limit. Public data only."""
from flask import current_app

from .base import CONFIRMED, LIKELY, SourceAdapter, candidate, evidence, polite_get


def _headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = current_app.config.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


class GitHubAdapter(SourceAdapter):
    name = "github"
    label = "GitHub (browser)"
    reliability = "high"
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        base = current_app.config["GITHUB_API_BASE"]
        found = 0
        for q in queries.get("github", [])[:4]:
            if found >= cap:
                break
            data, skip = polite_get(f"{base}/search/users", expect_json=True, headers=_headers(),
                                    params={"q": q, "per_page": min(cap, 15)})
            if skip:
                self.skip(skip)
                break
            for item in (data or {}).get("items", []):
                if found >= cap:
                    break
                c = self._enrich(base, item.get("login"), q)
                if c:
                    found += 1
                    yield c

    def _enrich(self, base, login, query):
        if not login:
            return None
        user, skip = polite_get(f"{base}/users/{login}", expect_json=True, headers=_headers())
        if skip or not user:
            self.skip(skip)
            return None
        profile_url = user.get("html_url") or f"https://github.com/{login}"
        ev = [evidence(f"GitHub profile matched search “{query}”", CONFIRMED, profile_url)]
        skills, projects = set(), []
        repos, skip = polite_get(f"{base}/users/{login}/repos", expect_json=True, headers=_headers(),
                                 params={"sort": "pushed", "per_page": 8, "type": "owner"})
        for r in (repos or []):
            if r.get("fork"):
                continue
            lang = (r.get("language") or "").lower()
            if lang:
                skills.add(lang)
            projects.append({"name": r.get("name"), "url": r.get("html_url"),
                             "stars": r.get("stargazers_count", 0),
                             "desc": (r.get("description") or "")[:200]})
            if lang:
                ev.append(evidence(f"Owns public repo “{r.get('name')}” in {r.get('language')}"
                                   + (f" ({r.get('stargazers_count')}★)" if r.get("stargazers_count") else ""),
                                   CONFIRMED, r.get("html_url") or profile_url))
        if user.get("bio"):
            ev.append(evidence(f"Bio: “{user['bio'][:150]}”", CONFIRMED, profile_url))
        if user.get("company"):
            ev.append(evidence(f"Company (self-reported): {user['company']}", LIKELY, profile_url))
        if user.get("location"):
            ev.append(evidence(f"Location (self-reported): {user['location']}", LIKELY, profile_url))
        if user.get("hireable"):
            ev.append(evidence("Marked themselves as hireable on GitHub", CONFIRMED, profile_url))
        followers = user.get("followers") or 0
        if followers >= 50:
            ev.append(evidence(f"{followers} GitHub followers", CONFIRMED, profile_url))
        return candidate(
            self.name, profile_url, user.get("name") or login,
            headline=(user.get("bio") or "").strip()[:400] or None,
            location=user.get("location"), company=(user.get("company") or "").lstrip("@") or None,
            email=user.get("email"),                       # only present when the user made it public
            website=user.get("blog") or None,
            skills=skills, projects=sorted(projects, key=lambda p: -(p.get("stars") or 0)),
            evidence_items=ev,
            raw={"login": login, "followers": followers, "public_repos": user.get("public_repos"),
                 "hireable": bool(user.get("hireable")), "created_at": user.get("created_at")},
        )
