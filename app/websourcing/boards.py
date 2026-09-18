"""Job-boards adapter — public Greenhouse / Lever / Ashby career-page APIs.

These are the *published* JSON feeds behind companies' own career pages (Greenhouse and
Lever document them publicly; Ashby exposes a posting API per board). A job board lists
jobs, not people — so this adapter uses them the only honest way: recruiter-supplied
board slugs/URLs confirm which companies hire (or hired) the same kind of role, and each
such company's public team/about page is then read through the WebPages extractor.
"""
import re
from urllib.parse import urlparse

from flask import current_app

from .base import SourceAdapter, polite_get
from .webpages import WebPagesAdapter

GH_URL = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)")
LEVER_URL = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")
ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")

TEAM_PATHS = ("/about", "/team", "/about-us", "/people")


class BoardsAdapter(SourceAdapter):
    name = "boards"
    label = "Greenhouse / Lever / Ashby (browser)"
    reliability = "medium"
    needs_seeds = True
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        cfg = current_app.config
        pages = WebPagesAdapter()
        keywords = [k.lower() for k in (req.get("titles") or [role.title])]
        found, companies = 0, 0
        for seed in seeds:
            if found >= cap or companies >= 5:
                break
            board = self._board(cfg, seed)
            if not board:
                continue
            kind, slug, jobs, skip = board
            if skip:
                self.skip(skip)
                continue
            similar = [j for j in jobs if any(k in j.lower() for k in keywords)]
            if not similar:
                continue    # this company doesn't hire this kind of role — skip its team page
            companies += 1
            site = self._company_site(seed)
            for path in TEAM_PATHS if site else ():
                for c in pages.extract(site + path, source=self.name):
                    if found >= cap:
                        break
                    c["evidence"].insert(0, {
                        "claim": f"Works at a company whose public {kind} board lists "
                                 f"“{similar[0][:80]}” (same kind of role)",
                        "label": "Confirmed", "url": seed})
                    found += 1
                    yield c
                if found:
                    break
        self.skips.extend(pages.skips)

    def _board(self, cfg, seed):
        """Return (kind, slug, [job titles], skip_reason) for a recognised board URL/slug."""
        m = GH_URL.search(seed)
        if m:
            data, skip = polite_get(f"{cfg['GREENHOUSE_API_BASE']}/boards/{m.group(1)}/jobs",
                                    expect_json=True)
            return "Greenhouse", m.group(1), [j.get("title", "") for j in (data or {}).get("jobs", [])], skip
        m = LEVER_URL.search(seed)
        if m:
            data, skip = polite_get(f"{cfg['LEVER_API_BASE']}/postings/{m.group(1)}",
                                    expect_json=True, params={"mode": "json"})
            return "Lever", m.group(1), [j.get("text", "") for j in (data or [])], skip
        m = ASHBY_URL.search(seed)
        if m:
            data, skip = polite_get(f"{cfg['ASHBY_API_BASE']}/job-board/{m.group(1)}",
                                    expect_json=True)
            return "Ashby", m.group(1), [j.get("title", "") for j in (data or {}).get("jobs", [])], skip
        return None

    @staticmethod
    def _company_site(seed):
        """The recruiter can paste "https://boards.greenhouse.io/acme https://acme.com" pairs;
        otherwise we have no site to read and only the board check happens."""
        for part in seed.split():
            u = urlparse(part if part.startswith("http") else "https://" + part)
            host = (u.hostname or "").lower()
            if host and not any(b in host for b in ("greenhouse", "lever.co", "ashbyhq")):
                return f"{u.scheme}://{u.netloc}"
        return None
