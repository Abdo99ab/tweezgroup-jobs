"""LinkedIn browser actions with Playwright (headed Chromium, persistent profile).

Everything here is best-effort and defensive: LinkedIn changes its DOM often, so each action tries
several selectors, reads visible text rather than class names where it can, and returns
(ok, detail) instead of raising. Any sign of a security checkpoint is reported as a warning so the
app pauses outreach for SOURCING_PAUSE_HOURS.

Selectors were written against the 2025-2026 LinkedIn web UI (English and French). If an action
starts failing, run `python -m worker check` and update the CANDIDATES lists below.
"""
import logging
import random
import re
import time
from urllib.parse import quote_plus

log = logging.getLogger("worker.linkedin")

WARNING_MARKERS = ("/checkpoint/", "/authwall", "security verification", "vérification de sécurité",
                   "unusual activity", "activité inhabituelle", "temporarily restricted", "temporairement restreint",
                   "captcha", "let's do a quick security check")

BTN_CONNECT = ['button[aria-label^="Invite"]', 'button[aria-label^="Inviter"]', 'button:has-text("Connect")',
               'button:has-text("Se connecter")']
BTN_MORE = ['button[aria-label="More actions"]', 'button[aria-label="Plus d\'actions"]',
            'button:has-text("More")', 'button:has-text("Plus")']
MENU_CONNECT = ['div[role="menu"] :text("Connect")', 'div[role="menu"] :text("Se connecter")',
                '.artdeco-dropdown__content :text("Connect")', '.artdeco-dropdown__content :text("Se connecter")']
BTN_ADD_NOTE = ['button[aria-label="Add a note"]', 'button[aria-label="Ajouter une note"]',
                'button:has-text("Add a note")', 'button:has-text("Ajouter une note")']
NOTE_FIELD = ['textarea#custom-message', 'textarea[name="message"]', 'div[role="dialog"] textarea']
BTN_SEND_INVITE = ['button[aria-label="Send invitation"]', 'button[aria-label="Send now"]',
                   'button[aria-label="Envoyer l\'invitation"]', 'button[aria-label="Envoyer maintenant"]',
                   'div[role="dialog"] button:has-text("Send")', 'div[role="dialog"] button:has-text("Envoyer")']
BTN_PENDING = ['button:has-text("Pending")', 'button:has-text("En attente")']
BTN_MESSAGE = ['button[aria-label^="Message"]', 'button[aria-label^="Envoyer un message"]',
               'a[href*="/messaging/"]:has-text("Message")', 'button:has-text("Message")']
MSG_BOX = ['div.msg-form__contenteditable[contenteditable="true"]', 'div[role="textbox"][contenteditable="true"]',
           'div[aria-label*="message" i][contenteditable="true"]']
MSG_SEND = ['button.msg-form__send-button', 'button[type="submit"]:has-text("Send")',
            'button[type="submit"]:has-text("Envoyer")']
MSG_CLOSE = ['button[aria-label^="Close your conversation"]', 'button[aria-label^="Fermer"]',
             '.msg-overlay-bubble-header button[aria-label*="Close" i]']
DEGREE = ['span.dist-value', 'span.distance-badge', 'span:has-text("1st")', 'span:has-text("1er")']


def human_pause(lo=1.0, hi=3.0):
    time.sleep(random.uniform(lo, hi))


class Warning_(Exception):
    """A security checkpoint / restriction was detected."""


class LinkedIn:
    def __init__(self, page, min_delay=20, max_delay=90):
        self.page = page
        self.min_delay = min_delay
        self.max_delay = max_delay

    # ------------------------------------------------------------------ helpers
    def _first(self, selectors, timeout=2500):
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                continue
        return None

    def goto(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        human_pause(2, 4)
        self._scroll_a_bit()
        self.check_warning()

    def _scroll_a_bit(self):
        try:
            for _ in range(random.randint(1, 3)):
                self.page.mouse.wheel(0, random.randint(250, 700))
                human_pause(0.4, 1.2)
        except Exception:
            pass

    def check_warning(self):
        url = self.page.url.lower()
        try:
            body = (self.page.inner_text("body") or "")[:4000].lower()
        except Exception:
            body = ""
        for m in WARNING_MARKERS:
            if m in url or m in body:
                raise Warning_(f"LinkedIn checkpoint detected ({m}) at {self.page.url}")

    def logged_in(self):
        try:
            self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
            human_pause(2, 3)
        except Exception as exc:
            log.warning("feed load failed: %s", exc)
            return False
        url = self.page.url
        if "/login" in url or "/authwall" in url or "/checkpoint" in url or "signup" in url:
            return False
        return self._first(['nav[aria-label*="Primary" i]', '#global-nav', 'a[href*="/mynetwork/"]'], 8000) is not None

    def between_actions(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # ------------------------------------------------------------------ search
    def search_people(self, query, location=None, max_results=50):
        """Yields dicts {profile_url, full_name, headline, location} from LinkedIn people search."""
        q = query if not location else f"{query} {location}"
        seen, out, page_no = set(), [], 1
        while len(out) < max_results and page_no <= 10:
            self.goto(f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(q)}&page={page_no}")
            cards = self._collect_cards()
            if not cards:
                break
            new = 0
            for c in cards:
                if c["profile_url"] in seen:
                    continue
                seen.add(c["profile_url"])
                out.append(c)
                new += 1
                if len(out) >= max_results:
                    break
            if new == 0:
                break
            page_no += 1
            human_pause(3, 7)
        return out

    def _collect_cards(self):
        """Read every result card as text lines: [name, degree?, headline, location, ...]."""
        js = """
        () => {
          const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
          const cards = new Map();
          for (const a of anchors) {
            const href = a.href.split('?')[0];
            if (!/linkedin\\.com\\/in\\//.test(href)) continue;
            let el = a;
            for (let i = 0; i < 8 && el; i++) {
              el = el.parentElement;
              if (!el) break;
              if (el.tagName === 'LI' || el.getAttribute('data-chameleon-result-urn') ||
                  (el.getAttribute('data-view-name') || '').includes('search-entity-result')) break;
            }
            if (!el || cards.has(href)) continue;
            const text = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            if (text.length) cards.set(href, text);
          }
          return Array.from(cards, ([href, lines]) => ({href, lines}));
        }"""
        try:
            raw = self.page.evaluate(js)
        except Exception as exc:
            log.warning("card collection failed: %s", exc)
            return []
        cards = []
        for item in raw:
            lines = [l for l in item["lines"] if not re.fullmatch(r"(•\s*)?(1st|2nd|3rd|1er|2e|3e|\+)\s*(degree|degré)?.*", l, re.I)]
            lines = [l for l in lines if l.lower() not in ("connect", "se connecter", "message", "follow", "suivre",
                                                            "view profile", "voir le profil", "linkedin member",
                                                            "membre linkedin")]
            if not lines:
                continue
            name = re.sub(r"\s*\((He|She|They|Il|Elle).*?\)\s*$", "", lines[0]).strip()
            if name.lower() in ("linkedin member", "membre linkedin") or len(name) < 2:
                continue
            headline = lines[1] if len(lines) > 1 else None
            location = lines[2] if len(lines) > 2 else None
            about = " | ".join(lines[3:6]) if len(lines) > 3 else None
            cards.append({"profile_url": item["href"], "full_name": name[:200], "headline": (headline or "")[:400] or None,
                          "location": (location or "")[:200] or None, "about": about})
        return cards

    # ------------------------------------------------------------------ profile actions
    def connection_state(self):
        """'connected' | 'pending' | 'can_connect' | 'unknown' for the profile currently open."""
        if self._first(BTN_PENDING, 1500):
            return "pending"
        deg = self._first(DEGREE, 1500)
        if deg:
            try:
                t = deg.inner_text().lower()
                if "1st" in t or "1er" in t:
                    return "connected"
            except Exception:
                pass
        if self._first(BTN_CONNECT, 1500):
            return "can_connect"
        if self._first(BTN_MORE, 1500):
            return "can_connect_via_more"
        if self._first(BTN_MESSAGE, 1500):
            return "connected"
        return "unknown"

    def connect(self, profile_url, note):
        self.goto(profile_url)
        state = self.connection_state()
        if state == "pending":
            return True, "invitation already pending"
        if state == "connected":
            return True, "already connected"
        btn = self._first(BTN_CONNECT, 2000)
        if btn is None:
            more = self._first(BTN_MORE, 2000)
            if more is None:
                return False, "no Connect button found"
            more.click()
            human_pause(1, 2)
            btn = self._first(MENU_CONNECT, 3000)
            if btn is None:
                return False, "Connect not in the More menu"
        btn.click()
        human_pause(1.5, 3)
        self.check_warning()
        if note:
            add = self._first(BTN_ADD_NOTE, 3000)
            if add:
                add.click()
                human_pause(1, 2)
                field = self._first(NOTE_FIELD, 3000)
                if field:
                    field.click()
                    self.page.keyboard.type(note[:300], delay=random.randint(25, 60))
                    human_pause(1, 2)
        send = self._first(BTN_SEND_INVITE, 3000)
        if send is None:
            return False, "Send button not found in the invitation dialog"
        send.click()
        human_pause(2, 3)
        self.check_warning()
        if self._first(['div[role="dialog"]:has-text("weekly invitation limit")',
                        'div[role="dialog"]:has-text("limite hebdomadaire")'], 1500):
            raise Warning_("weekly invitation limit reached")
        return True, "invitation sent" + (" with note" if note else "")

    def is_connected(self, profile_url):
        self.goto(profile_url)
        state = self.connection_state()
        return state == "connected"

    def message(self, profile_url, text):
        self.goto(profile_url)
        btn = self._first(BTN_MESSAGE, 3000)
        if btn is None:
            return False, "no Message button (not connected yet?)"
        btn.click()
        human_pause(2, 3)
        box = self._first(MSG_BOX, 6000)
        if box is None:
            return False, "message box did not open"
        box.click()
        for i, line in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            if line:
                self.page.keyboard.type(line, delay=random.randint(15, 45))
        human_pause(1, 2)
        send = self._first(MSG_SEND, 3000)
        if send is None:
            return False, "message Send button not found"
        try:
            if send.is_disabled():
                human_pause(1, 2)
        except Exception:
            pass
        send.click()
        human_pause(2, 3)
        self.check_warning()
        close = self._first(MSG_CLOSE, 1500)
        if close:
            try:
                close.click()
            except Exception:
                pass
        return True, "message sent"

    # ------------------------------------------------------------------ inbox
    def read_inbox(self, max_threads=15):
        """Unread conversations -> [{profile_url, text, at}] (last message from the other person)."""
        self.goto("https://www.linkedin.com/messaging/?filter=unread")
        out = []
        items = self.page.locator('li.msg-conversation-listitem, li[class*="conversation-listitem"]')
        try:
            n = min(items.count(), max_threads)
        except Exception:
            n = 0
        for i in range(n):
            try:
                items.nth(i).click()
                human_pause(2, 3)
                link = self._first(['a.msg-thread__link-to-profile', '.msg-thread a[href*="/in/"]',
                                    'header a[href*="/in/"]'], 3000)
                url = link.get_attribute("href").split("?")[0] if link else None
                if not url:
                    continue
                js = """
                () => {
                  const evs = Array.from(document.querySelectorAll('.msg-s-event-listitem, li[class*="event-listitem"]'));
                  const mine = Array.from(document.querySelectorAll('.msg-s-event-listitem--other, .msg-s-event-listitem'))
                  const last = evs.filter(e => e.className.includes('--other') || !e.className.includes('--self')).slice(-1)[0];
                  if (!last) return null;
                  const body = last.querySelector('.msg-s-event-listitem__body, p');
                  const t = last.querySelector('time');
                  return {text: body ? body.innerText.trim() : last.innerText.trim(), at: t ? t.getAttribute('datetime') : null};
                }"""
                data = self.page.evaluate(js)
                if data and data.get("text"):
                    out.append({"profile_url": url, "text": data["text"][:3000], "at": data.get("at")})
            except Exception as exc:
                log.warning("inbox thread %s failed: %s", i, exc)
        return out
