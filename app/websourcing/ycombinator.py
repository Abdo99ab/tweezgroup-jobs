"""Y Combinator ecosystem adapter — the public, community-maintained YC company dataset
(yc-oss.github.io/api, MIT-licensed static JSON rebuilt daily from YC's own public directory).

Finds YC companies in the role's industry, then reads each company's public team/founders
page through the WebPages extractor. Founders and early employees of small YC startups are
exactly the senior generalists many roles need."""
from flask import current_app

from .base import SourceAdapter, polite_get
from .webpages import WebPagesAdapter

TEAM_PATHS = ("/about", "/team", "/founders", "/people")


class YCombinatorAdapter(SourceAdapter):
    name = "ycombinator"
    label = "YC startup ecosystem (browser)"
    reliability = "medium"
    needs_seeds = False
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        base = current_app.config["YC_API_BASE"]
        data, skip = polite_get(f"{base}/companies/all.json", expect_json=True, timeout=60)
        if skip:
            self.skip(skip)
            return
        keywords = [k.lower() for k in (req.get("industries") or []) +
                    (req.get("skills") or [])[:3] + [role.title]]
        picked = []
        for co in (data or []):
            hay = " ".join([co.get("one_liner") or "", " ".join(co.get("tags") or []),
                            " ".join(co.get("industries") or []) if isinstance(co.get("industries"), list)
                            else (co.get("industry") or "")]).lower()
            if co.get("website") and any(k in hay for k in keywords if k):
                picked.append(co)
            if len(picked) >= 6:
                break
        pages = WebPagesAdapter()
        found = 0
        for co in picked:
            if found >= cap:
                break
            site = (co.get("website") or "").rstrip("/")
            got_here = 0
            for path in TEAM_PATHS:
                for c in pages.extract(site + path, source=self.name):
                    if found >= cap:
                        break
                    c["evidence"].insert(0, {
                        "claim": f"On the public team page of {co.get('name')} "
                                 f"(YC {co.get('batch', '')}: {co.get('one_liner', '')[:80]})",
                        "label": "Confirmed", "url": site + path})
                    c["company"] = c.get("company") or co.get("name")
                    found += 1
                    got_here += 1
                    yield c
                if got_here:
                    break
        self.skips.extend(pages.skips)
