"""Thin HTTP client for /api/v1/sourcing/*."""
import logging

import requests

from . import config

log = logging.getLogger("worker.api")


class Api:
    def __init__(self, base=None, key=None):
        self.base = (base or config.APP_URL) + "/api/v1"
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key or config.API_KEY

    def _r(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def status(self):
        return self._r("GET", "/sourcing/status")

    def pause(self, reason, hours=None):
        return self._r("POST", "/sourcing/pause", json={"reason": reason, "hours": hours})

    def pending_searches(self):
        return self._r("GET", "/sourcing/searches", params={"status": "pending"})["searches"]

    def next_web_job(self):
        return self._r("GET", "/websourcing/jobs").get("job")

    def post_web_results(self, run_id, source, candidates, notes=None, done=True):
        return self._r("POST", f"/websourcing/jobs/{run_id}/results",
                       json={"source": source, "candidates": candidates or [],
                             "notes": notes or [], "done": done})

    def post_results(self, search_id, profiles, done=False, error=None):
        return self._r("POST", f"/sourcing/searches/{search_id}/results",
                       json={"profiles": profiles, "done": done, "error": error})

    def queue(self, limit):
        return self._r("GET", "/sourcing/queue", params={"limit": limit})

    def outcome(self, profile_id, action, ok=True, detail=None, accepted=None, reply_text=None, warning=None):
        body = {"action": action, "ok": ok, "detail": detail}
        if accepted is not None:
            body["accepted"] = accepted
        if reply_text:
            body["reply_text"] = reply_text
        if warning:
            body["warning"] = warning
        return self._r("POST", f"/sourcing/profiles/{profile_id}/outcome", json=body)

    def inbox(self, messages):
        return self._r("POST", "/sourcing/inbox", json={"messages": messages})
