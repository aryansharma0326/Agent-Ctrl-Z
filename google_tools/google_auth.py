#Importing packages

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",  # read, create, change, delete events
    "https://www.googleapis.com/auth/gmail.compose",    # drafts (the email hold) + sending
]


def _saved_credentials() -> Credentials | None:
    """token.json from an earlier login, if it covers every scope we need."""
    if not TOKEN_FILE.exists():
        return None
    saved = json.loads(TOKEN_FILE.read_text())
    if not set(SCOPES) <= set(saved.get("scopes", [])):
        return None  # scopes changed since that login: log in again
    return Credentials.from_authorized_user_info(saved, SCOPES)


def get_credentials() -> Credentials:
    creds = _saved_credentials()
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # Testing-mode logins expire after 7 days: log in again

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"Missing {CREDENTIALS_FILE}\n"
                "Download the Desktop app client JSON from Google Auth platform > Clients "
                "and save it in the repo root as credentials.json.")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)  # opens your browser to log in

    TOKEN_FILE.write_text(creds.to_json())
    return creds


@lru_cache(maxsize=1)
def calendar_service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def gmail_service():
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)