"""Public-source discovery in the headed Chromium (same profile as LinkedIn).

Flask's Python HTTP client is often blocked (GitHub ConnectTimeout, GitLab 403).
The worker visits the public search pages as a normal browser instead, scrapes
visible result cards, and posts them back. No logins are performed here.
"""
import logging
import re
from urllib.parse import quote_plus

from .linkedin import human_pause

log = logging.getLogger("worker.websearch")

SKIP_GH = {"topics", "explore", "pricing", "login", "settings", "orgs", "search", "about",
           "features", "marketplace", "sponsors", "collections", "trending", "events", "readme"}


def _js_cards(href_re):
    return f"""
    () => {{
      const re = {href_re};
      const skip = new Set({list(SKIP_GH)!r});
      const out = [], seen = new Set();
      for (const a of document.querySelectorAll('a[href]')) {{
        const href = (a.href || '').split('?')[0].replace(/\\/$/, '');
        const m = href.match(re);
        if (!m) continue;
        if (m[1] && skip.has(m[1].toLowerCase())) continue;
        if (seen.has(href)) continue;
        seen.add(href);
        let el = a;
        for (let i = 0; i < 6 && el; i++) el = el.parentElement;
        const lines = ((el || a).innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        out.push({{profile_url: href, full_name: lines[0] || m[1] || href,
                   headline: lines.slice(1, 3).join(' — ').slice(0, 400),
                   location: lines.find(l => /france|paris|remote|london|berlin|algeria/i.test(l)) || ''}});
        if (out.length >= 30) break;
      }}
      return out;
    }}
    """


class WebSearch:
    def __init__(self, page):
        self.page = page

    def goto(self, url):
        log.info("open %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        human_pause(2, 4)
        try:
            self.page.mouse.wheel(0, 800)
            human_pause(0.6, 1.4)
        except Exception:
            pass

    def run_source(self, source, job):
        fn = getattr(self, f"search_{source}", None)
        if not fn:
            return [], [f"no browser scraper for {source}"]
        try:
            found = fn(job) or []
            return found, []
        except Exception as exc:
            log.exception("%s failed", source)
            return [], [f"{type(exc).__name__}: {exc}"]

    def _eval(self, js):
        try:
            return self.page.evaluate(js) or []
        except Exception as exc:
            log.warning("evaluate failed: %s", exc)
            return []

    def search_github(self, job):
        cap, out = job.get("cap") or 25, []
        queries = (job.get("queries") or {}).get("github") or [job.get("title") or "developer"]
        js = _js_cards(r"/^https:\/\/github\.com\/([A-Za-z0-9-]+)$/")
        for q in queries[:3]:
            if len(out) >= cap:
                break
            self.goto(f"https://github.com/search?q={quote_plus(q)}&type=users")
            for c in self._eval(js):
                if c["profile_url"] not in {x["profile_url"] for x in out}:
                    out.append(c)
                if len(out) >= cap:
                    break
        return out[:cap]

    def search_gitlab(self, job):
        cap, out = job.get("cap") or 25, []
        queries = (job.get("queries") or {}).get("gitlab") or [job.get("title") or "developer"]
        js = _js_cards(r"/^https:\/\/gitlab\.com\/([A-Za-z0-9_.-]+)$/")
        for q in queries[:3]:
            if len(out) >= cap:
                break
            self.goto(f"https://gitlab.com/search?scope=users&search={quote_plus(q)}")
            for c in self._eval(js):
                if c["profile_url"] not in {x["profile_url"] for x in out}:
                    out.append(c)
                if len(out) >= cap:
                    break
        return out[:cap]

    def search_stackexchange(self, job):
        skills = (job.get("requirements") or {}).get("skills") or []
        tags = [re.sub(r"[^a-z0-9+.-]", "-", (s or "").lower()).strip("-") for s in skills[:4]]
        tags = [t for t in tags if t] or ["python"]
        cap, out = job.get("cap") or 25, []
        js = """
        () => {
          const out = [], seen = new Set();
          for (const a of document.querySelectorAll('a[href*="/users/"]')) {
            const href = a.href.split('?')[0];
            if (!/\\/users\\/\\d+/.test(href) || seen.has(href)) continue;
            seen.add(href);
            const card = a.closest('.user-info, .user-card, div') || a.parentElement;
            const lines = ((card && card.innerText) || a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            out.push({profile_url: href, full_name: lines[0] || a.innerText, headline: lines[1] || '', location: ''});
            if (out.length >= 25) break;
          }
          return out;
        }"""
        for tag in tags:
            if len(out) >= cap:
                break
            self.goto(f"https://stackoverflow.com/tags/{quote_plus(tag)}/topusers")
            for c in self._eval(js):
                if c["profile_url"] not in {x["profile_url"] for x in out}:
                    c["headline"] = c.get("headline") or f"Top [{tag}] on Stack Overflow"
                    out.append(c)
                if len(out) >= cap:
                    break
        return out[:cap]

    def search_hackernews(self, job):
        cap = job.get("cap") or 25
        keywords = [k.lower() for k in ((job.get("requirements") or {}).get("skills") or [])
                    + ((job.get("requirements") or {}).get("titles") or []) if k]
        self.goto("https://news.ycombinator.com/submitted?id=whoishiring")
        # latest "who wants to be hired" thread
        href = self.page.evaluate("""() => {
          const a = [...document.querySelectorAll('.titleline a, a[href*="item?id="]')]
            .find(x => /who wants to be hired/i.test(x.innerText || ''));
          return a ? a.href : null;
        }""")
        if not href:
            return []
        self.goto(href)
        rows = self._eval("""() => {
          return [...document.querySelectorAll('.comtr')].slice(0, 80).map(tr => {
            const user = (tr.querySelector('.hnuser') || {}).innerText || '';
            const text = (tr.querySelector('.commtext') || {}).innerText || '';
            const a = tr.querySelector('a[href*="item?id="]');
            return {user, text, url: a ? a.href : location.href, id: tr.id};
          }).filter(r => r.text && r.text.length > 80);
        }""")
        out = []
        for r in rows:
            low = (r.get("text") or "").lower()
            if keywords and not any(k in low for k in keywords):
                continue
            loc_m = re.search(r"(?im)^\\s*location\\s*[:\\-]\\s*(.+)$", r.get("text") or "")
            out.append({
                "profile_url": r.get("url") or href,
                "full_name": r.get("user") or "HN user",
                "headline": (r.get("text") or "").splitlines()[0][:400],
                "location": (loc_m.group(1).strip()[:200] if loc_m else ""),
            })
            if len(out) >= cap:
                break
        return out

    def search_ycombinator(self, job):
        q = " ".join(((job.get("requirements") or {}).get("skills") or [])[:2]
                     or [job.get("title") or "software"])
        self.goto(f"https://www.ycombinator.com/companies?query={quote_plus(q)}")
        cards = self._eval("""() => {
          const out = [];
          for (const a of document.querySelectorAll('a[href*="/companies/"]')) {
            const href = a.href.split('?')[0];
            if (/\\/companies\\/?$/.test(href) || out.find(x => x.profile_url === href)) continue;
            const lines = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            if (!lines[0]) continue;
            out.push({profile_url: href, full_name: lines[0], headline: lines.slice(1, 3).join(' — ').slice(0, 400), location: ''});
            if (out.length >= 15) break;
          }
          return out;
        }""")
        return cards[: job.get("cap") or 25]

    def search_webpages(self, job):
        return self._visit_seeds(job.get("seeds") or [], job.get("cap") or 25)

    def search_boards(self, job):
        seeds = [u for u in (job.get("seeds") or [])
                 if any(b in u for b in ("greenhouse", "lever.co", "ashbyhq"))]
        return self._visit_seeds(seeds, job.get("cap") or 25)

    def _visit_seeds(self, seeds, cap):
        out = []
        js = """
        () => {
          const people = [];
          for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
              const d = JSON.parse(s.textContent);
              const arr = Array.isArray(d) ? d : [d];
              for (const n of arr) {
                if (n && (n['@type'] === 'Person' || (Array.isArray(n['@type']) && n['@type'].includes('Person')))) {
                  people.push({profile_url: n.url || location.href, full_name: n.name || '',
                               headline: n.jobTitle || n.description || '', location: (n.address && n.address.addressLocality) || ''});
                }
              }
            } catch (e) {}
          }
          for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
            const email = a.href.replace(/^mailto:/i, '').split('?')[0];
            const name = (a.innerText || email.split('@')[0]).trim();
            people.push({profile_url: location.href, full_name: name, headline: '', location: '', email});
          }
          return people.filter(p => p.full_name).slice(0, 20);
        }"""
        for url in seeds[:12]:
            if " " in url:
                url = url.split()[0]
            if not url.startswith("http"):
                continue
            try:
                self.goto(url)
            except Exception as exc:
                log.warning("seed %s: %s", url, exc)
                continue
            for c in self._eval(js):
                c["profile_url"] = c.get("profile_url") or url
                out.append(c)
                if len(out) >= cap:
                    return out
        return out
