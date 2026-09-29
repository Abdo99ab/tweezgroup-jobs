# LinkedIn sourcing worker (Milestone 1)

A small Playwright process that drives a **headed Chromium logged into the recruiting LinkedIn account** and does
only what the applicant system tells it to: run searches, send connection requests, check acceptance, send the
message with the candidate's personal apply link, send one reminder, and read replies. All decisions — which
profiles, which text, when, how many per day, pause on warnings — live in the app (`app/sourcing.py`), so the
worker is deliberately dumb.

```
 admin /admin/sourcing ──queues search──▶ app DB ◀──polls──  worker (this folder, on your PC or a VPS)
 recruiter approves + clicks Send ─────▶ queue  ──────────▶  Chromium: LinkedIn search / connect / message / inbox
 candidate clicks jobs.tweezgroup.com/r/<token> ──▶ pre-filled apply form ──▶ CV ──▶ normal pipeline (score, Drive, ClickUp)
```

## Safety rails (enforced by the app, not by this code)
- **Human-confirm mode** by default: every invitation/message waits for a recruiter's "Send" in the admin.
  Per-role *Auto-send* checkbox turns that off once the pilot is clean.
- Daily caps (env on the app): `SOURCING_CAP_CONNECTS=20`, `SOURCING_CAP_MESSAGES=40`, `SOURCING_CAP_VIEWS=100`;
  first week at half. Weekdays only, `SOURCING_HOURS=09:00-18:00` Europe/Paris.
- Any checkpoint / captcha / "unusual activity" page → the worker reports it and the app **pauses everything for
  48 h** (banner + Resume button in `/admin/sourcing`).
- Random 20–90 s between actions, scrolling, human typing speed, one browser session, persistent profile.
- Use a dedicated recruiting account (Mehdi or "Tweezgroup Talent"), never a founder's account.

LinkedIn's terms forbid automation; this is a known, accepted risk minimised by the rules above. If it becomes
unacceptable, keep everything and only replace the send step with ProspectHalo.

## Recommended setup: app on Render, worker on your PC

The Flask app stays on [jobs.tweezgroup.com](https://jobs.tweezgroup.com). Only Chromium (LinkedIn login)
needs to run on a desktop — Mehdi’s PC. The worker polls the live API over HTTPS with `X-API-Key`.
No VPN, no second Flask process.

```bash
cd tweezgroup-jobs
pip install -r worker/requirements.txt
playwright install chromium
cp worker/.env.example worker/.env
# Edit worker/.env:
#   APP_URL=https://jobs.tweezgroup.com
#   API_KEY=<exact value from Render → web service → Environment → API_KEY>
python -m worker login                  # Chromium: log into the recruiting LinkedIn account, press Enter
python -m worker check                  # must print API OK + Mode: remote app …
python -m worker run                    # leave this open; Ctrl+C to stop
```

Then open **https://jobs.tweezgroup.com/admin** → Web Sourcing → **Discover**. The same Chromium window
runs LinkedIn, then GitHub / GitLab / SO / HN / YC. Candidates appear on the live site.

If `check` says **unauthorized**, the key does not match Render’s `API_KEY` (common when someone copied a
local `.env`). If it hangs or 502s, open the jobs site once in a browser to wake Render free tier, then retry.

**Only for local development:** set `APP_URL=http://localhost:8000` and use the same `API_KEY` as the local
repo `.env`, and run Flask yourself. Discover and the worker must hit the same app.

Windows: Task Scheduler → *At log on* → `python -m worker run` in the repo folder, "Run only when user is logged
on" (the browser needs a desktop). The worker idles outside working hours and while paused.

**One worker, many roles.** `python -m worker run` is a single Chromium logged into one LinkedIn account. Discover on two roles at once is fine: each role has its own candidate list, and the worker plays their LinkedIn searches and public-site tabs back-to-back (round-robin). Do **not** start a second `worker run` on the same `LINKEDIN_PROFILE_DIR` — Playwright will fight over the profile. True parallel LinkedIn needs a second recruiting account, a second profile folder, and a second worker process.

## Run on a VPS (always on)

```bash
# Ubuntu 22.04, Docker installed; from the repo root
cp worker/.env.example .env && nano .env            # APP_URL, API_KEY
docker compose -f worker/docker-compose.yml build
# one-time login: copy the profile folder made by `python -m worker login` on your PC into worker/data/linkedin-profile
docker compose -f worker/docker-compose.yml up -d
docker compose -f worker/docker-compose.yml logs -f
```

Chromium runs headed under Xvfb inside the container. Prefer a French/EU region so the IP matches the account.
If LinkedIn asks for verification after the move, log in once via VNC or re-copy a fresh profile from your PC.

## How a pass works (`python -m worker once`)
1. `GET /api/v1/sourcing/status` — paused? outside hours? delays to use.
2. Pending searches → LinkedIn people search → cards posted in batches of 25 to `/searches/<id>/results`
   (the app pre-scores them with Claude and they appear in *To review*).
3. `GET /api/v1/sourcing/queue` → up to `MAX_ACTIONS_PER_PASS` actions (already filtered by caps and confirmation):
   `connect` (invite + note), `check_accept` (profile shows 1st degree?), `message` / `reminder` (with the link).
   Each result goes to `/profiles/<id>/outcome`.
4. Every `INBOX_EVERY_MINUTES`: unread threads → `/sourcing/inbox`; the app classifies replies
   (interested / question / not now / negative) and stops reminders where a human should answer.

## When LinkedIn changes its page
Selectors live at the top of `worker/linkedin.py` (`BTN_CONNECT`, `MSG_BOX`, …) as ordered candidate lists in
English and French; add the new one first. Search results are read as text lines (name, headline, location)
rather than class names, so they survive most redesigns.

## Environment (worker/.env)
| Var | Default | Meaning |
|---|---|---|
| `APP_URL` | https://jobs.tweezgroup.com | live applicant system (use localhost only if Flask is local too) |
| `API_KEY` | — | same value as that app's `API_KEY` (Render Environment for production) |
| `LINKEDIN_PROFILE_DIR` | ~/.tweez-linkedin-profile | persistent Chromium profile (the login) |
| `HEADLESS` | 0 | keep 0 |
| `POLL_SECONDS` | 300 | idle wait between passes |
| `INBOX_EVERY_MINUTES` | 30 | inbox check interval |
| `MAX_ACTIONS_PER_PASS` | 8 | actions per pass (caps still apply) |
| `BROWSER_LOCALE` / `BROWSER_TZ` | fr-FR / Europe/Paris | should match the account |
