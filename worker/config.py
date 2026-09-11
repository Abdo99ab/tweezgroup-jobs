import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv()

APP_URL = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
PROFILE_DIR = Path(os.environ.get("LINKEDIN_PROFILE_DIR", Path.home() / ".tweez-linkedin-profile"))
HEADLESS = os.environ.get("HEADLESS", "0") == "1"          # keep headed: LinkedIn is far less suspicious
SLOW_MO = int(os.environ.get("SLOW_MO_MS", "60"))           # ms added to every Playwright action
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))   # idle wait between passes
INBOX_EVERY = int(os.environ.get("INBOX_EVERY_MINUTES", "30"))
LOCALE = os.environ.get("BROWSER_LOCALE", "fr-FR")
TIMEZONE = os.environ.get("BROWSER_TZ", "Europe/Paris")
MAX_ACTIONS_PER_PASS = int(os.environ.get("MAX_ACTIONS_PER_PASS", "8"))
