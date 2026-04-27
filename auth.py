"""Authentication for Lark/Feishu (user OAuth) and Google Calendar (OAuth).

Both sides cache tokens on disk and auto-refresh. First run pops a browser.
"""
from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

LARK_OAUTH_REDIRECT_PORT = 8765
LARK_OAUTH_REDIRECT_URI = f"http://localhost:{LARK_OAUTH_REDIRECT_PORT}/"

LARK_AUTH_HOST = {
    "feishu.cn": "https://accounts.feishu.cn",
    "larksuite.com": "https://accounts.larksuite.com",
}
LARK_API_HOST = {
    "feishu.cn": "https://open.feishu.cn",
    "larksuite.com": "https://open.larksuite.com",
}

LARK_SCOPES = [
    "calendar:calendar:readonly",
    "calendar:calendar.event:read",
    "calendar:calendar.free_busy:read",
    "offline_access",
]


def get_google_service(credentials_path: str, token_path: str):
    """Return an authenticated Google Calendar v3 service. First run opens browser."""
    creds: Optional[Credentials] = None
    token_file = Path(token_path)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


class LarkAuth:
    """Lark/Feishu user-access-token manager with OAuth 2.0 code flow."""

    def __init__(self, domain: str, app_id: str, app_secret: str, token_path: str):
        if domain not in LARK_AUTH_HOST:
            raise ValueError(f"Unknown Lark domain: {domain!r}. Use 'feishu.cn' or 'larksuite.com'.")
        self.domain = domain
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_path = Path(token_path)
        self._cached: Optional[dict] = None

    def api_base(self) -> str:
        return LARK_API_HOST[self.domain]

    def get_user_access_token(self) -> str:
        """Return a valid user_access_token. Refresh or re-OAuth if necessary."""
        if self._cached is None:
            self._load_cache()

        if self._cached and self._cached.get("expires_at", 0) > time.time() + 60:
            return self._cached["access_token"]

        if self._cached and self._cached.get("refresh_token"):
            try:
                self._refresh()
                return self._cached["access_token"]
            except Exception:
                # Refresh failed (expired, revoked, etc.) — fall back to full OAuth.
                pass

        self._oauth_flow()
        return self._cached["access_token"]

    # --- internal ---

    def _load_cache(self) -> None:
        if self.token_path.exists():
            self._cached = json.loads(self.token_path.read_text())

    def _save_cache(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(self._cached, indent=2))

    def _store_tokens(self, data: dict) -> None:
        now = int(time.time())
        prior_refresh = (self._cached or {}).get("refresh_token")
        self._cached = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or prior_refresh,
            "expires_at": now + int(data.get("expires_in", 7200)) - 60,
            "refresh_expires_at": now + int(data.get("refresh_token_expires_in", 7 * 24 * 3600)),
        }
        self._save_cache()

    def _refresh(self) -> None:
        r = requests.post(
            f"{self.api_base()}/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "refresh_token": self._cached["refresh_token"],
            },
            timeout=15,
        )
        r.raise_for_status()
        self._store_tokens(r.json())

    def _oauth_flow(self) -> None:
        state = secrets.token_urlsafe(16)
        result: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result["code"] = q.get("code", [None])[0]
                result["state"] = q.get("state", [None])[0]
                result["error"] = q.get("error", [None])[0]
                result["error_description"] = q.get("error_description", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                if result.get("error"):
                    body = f"<h2>Lark auth failed: {result['error']}</h2><p>{result.get('error_description','')}</p>"
                else:
                    body = "<h2>Lark auth complete. You can close this tab.</h2>"
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args, **kwargs):
                pass

        server = http.server.HTTPServer(("localhost", LARK_OAUTH_REDIRECT_PORT), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        params = {
            "client_id": self.app_id,
            "redirect_uri": LARK_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "scope": " ".join(LARK_SCOPES),
        }
        authz_url = (
            f"{LARK_AUTH_HOST[self.domain]}/open-apis/authen/v1/authorize?"
            + urllib.parse.urlencode(params)
        )

        print("\n--- Lark OAuth required ---")
        print("Opening browser. If it doesn't open, copy this URL into a browser:\n")
        print(f"  {authz_url}\n")
        try:
            webbrowser.open(authz_url)
        except Exception:
            pass

        thread.join(timeout=300)
        server.server_close()

        if result.get("error"):
            raise RuntimeError(f"Lark OAuth error: {result['error']}: {result.get('error_description')}")
        if not result.get("code"):
            raise RuntimeError("Lark OAuth: no authorization code received (timed out?)")
        if result.get("state") != state:
            raise RuntimeError("Lark OAuth: state mismatch (possible CSRF). Aborting.")

        r = requests.post(
            f"{self.api_base()}/open-apis/authen/v2/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": result["code"],
                "redirect_uri": LARK_OAUTH_REDIRECT_URI,
            },
            timeout=15,
        )
        r.raise_for_status()
        self._store_tokens(r.json())
