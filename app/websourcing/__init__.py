"""Public-web candidate sourcing — free public sources only, adapter per source.

Hard rules baked into base.py: GET-only with an identifying bot User-Agent, robots.txt
respected, rate limits backed off (never bypassed), no logins, no CAPTCHAs, no private
profiles, no platforms that prohibit automated access. Evidence-labelled scoring
(Confirmed / Likely / Unknown), and outreach only ever on an explicit recruiter action.
"""
from .engine import (ADAPTERS, BROWSER_SITES, SITES, apply_filters, build_email,  # noqa: F401
                     claim_browser_job, enabled_sources, filters_from_args, ingest_external,
                     ingest_worker_profiles, is_paused, launch_prompt, move_to_outreach,
                     paused_role_ids, reap_stale_runs, report_browser_source, send_email,
                     set_paused, start_run, stop_role_runs)
