import argparse
import logging
import sys

from . import config
from .client import Api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


def browser():
    from playwright.sync_api import sync_playwright
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(config.PROFILE_DIR), headless=config.HEADLESS, slow_mo=config.SLOW_MO, locale=config.LOCALE,
        timezone_id=config.TIMEZONE, viewport={"width": 1280, "height": 860},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def cmd_login():
    pw, ctx, page = browser()
    page.goto("https://www.linkedin.com/login")
    print("\nLog into the RECRUITING LinkedIn account in the browser window, then press Enter here.")
    input()
    from .linkedin import LinkedIn
    ok = LinkedIn(page).logged_in()
    print("Session saved in", config.PROFILE_DIR, "— logged in:" if ok else "— NOT logged in:", ok)
    ctx.close()
    pw.stop()
    return 0 if ok else 1


def cmd_check():
    api = Api()
    try:
        st = api.status()
        print("API OK:", config.APP_URL, "| paused:", st.get("paused_until"), "| caps left:", st["caps"]["left"],
              "| pending searches:", st["pending_searches"], "| due actions:", st["due_actions"])
    except Exception as exc:
        print("API ERROR:", exc)
        return 1
    pw, ctx, page = browser()
    from .linkedin import LinkedIn
    ok = LinkedIn(page).logged_in()
    print("LinkedIn session valid:", ok, "(run `python -m worker login` if False)")
    ctx.close()
    pw.stop()
    return 0 if ok else 1


def cmd_run(once=False):
    from .linkedin import LinkedIn
    from .main import Worker
    api = Api()
    pw, ctx, page = browser()
    li = LinkedIn(page)
    if not li.logged_in():
        log.error("LinkedIn session is not valid — run `python -m worker login` first")
        ctx.close()
        pw.stop()
        return 1
    w = Worker(api, li)
    try:
        if once:
            print(w.pass_once())
        else:
            w.run_forever()
    finally:
        ctx.close()
        pw.stop()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="worker", description=__doc__)
    p.add_argument("command", choices=["login", "run", "once", "check"])
    a = p.parse_args(argv)
    if a.command == "login":
        return cmd_login()
    if a.command == "check":
        return cmd_check()
    return cmd_run(once=a.command == "once")


if __name__ == "__main__":
    sys.exit(main())
