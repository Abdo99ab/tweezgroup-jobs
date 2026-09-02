# Tweezgroup Applicant System (HR Superagent — Milestone 0)

The single source of truth for candidates. A candidate applies with one link (or the form embedded on a company website); the CV lands in **TWEEZ-CV-BANK on Google Drive** (in the role's subfolder); Claude writes a screening summary; a **ClickUp task assigned to Mehdi Mahcene** is created in the role's list and the summary is posted as a comment **tagging Ahmidou, Taoufik Mousselmal and Abderrahmane Hammia**.

```
candidate ──> /apply/<role> (link or iframe) ──> Postgres record + CV → Google Drive (TWEEZ-CV-BANK/<Role> CVs)
                                                     │
                                                     ├──> Claude: score + summary (against the role's requirements)
                                                     └──> ClickUp task (assignee: Mehdi) + summary comment (cc Ahmidou, Taoufik, Abderrahmane)
                       recruiter admin (/admin) ◄────┘        agent API (/api/v1) for Milestones 3–4
```

## What's in V1

| Area | What it does |
|---|---|
| Public | `/jobs` list, `/apply/<slug>` form (name, email, phone, LinkedIn, location, **years of experience in this field**, CV PDF/DOC/DOCX, note, consent checkbox), confirmation page, `/privacy` notice; `?embed=1` for iframes |
| Admin (`/admin`) | Login, pipeline dashboard, roles (create/edit, **screening requirements = the agent's knowledge base**), applicant list with filters + full-text search over CV text, detail page with status change, notes, activity log, CV view, CSV export, GDPR delete |
| Agent API (`/api/v1`) | API-key protected. List/read roles and applicants (incl. extracted CV text), update status/score/summary, log events (e.g. `email_sent`), register sourced candidates and get their personalised apply link, pipeline stats |
| Auto-summary | On every application Claude (`SUMMARY_MODEL`, default claude-haiku-4-5) reads the CV against the role's requirements and writes score 0–100, recommendation, highlights, matches, gaps and flags |
| ClickUp | Task created on apply in the role's list, **assigned to Mehdi Mahcene**; the summary posted as a comment **@-tagging Ahmidou, Taoufik Mousselmal, Abderrahmane Hammia**; every status change pushed with the board's exact status names. People are matched by ClickUp display name (`CLICKUP_ASSIGNEE`, `CLICKUP_MENTIONS`) |
| Storage | CVs uploaded to Google Drive **TWEEZ-CV-BANK**, into the role's **designated subfolder** (pinned per role — all 15 existing folders are pre-mapped by `flask seed-roles`; an unpinned role is matched to the best-named existing folder, e.g. "Head of Accounting" → "Head Acountant CVs"; only a role with no folder gets a new `<Role> CVs`). File name **`<CODE><DDMMYYYY> - <Candidate>.pdf`**, e.g. `HM31082026 - Sara Benali.pdf`. Drive link stored on the record and in the ClickUp task. S3 and local disk remain available |
| GDPR | Consent timestamp, `retention_until` (12 months by default), `flask purge-expired` anonymises expired rows and deletes files; per-applicant delete in admin and API |

Statuses mirror the ClickUp recruiting lists exactly, plus an internal `new` for unscreened applications:

| App status | ClickUp status |
|---|---|
| `new` | — (not on the board yet) |
| `filtered` | FILTRED APPLICATION |
| `selected` | SELECTED/ IN PROGRESS |
| `test_sent` | TEST SENT |
| `test_returned` | TEST RETURNED |
| `interview_done` | INTERVIEW DONE |
| `interview2_done` | 2ND INTERVIEW DONE |
| `contract_sent` | CONTRACT SENT |
| `rejected` | REJECTED |
| `hired` | HIRED |

`CLICKUP_CREATE_ON=apply` (default) creates a task for every application (it lands in the list's first column, FILTRED APPLICATION). Set `CLICKUP_CREATE_ON=filtered` to create tasks only once an applicant is moved to `filtered`.

### Role codes (CV file names)

| Role | Code | Role | Code |
|---|---|---|---|
| Head of Marketing | HM | Graphic Designer | GD |
| TikTok Shop Manager | TTS | Content Creator | CC |
| Amazon Account Manager | AAM | Sourcing Manager | SM |
| Head of Accounting | HA | Brand Manager | BM |
| Customer Support | CS | B2B Manager | BB |
| Bookkeeper | BK | B2B Prospector | BP (auto) |
| Data Scientist | DA | Chief Operating Officer | COO (auto) |

Codes are stored on each role (editable in `/admin`); unknown titles get their initials. The date is the application date in Paris time, `DDMMYYYY`. `flask --app wsgi seed-roles` creates all 16 roles (closed) with codes and their Drive folders pinned — open the ones you're hiring for.

### Automation, step by step

Right after the candidate sees the confirmation page (background thread, ~5–15 s):

1. Claude summarises the CV → `score`, `ai_summary` on the record.
2. ClickUp task created in the role's list, assignee Mehdi Mahcene, description with contact details, declared experience, Drive link to the CV, link to the admin record.
3. Comment posted on the task with the summary, ending with `cc @Ahmidou, @Taoufik Mousselmal, @Abderrahmane Hammia` (real mentions; falls back to plain text if the API rejects the rich format).

Each step is idempotent. `flask --app wsgi process-pending` (or `POST /api/v1/applicants/<id>/process`, or the "Re-run" button in the admin) retries whatever is missing.

## Run it locally (2 minutes)

```bash
pip install -r requirements.txt
cp .env.example .env            # edit at least ADMIN_PASSWORD, API_KEY, SECRET_KEY
flask --app wsgi rotate-keys    # prints random SECRET_KEY / API_KEY to paste into .env
flask --app wsgi seed           # creates a sample "Head of Marketing" role
# Linux / macOS:
gunicorn -w 2 -b 0.0.0.0:8000 wsgi:app
# Windows (gunicorn needs Unix fcntl — use Flask's server instead):
flask --app wsgi run --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000/apply/head-of-marketing and http://localhost:8000/admin (user `admin`).

Defaults without a `.env`: SQLite in `./data`, CVs on local disk in `./data/uploads`, ClickUp off.

## Deploy for free (Render + Supabase)

Verified Sept 2026: Render's free web service needs no card and supports a custom domain; Supabase's free plan is permanent (500 MB Postgres + 1 GB S3-compatible file storage). Two caveats, one fix: Render spins the service down after 15 idle minutes (~1 min to wake) and Supabase pauses a project after a week without database activity — a free UptimeRobot monitor hitting `/jobs` every 5 minutes keeps both alive.

1. **Supabase** (supabase.com → New project, region Frankfurt, free plan): *Project Settings → Database → Connection string (URI, Transaction pooler)* → `DATABASE_URL`. Only the database is used; CVs go to Drive.
2. **Google Drive access** (as the owner of TWEEZ-CV-BANK, mehdi@tweezgroup.com — or any account with edit rights on it):
   - console.cloud.google.com → new project → *APIs & Services → Enable* "Google Drive API" → *Credentials → OAuth client ID → Desktop app* → download `client_secret.json`. (First time: configure the OAuth consent screen as *Internal*.)
   - `pip install google-auth-oauthlib && python scripts/gdrive_auth.py client_secret.json` → sign in → paste the printed `GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN` into the environment.
   - If Google shows **Error 400: redirect_uri_mismatch**, the JSON is a Web client. In that OAuth client add Authorized redirect URI `http://127.0.0.1:8080/` (exact, with trailing slash), or create a **Desktop app** client instead and re-download the JSON. Then re-run the script.
   - Alternative for IT-managed setups: a service account with domain-wide delegation, `GOOGLE_SERVICE_ACCOUNT_JSON` + `GOOGLE_IMPERSONATE_USER=mehdi@tweezgroup.com`. A plain service account cannot upload into a My Drive folder.
3. **Claude**: an Anthropic API key (console.anthropic.com) → `ANTHROPIC_API_KEY`. Haiku costs well under a cent per CV.
4. **Push this folder to a GitHub repo** (the `render.yaml` blueprint is at the root).
5. **Render** (render.com, sign in with GitHub) → *New → Blueprint* → pick the repo → fill in the `sync: false` variables (`ADMIN_PASSWORD`, `DATABASE_URL`, `GOOGLE_OAUTH_*`, `ANTHROPIC_API_KEY`, `CLICKUP_API_TOKEN`, `CLICKUP_LIST_ID`, `PUBLIC_BASE_URL`, `FRAME_ANCESTORS`). `SECRET_KEY` and `API_KEY` are generated for you (copy `API_KEY` from *Environment* for the agent).
6. **ClickUp**: personal API token (*Settings → Apps*). Each role's list ID goes in the role's *ClickUp list ID* field in `/admin` (from the list URL `/v/li/<id>`); `CLICKUP_LIST_ID` is the fallback. Names in `CLICKUP_ASSIGNEE` / `CLICKUP_MENTIONS` must match the members' ClickUp display names (partial match is fine).
7. **Domain**: Render → *Settings → Custom Domains* → add `jobs.tweezgroup.com`, then create the CNAME it shows at your DNS provider. Set `PUBLIC_BASE_URL=https://jobs.tweezgroup.com`.
8. **Keep-alive**: uptimerobot.com (free) → HTTP monitor on `https://jobs.tweezgroup.com/jobs`, every 5 min.
9. **Retention job**: Render's free plan has no cron; hit it from the agent or any scheduler: `curl -X POST -H "X-API-Key: …" https://jobs.tweezgroup.com/api/v1/maintenance/purge-expired` (weekly is plenty).
10. **First roles**: `/admin` → New role → paste the screening requirements and the role's ClickUp list ID.

Any other host works the same way (Docker image, `PORT` env respected): Koyeb, Fly, a DigitalOcean droplet with `docker compose up -d`, etc.

### Use it on the company websites

Every role has two URLs — the full page for LinkedIn/job-board posts, and an embed for zenpur.fr / nubiana etc.:

```html
<iframe src="https://jobs.tweezgroup.com/apply/head-of-marketing?embed=1"
        style="width:100%;min-height:1150px;border:0" title="Apply — Head of Marketing"></iframe>
```

Set `FRAME_ANCESTORS="https://zenpur.fr https://www.zenpur.fr https://nubiana.com"` (space-separated) to allow those sites to frame the form; everything else is refused. The snippet is also shown per role on the admin dashboard.

## Agent API contract

Send `X-API-Key: <API_KEY>` on every call. All ids are the applicant's `public_id` (short opaque string).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/roles?open=1` | Roles with `requirements` (knowledge base) and applicant counts |
| PATCH | `/api/v1/roles/<slug>` | Update `requirements`, `description`, `is_open`… |
| GET | `/api/v1/applicants?status=new&unscored=1&role=<slug>&since=<ISO>&limit=50&include_cv_text=1` | Poll for work |
| GET | `/api/v1/applicants/<id>` | Full record incl. `cv_text` and `events` |
| GET | `/api/v1/applicants/<id>/cv` | Raw CV file |
| PATCH | `/api/v1/applicants/<id>` | `{"status","score" (0-100),"ai_summary","notes","actor"}` — status syncs to ClickUp, summary posted as a comment |
| POST | `/api/v1/applicants/<id>/events` | `{"kind":"email_sent","message":"..."}` — audit trail for Gmail follow-ups etc. |
| POST | `/api/v1/applicants` | `{"role","full_name","email","linkedin_url","source":"linkedin"}` → creates the record + ClickUp task, returns `apply_url` to send the candidate |
| DELETE | `/api/v1/applicants/<id>` | Purge personal data (GDPR request) |
| GET | `/api/v1/stats` | Counts per role and status |
| POST | `/api/v1/maintenance/purge-expired` | Run the GDPR retention purge (for hosts without cron) |

### The screening loop the agent runs (Milestone 3 preview)

```
every 15 min:
  roles = GET /roles?open=1                          # requirements = what "good" looks like
  for a in GET /applicants?status=new&unscored=1&include_cv_text=1:
      score, summary = claude(a.cv_text, role.requirements)
      PATCH /applicants/{a.id}  {status: "filtered"|"rejected", score, ai_summary}   # "filtered" creates the ClickUp task
      if filtered: send Gmail invite; POST /applicants/{a.id}/events {kind:"email_sent"}
```

## Project layout

```
app/__init__.py   app factory + CLI (seed, seed-roles, process-pending, purge-expired, rotate-keys)
app/naming.py     role codes, CV file names, Drive folder matching
app/config.py     all settings from environment
app/models.py     Role, Applicant, Event
app/storage.py    S3 (Spaces) / local storage
app/extract.py    PDF/DOCX text extraction
app/clickup.py    ClickUp sync (task, assignee, mentions, statuses)
app/summarize.py  CV auto-summary with Claude
app/pipeline.py   post-application automation (summary → task → comment), retries
scripts/gdrive_auth.py  one-time Google Drive token helper
app/public.py     candidate-facing routes
app/admin.py      recruiter admin
app/api.py        agent API
app/templates/    server-rendered pages (no JS build step)
```

## Security notes

- Change `ADMIN_PASSWORD`, `API_KEY` and `SECRET_KEY` before exposing publicly; serve over HTTPS only.
- The bucket must be private; CVs are only served through the app (admin session or API key).
- Uploads are limited to PDF/DOC/DOCX and 10 MB (`MAX_UPLOAD_MB`).
- Single shared admin login is deliberate for V1; per-user accounts and roles are Milestone 6 (Governance).
