"""CRUD against the dedicated mirror Google Calendar.

All writes use sendUpdates='none' so no invitations are ever sent.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from googleapiclient.errors import HttpError


class GoogleClient:
    def __init__(self, service, calendar_id: str, logger: logging.Logger):
        self.service = service
        self.calendar_id = calendar_id
        self.log = logger

    def list_events(self, time_min_iso: str, time_max_iso: str) -> List[Dict]:
        events: List[Dict] = []
        page_token = None
        while True:
            resp = (
                self.service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min_iso,
                    timeMax=time_max_iso,
                    singleEvents=False,
                    showDeleted=False,
                    pageToken=page_token,
                    maxResults=2500,
                )
                .execute()
            )
            events.extend(resp.get("items") or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return events

    def insert(self, body: dict) -> dict:
        return (
            self.service.events()
            .insert(calendarId=self.calendar_id, body=body, sendUpdates="none")
            .execute()
        )

    def update(self, event_id: str, body: dict) -> dict:
        return (
            self.service.events()
            .update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates="none",
            )
            .execute()
        )

    def delete(self, event_id: str) -> None:
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates="none",
            ).execute()
        except HttpError as e:
            # 404 = already gone; 410 = gone. Either way: nothing to do.
            if e.resp.status in (404, 410):
                return
            raise
