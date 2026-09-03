"""One-time helper: obtain a Google refresh token (Drive + Gmail send) for the account that owns TWEEZ-CV-BANK.

Also enable the "Gmail API" in the same Google Cloud project so test invitations can be sent from this account.

Google Cloud Console (avoids Error 400: redirect_uri_mismatch):

1. Enable the Google Drive API.
2. Credentials -> Create credentials -> OAuth client ID.
   Preferred: Application type = Desktop app, then download the JSON.
   If you already have a Web client, open it and add this Authorized redirect URI
   *exactly* (trailing slash included):

       http://127.0.0.1:8080/

3. pip install google-auth-oauthlib
4. python scripts/gdrive_auth.py client_secret.json
   Sign in as the Drive owner (e.g. mehdi@tweezgroup.com).
5. Copy the three printed values into the app's environment.

Usage:
  python scripts/gdrive_auth.py [client_secret.json] [--port 8080]
"""
import argparse
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/gmail.send"]
DEFAULT_PORT = 8080
HOST = "127.0.0.1"


def load_client(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "installed" in data:
        return "installed", data["installed"]
    if "web" in data:
        return "web", data["web"]
    sys.exit(f"{path} is not a Google OAuth client JSON (expected 'installed' or 'web').")


def main():
    parser = argparse.ArgumentParser(description="Obtain a Google Drive refresh token.")
    parser.add_argument("client_secrets", nargs="?", default="client_secret.json",
                        help="Path to the OAuth client JSON downloaded from Google Cloud Console")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Loopback port for the OAuth redirect (default {DEFAULT_PORT})")
    args = parser.parse_args()

    kind, client = load_client(args.client_secrets)
    redirect_uri = f"http://{HOST}:{args.port}/"

    print(f"OAuth client type: {kind}")
    print(f"Redirect URI this script will send: {redirect_uri}")
    print()

    if kind == "web":
        registered = client.get("redirect_uris") or []
        if redirect_uri not in registered and redirect_uri.rstrip("/") not in registered:
            print("This JSON is a WEB application client, not a Desktop app.")
            print("Google will return redirect_uri_mismatch until this URI is authorized:")
            print()
            print(f"    {redirect_uri}")
            print()
            print("Fix (either one):")
            print("  A. Cloud Console -> APIs & Services -> Credentials -> this OAuth client")
            print("     -> Authorized redirect URIs -> Add URI -> paste the line above -> Save")
            print("     Then wait a few seconds and re-run this script.")
            print("  B. Create a new OAuth client of type Desktop app, download that JSON,")
            print("     and pass it to this script instead (no redirect URI to register).")
            print()
            try:
                input("Press Enter after you have saved the redirect URI (or Ctrl+C to abort)... ")
            except EOFError:
                pass

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    creds = flow.run_local_server(
        host=HOST,
        bind_addr=HOST,
        port=args.port,
        prompt="consent",
        access_type="offline",
        open_browser=True,
        redirect_uri_trailing_slash=True,
    )
    if not creds.refresh_token:
        sys.exit(
            "Google did not return a refresh token. On the consent screen choose the "
            "Drive-owner account and tick every permission. If you previously granted "
            "access, revoke the app at https://myaccount.google.com/permissions and retry."
        )

    print()
    print("Paste these into .env:")
    print("GOOGLE_OAUTH_CLIENT_ID=" + client["client_id"])
    print("GOOGLE_OAUTH_CLIENT_SECRET=" + client["client_secret"])
    print("GOOGLE_OAUTH_REFRESH_TOKEN=" + creds.refresh_token)


if __name__ == "__main__":
    main()
