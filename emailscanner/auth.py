"""Google OAuth.

Locally: `python -m emailscanner auth --client-secret client_secret.json` opens a
browser once, saves token.json, and prints the values to paste into GitHub secrets.

In GitHub Actions: credentials are rebuilt from GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .config import ROOT

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",          # read, label, trash, send
    "https://www.googleapis.com/auth/gmail.settings.basic",  # create filters
    "https://www.googleapis.com/auth/spreadsheets",          # review sheet + trackers
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKEN_FILE = ROOT / "token.json"


def get_credentials() -> Credentials:
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if cid and secret and refresh:
        creds = Credentials(
            token=None,
            refresh_token=refresh,
            token_uri=TOKEN_URI,
            client_id=cid,
            client_secret=secret,
            scopes=SCOPES,
        )
    elif TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    else:
        raise SystemExit(
            "No Google credentials found. Run `python -m emailscanner auth` locally first, "
            "or set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN."
        )
    if not creds.valid:
        creds.refresh(Request())
    return creds


def run_local_auth(client_secret_path: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    info = json.loads(Path(client_secret_path).read_text(encoding="utf-8"))
    info = info.get("installed") or info.get("web") or {}
    print("\nSigned in. Saved token.json (keep it private, it's gitignored).\n")
    print("Add these as GitHub repository secrets:\n")
    print(f"GOOGLE_CLIENT_ID      = {info.get('client_id', creds.client_id)}")
    print(f"GOOGLE_CLIENT_SECRET  = {info.get('client_secret', creds.client_secret)}")
    print(f"GOOGLE_REFRESH_TOKEN  = {creds.refresh_token}")
