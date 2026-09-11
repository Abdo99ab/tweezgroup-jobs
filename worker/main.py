"""The worker loop: ask the app what to do, do it in the browser, report back."""
import logging
import time
from datetime import datetime, timedelta

from . import config
from .client import Api
from .linkedin import LinkedIn, Warning_

log = logging.getLogger("worker")

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def within_hours(status, now=None):
    """status['hours'] like '09:00-18:00' in status['tz']; weekends are off."""
    tz = ZoneInfo(status.get("tz", "Europe/Paris")) if ZoneInfo else None
    now = now or (datetime.now(tz) if tz else datetime.now())
    if now.weekday() >= 5:
        return False
    try:
        start, end = status.get("hours", "09:00-18:00").split("-")
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except ValueError:
        return True
    t = now.hour * 60 + now.minute
    return sh * 60 + sm <= t < eh * 60 + em


def _paused(status):
    pu = status.get("paused_until")
    if not pu:
        return None
    try:
        until = datetime.fromisoformat(pu.replace("Z", ""))
    except ValueError:
        return None
    return until if until > datetime.utcnow() else None


class Worker:
    def __init__(self, api: Api, linkedin: LinkedIn):
        self.api = api
        self.li = linkedin
        self.last_inbox = None

    # ----------------------------------------------------------------- one pass
    def pass_once(self):
        status = self.api.status()
        until = _paused(status)
        if until:
            log.info("paused until %s UTC (%s)", until, status.get("paused_reason"))
            return "paused"
        if not within_hours(status):
            log.info("outside working hours (%s %s) — idle", status.get("hours"), status.get("tz"))
            return "off-hours"
        self.li.min_delay = status.get("min_delay", self.li.min_delay)
        self.li.max_delay = status.get("max_delay", self.li.max_delay)
        try:
            self.run_searches()
            n = self.run_queue()
            if self.last_inbox is None or datetime.utcnow() - self.last_inbox > timedelta(minutes=config.INBOX_EVERY):
                self.run_inbox()
        except Warning_ as w:
            log.error("WARNING from LinkedIn — pausing: %s", w)
            self.api.pause(str(w)[:300])
            return "warning"
        return "ok"

    def run_searches(self):
        for s in self.api.pending_searches():
            log.info("search #%s (%s): %r", s["id"], s["role"], s["query"])
            try:
                profiles = self.li.search_people(s["query"], s.get("location"), s.get("max_results", 50))
            except Warning_:
                self.api.post_results(s["id"], [], done=False, error="paused on LinkedIn warning")
                raise
            except Exception as exc:
                log.exception("search failed")
                self.api.post_results(s["id"], [], done=True, error=str(exc)[:300])
                continue
            for i in range(0, len(profiles), 25):
                self.api.post_results(s["id"], profiles[i:i + 25], done=False)
            self.api.post_results(s["id"], [], done=True)
            log.info("search #%s: %d profiles", s["id"], len(profiles))
            self.li.between_actions()

    def run_queue(self):
        q = self.api.queue(config.MAX_ACTIONS_PER_PASS)
        actions = q.get("actions", [])
        log.info("%d action(s) to do; caps left %s", len(actions), q.get("status", {}).get("caps", {}).get("left"))
        done = 0
        for a in actions:
            pid, action, url, text = a["profile_id"], a["action"], a["profile_url"], a.get("text")
            log.info("%s -> %s (%s)", action, a.get("full_name"), url)
            try:
                if action == "connect":
                    ok, detail = self.li.connect(url, text)
                    self.api.outcome(pid, "connect", ok=ok, detail=detail)
                elif action == "check_accept":
                    accepted = self.li.is_connected(url)
                    self.api.outcome(pid, "check_accept", ok=True, accepted=accepted,
                                     detail="connected" if accepted else "not yet")
                elif action in ("message", "reminder"):
                    ok, detail = self.li.message(url, text)
                    self.api.outcome(pid, action, ok=ok, detail=detail)
                else:
                    self.api.outcome(pid, action, ok=False, detail="unknown action")
                done += 1
            except Warning_ as w:
                self.api.outcome(pid, action, ok=False, detail=str(w)[:300], warning=str(w)[:300])
                raise
            except Exception as exc:
                log.exception("action failed")
                self.api.outcome(pid, action, ok=False, detail=str(exc)[:300])
            self.li.between_actions()
        return done

    def run_inbox(self):
        try:
            msgs = self.li.read_inbox()
        except Warning_:
            raise
        except Exception:
            log.exception("inbox read failed")
            msgs = []
        self.last_inbox = datetime.utcnow()
        if msgs:
            r = self.api.inbox(msgs)
            log.info("inbox: %d unread thread(s), %s matched", len(msgs), r.get("matched"))

    # ----------------------------------------------------------------- loop
    def run_forever(self):
        while True:
            try:
                result = self.pass_once()
            except Exception:
                log.exception("pass failed")
                result = "error"
            wait = config.POLL_SECONDS if result in ("ok", "error") else max(config.POLL_SECONDS, 900)
            log.info("sleeping %ss", wait)
            time.sleep(wait)
