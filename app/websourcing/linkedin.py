"""LinkedIn is not fetched from the Flask process.

Discover queues a people-search for the headed Playwright worker (`python -m worker run`).
Chromium — already logged into the recruiting account — types the query, the worker posts
profiles back, and the app AI-scores them onto the same review list as the public sources.
"""
from .base import SourceAdapter


class LinkedInAdapter(SourceAdapter):
    name = "linkedin"
    label = "LinkedIn (browser)"
    reliability = "high"
    needs_worker = True

    def search(self, role, req, queries, seeds, cap):
        # The engine queues a SourcingSearch instead of calling this.
        return
        yield  # pragma: no cover
