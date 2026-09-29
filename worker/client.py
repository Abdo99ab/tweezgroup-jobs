"""Thin HTTP client for /api/v1/sourcing/* (and websourcing jobs).

The app usually runs on Render; this worker runs on a recruiter PC. Auth is only
X-API-Key — no VPN required. Cold starts on free Render can take ~30–60s, so we retry.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger("worker.api")

# Transient failures while Render wakes / network blips.
_RETRY_STATUSES = {502, 503, 504}
_MAX_ATTEMPTS = 4


class Api:
    def __init__(self, base=None, key=None):
        self.base = (base or config.APP_URL).rstrip("/") + "/api/v1"
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key or config.API_KEY
        self.s.headers["User-Agent"] = "tweezgroup-sourcing-worker/1"

    def _r(self, method, path, **kw):
        url = self.base + path
        timeout = kw.pop("timeout", 90)
        last_exc = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                r = self.s.request(method, url, timeout=timeout, **kw)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= _MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"{method} {path} failed talking to {config.APP_URL}: {exc}. "
                        "If the app is on Render free tier, open the site once to wake it, then retry."
                    ) from exc
                wait = 5 * attempt
                log.warning("%s %s network error (%s); retry in %ss", method, path, exc, wait)
                time.sleep(wait)
                continue

            if r.status_code == 401:
                raise RuntimeError(
                    f"{method} {path} -> 401 unauthorized. "
                    f"worker/.env API_KEY must exactly match the API_KEY of the app at {config.APP_URL} "
                    "(Render dashboard > Environment > API_KEY when APP_URL is the live site)."
                )
            if r.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                wait = 8 * attempt
                log.warning("%s %s -> %s (app waking?); retry in %ss", method, path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"{method} {path} failed: {last_exc}")

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
