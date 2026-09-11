# Milestone 1 — LinkedIn Sourcing & Outreach via browser automation

Owner: Meda. Drafted 8 Sep 2026. Depends on M0 being deployed (public `jobs.tweezgroup.com` URL is required — candidates must be able to open the link).

## Principle
The agent never asks a candidate to email a CV. Every message ends with a **personalised apply link** into the Applicant System. Once the candidate uploads the CV, the existing pipeline (Claude score → Drive TWEEZ-CV-BANK → ClickUp task for Mehdi) takes over untouched. Sourcing only has to get people to the link.

## End-to-end flow

```
Role (requirements) ──> Claude builds LinkedIn search (title/keywords/location/booleans)
      │
      ▼
Browser worker (Playwright, recruiter's own LinkedIn session) runs the search,
scrapes result cards (name, headline, location, profile URL, current company)
      │
      ▼
Claude pre-scores each profile against the role requirements (0–100, one-line reason)
      │
      ▼
Admin "Sourcing" tab: shortlist queue ── recruiter approves / rejects (batch)
      │
      ▼
POST /api/v1/applicants {role, full_name, linkedin_url, source: linkedin}
      → record in status "sourced" + personalised apply_url (token)
      │
      ▼
Browser worker sends: (1) connection request with note, or InMail/Open-profile message
                      (2) on accept → message with the apply link
                      (3) reminder after 4 days if link not opened / not applied
      │
      ▼
Candidate opens link → form pre-filled (name, LinkedIn URL, source=LinkedIn) → uploads CV
      → status "new" → existing M3/M4 automation (score, Drive, ClickUp, test email)
```

## What gets built

### 1. Applicant System changes (no browser needed — build first)
- New statuses before `new`: `sourced` → `contacted` → `link_opened` (then the normal flow). Not synced to ClickUp until the CV arrives (keeps the board clean), configurable.
- **Tokenised apply link**: `/apply/<role>?t=<token>` pre-fills name + LinkedIn URL, locks `source=LinkedIn`, records `link_opened` on first open, and attaches the upload to the sourced record instead of creating a duplicate.
- New tables: `SourcingSearch` (role, query, filters, run date, results count) and `SourcedProfile` (profile URL, name, headline, location, company, pre-score, reason, decision, outreach step, last message at, reply text).
- Admin **Sourcing** tab: per role → run/view searches, shortlist queue with approve/reject, outreach queue with message preview, funnel stats (found → shortlisted → contacted → accepted → link opened → applied → selected).
- API additions for the worker: `GET /api/v1/sourcing/queue` (approved, next step due), `POST /api/v1/sourcing/results` (scraped profiles), `POST /api/v1/sourcing/<id>/outreach` (step done, reply captured).
- Message templates per role (connection note ≤300 chars; follow-up with link; reminder) stored on the Role, with `{first_name}`, `{role}`, `{brand}`, `{apply_url}` placeholders. Claude personalises one line from the profile headline.

### 2. Browser worker (separate process — NOT on Render free tier)
- Python + Playwright, **headed Chromium with a persistent profile** logged into the recruiting LinkedIn account. Runs on Meda's PC (same idea as Claude Desktop driving the browser) or a small VPS with a desktop session; the app only talks to it through the API above.
- Jobs: `search(role)` → scrape → post results; `outreach()` → pull the queue, open each profile, send the step's message, post the outcome; `inbox()` → read replies, post them back (Claude classifies: interested / not now / question / negative) and flags questions for a human.
- Human-like behaviour: random delays 20–90 s between actions, working hours only, scroll before clicking, no more than one session at a time.

### 3. Safety rails (LinkedIn ToS — accepted risk, minimised)
- **Human-confirm mode is the default**: the worker prepares each message and the recruiter clicks "Send" in the admin (worker executes). Full auto-send is a per-role toggle to enable after 2 weeks without warnings.
- Hard caps, env-configurable: 20 connection requests/day, 40 messages/day, 100 profile views/day (week 1 at half), automatically paused for 48 h if LinkedIn shows any warning/captcha.
- Use a dedicated recruiting account (Mehdi's or a "Tweezgroup Talent" profile), never the founders' accounts. Reply to candidates as that person; messages business-only.
- Every message and reply logged as an Event on the candidate (audit trail).
- Option B if the risk is unacceptable: route the send step through ProspectHalo (already connected, handles LinkedIn rate limits), keep our sourcing/scoring/link logic identical.

### 4. Search strategy per role
Claude derives from the role's requirements: 3–5 title variants, must-have keywords, location/remote filter, seniority, languages; runs them as separate searches and dedupes on profile URL. Sales Navigator not required for V1 (standard search + filters); can be added later.

## Build order
| Step | Work | Est. |
|---|---|---|
| 0 | Deploy M0 (link must be public) + decide LinkedIn account | — |
| 1 | Statuses, tokenised apply link, sourcing tables, API, admin Sourcing tab, templates | 2 days |
| 2 | Worker: login profile, search + scrape, post results, Claude pre-score | 2 days |
| 3 | Worker: outreach steps in human-confirm mode, reply reading, funnel stats | 2 days |
| 4 | Pilot on 1 role (e.g. B2B Manager), 50 profiles, 2 weeks, review funnel | pilot |
| 5 | Enable auto-send within caps, add reminders, weekly digest to the team | 1 day |

## Decisions needed from Meda
1. Which LinkedIn account the worker uses (recommend: Mehdi or a dedicated Tweezgroup Talent account).
2. Where the worker runs: his PC (free, only when the PC is on) vs a small VPS (~€5/month, always on).
3. Human-confirm only, or auto-send after the pilot.
4. First pilot role.

## Success metrics (pilot)
Connection acceptance ≥ 30 %, link-open ≥ 50 % of accepted, application ≥ 40 % of opens, ≥ 5 SELECTED candidates from 50 profiles, zero LinkedIn warnings.

## Multi-channel sourcing (added 8 Sep 2026)
LinkedIn stays the only channel that needs the browser worker. Everything else is either inbound (post the link) or an official API, so it carries no account risk. All channels land on the same tokenised apply link; the form's "Which platform did you apply from?" select gains the new sources (Indeed, Welcome to the Jungle, Google Jobs, Torre, Referral, Job board other).

### Tier 1 — inbound, free, build with M2 (job posting workflow)
| Channel | Integration | Work |
|---|---|---|
| **Google for Jobs** | `JobPosting` JSON-LD on every `/apply/<slug>` page (title, description, datePosted, validThrough, hiringOrganization, jobLocationType TELECOMMUTE, applicantLocationRequirements) + sitemap | 0.5 day, permanent free traffic |
| **Indeed** (FR/EU + DZ) | XML job feed at `/jobs/feed.xml` generated from open roles, external apply URL → our link; Indeed indexes it automatically | 0.5 day |
| **Welcome to the Jungle** (FR) | Manual post per role with external apply URL (paid employer page) | manual, M2 checklist |
| **Remote boards** — Torre, RemoteOK, We Work Remotely, Remotive, Himalayas | Manual post with external apply URL; Torre also accepts opportunities via API | manual / 0.5 day for Torre API |
| **Emploitic / Emploi Partner** (Algeria) | Manual post with external apply URL for Algiers-based roles | manual |
| **Facebook groups / Slack & Discord communities** (e-commerce, Amazon sellers, TikTok Shop) | Template post + link, tracked by the source field | template only |

### Tier 2 — outbound with official APIs (safe to automate in the worker's place)
| Channel | Use | Integration |
|---|---|---|
| **Torre** | Remote talent search for all roles | Public people-search API → same pre-score → shortlist → message with link |
| **GitHub** | Data Scientist / dev profiles | Search API by language/location; contact via profile email |
| **Apollo** (connected) | Fallback contact channel | Enrich LinkedIn profile → verified email → Gmail message with link when LinkedIn gets no reply |
| **Upwork / Malt** | TikTok video creators, graphic designers, content creators | Search freelancers by skill; message carries the link (their messaging, our form) |
| **Behance / Dribbble** | Graphic Designer, Content Creator portfolios | Search + platform message with link |

### Tier 3 — employee referrals (cheapest quality channel)
New `/refer` page on the app: a team member enters candidate name + LinkedIn/email + role → system creates the sourced record, generates the tokenised link and emails the candidate; referrer stored on the record (`referred_by`) and shown in the ClickUp task. 0.5 day.

### Data model additions
`SourcedProfile.channel` (linkedin / torre / github / apollo / upwork / referral …), `Applicant.referred_by`, extended `source` enum. Funnel stats in the Sourcing tab broken down per channel so the team sees which platform actually produces SELECTED candidates.

### Priority order
1. Google for Jobs schema + Indeed feed (M2, one day, free volume).
2. `/refer` page.
3. Torre + Apollo as API-safe outbound alongside the LinkedIn pilot.
4. Welcome to the Jungle / Emploitic / remote boards as a manual posting checklist in M2.
5. Upwork/Malt/Behance only when a creative role opens.
