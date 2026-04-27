"""Fetch events from the user's primary Lark calendar (also branded Feishu in
China). The API host is selected by the configured domain in auth.LarkAuth.

Uses user-access-token auth. All calls go through the user's permissions, so
anything they can see in the Lark UI, we can read.
"""
from __future__ import annotations

import logging
from typing import Dict, List

import requests


class LarkClient:
    def __init__(self, auth, logger: logging.Logger):
        self.auth = auth
        self.log = logger
        self._session = requests.Session()

    # --- public API used by sync.py ---

    def primary_calendar_id(self) -> str:
        """Return the user's primary calendar ID.

        First tries POST /calendars/primary (the documented endpoint). If that
        returns nothing (some tenants behave oddly), falls back to listing all
        calendars and picking the one with type='primary'.
        """
        r = self._post("/open-apis/calendar/v4/calendars/primary")
        calendars = r["data"].get("calendars") or []
        if calendars:
            return calendars[0]["calendar"]["calendar_id"]

        self.log.info("primary endpoint returned empty; falling back to /calendars list")
        r = self._get("/open-apis/calendar/v4/calendars", params={"page_size": 50})
        for item in r["data"].get("calendar_list") or []:
            if item.get("type") == "primary":
                return item["calendar_id"]
        raise RuntimeError(
            "Could not find a primary Lark calendar. "
            "The OAuth token may be missing calendar scopes, or the user has no primary calendar."
        )

    def list_events(self, calendar_id: str, start_ts: int, end_ts: int) -> List[Dict]:
        """List events in [start_ts, end_ts] (unix seconds). Paginates."""
        events: List[Dict] = []
        page_token = None
        while True:
            params = {
                "start_time": str(start_ts),
                "end_time": str(end_ts),
                "page_size": 500,
            }
            if page_token:
                params["page_token"] = page_token
            r = self._get(
                f"/open-apis/calendar/v4/calendars/{calendar_id}/events",
                params=params,
            )
            data = r["data"]
            events.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return events

    def list_event_instances(
        self, calendar_id: str, event_id: str, start_ts: int, end_ts: int
    ) -> List[Dict]:
        """Expand a recurring event into its individual instances within [start_ts, end_ts]."""
        instances: List[Dict] = []
        page_token = None
        while True:
            params = {
                "start_time": str(start_ts),
                "end_time": str(end_ts),
                "page_size": 500,
            }
            if page_token:
                params["page_token"] = page_token
            try:
                r = self._get(
                    f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}/instances",
                    params=params,
                )
            except Exception as e:
                self.log.warning(f"Instances fetch failed for {event_id}: {e}")
                return instances
            data = r["data"]
            instances.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return instances

    def list_attendees(self, calendar_id: str, event_id: str) -> List[str]:
        """Return display names for an event's attendees."""
        names: List[str] = []
        page_token = None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            try:
                r = self._get(
                    f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
                    params=params,
                )
            except Exception as e:
                self.log.warning(f"Attendees fetch failed for {event_id}: {e}")
                return names
            data = r["data"]
            for a in data.get("items") or []:
                names.append(_attendee_display_name(a))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return names

    # --- internal ---

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, params: dict = None, json_body: dict = None) -> dict:
        return self._request("POST", path, params=params, json_body=json_body)

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
        url = self.auth.api_base() + path
        headers = {"Authorization": f"Bearer {self.auth.get_user_access_token()}"}
        resp = self._session.request(
            method, url, headers=headers, params=params, json=json_body, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code", 0) != 0:
            raise RuntimeError(
                f"Lark API {method} {path} error: code={body.get('code')} msg={body.get('msg')}"
            )
        return body


def _attendee_display_name(a: dict) -> str:
    """Best-effort human-readable attendee name."""
    return (
        a.get("display_name")
        or (a.get("user") or {}).get("display_name")
        or (a.get("user") or {}).get("name")
        or (a.get("chat") or {}).get("name")
        or (a.get("resource") or {}).get("display_name")
        or (a.get("third_party") or {}).get("display_name")
        or a.get("third_party_email")
        or "(unknown)"
    )
