"""Entry point: one-way sync Lark/Feishu → Google Calendar.

Each run does one reconciliation pass:
  1. Fetch Lark events in (now − past_days, now + future_days).
  2. Compare against state.db (Lark ID → Google ID mapping).
  3. Create / update / delete on the Google mirror calendar.
  4. Log counts.

Designed to be triggered on a schedule (launchd on macOS, cron on Linux);
each invocation runs one pass and exits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

# Silence noisy startup warnings from urllib3 (LibreSSL on macOS) and the Google
# libs warning about Python 3.9 being past EOL. Both are informational and
# repeat on every run — under launchd they'd spam launchd.err.log forever.
# Match by message so the filter is active before urllib3 itself is imported.
warnings.filterwarnings("ignore", message=r".*urllib3 v2 only supports OpenSSL.*")
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")

import yaml

import auth
from google_client import GoogleClient
from lark_client import LarkClient


PROJECT_DIR = Path(__file__).resolve().parent


def load_config() -> dict:
    with open(PROJECT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def setup_logging(log_path: str) -> logging.Logger:
    log = logging.getLogger("sync")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    # Only mirror logs to stdout when running interactively. Under launchd,
    # stdout gets captured to launchd.out.log which would duplicate sync.log.
    if sys.stdout.isatty():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    return log


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # We track content_hash because Lark's list-events response has no
    # update_time field. The hash covers any field that affects what we mirror.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mappings (
            lark_event_id   TEXT PRIMARY KEY,
            google_event_id TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            last_sync_at    INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def lark_event_hash(ev: dict) -> str:
    """Stable hash of a Lark event's sync-relevant content.

    Excludes fields that are transient (app_link embeds instance timestamps),
    user-local (self_rsvp_status doesn't affect what we mirror), or internal
    annotations we added.
    """
    excluded = {"app_link", "self_rsvp_status", "_parent_event_id"}
    relevant = {k: v for k, v in ev.items() if k not in excluded}
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, default=str).encode()
    ).hexdigest()


def normalize_lark_event(ev: dict, attendees: List[str], cfg: dict) -> dict:
    """Convert a Lark event dict to a Google Calendar API body."""
    title_prefix = cfg["sync"]["title_prefix"]
    private_title = cfg["sync"]["private_title"]
    is_private = ev.get("visibility") == "private"

    start = ev.get("start_time") or {}
    end = ev.get("end_time") or {}
    tz = start.get("timezone") or end.get("timezone") or "UTC"

    # All-day events use {"date": "YYYY-MM-DD"}; timed events use {"timestamp": "<unix-seconds>"}.
    if start.get("date"):
        g_start = {"date": start["date"]}
        g_end = {"date": end.get("date", start["date"])}
    else:
        g_start = {
            "dateTime": datetime.fromtimestamp(int(start["timestamp"]), tz=timezone.utc).isoformat(),
            "timeZone": tz,
        }
        g_end = {
            "dateTime": datetime.fromtimestamp(int(end["timestamp"]), tz=timezone.utc).isoformat(),
            "timeZone": tz,
        }

    if is_private:
        return {
            "summary": private_title,
            "start": g_start,
            "end": g_end,
            "transparency": "opaque",
        }

    title = ev.get("summary") or "(no title)"
    body: dict = {
        "summary": title_prefix + title,
        "start": g_start,
        "end": g_end,
        "transparency": "opaque",
    }

    desc_parts: List[str] = []
    if ev.get("description"):
        desc_parts.append(ev["description"])
    vc = ev.get("vchat") or {}
    if vc.get("meeting_url"):
        desc_parts.append(f"[Lark VC] {vc['meeting_url']}")
    if attendees:
        desc_parts.append(f"Lark attendees: {', '.join(attendees)}")
    if desc_parts:
        body["description"] = "\n\n".join(desc_parts)

    loc = ev.get("location") or {}
    if loc.get("name"):
        body["location"] = loc["name"]

    if ev.get("reminders"):
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": int(r.get("minutes", 10))}
                for r in ev["reminders"][:5]
            ],
        }

    # NOTE: We intentionally do not propagate Lark's `recurrence` field. Lark's
    # list-events API returns individual instances of recurring series, each of
    # which may also carry the parent RRULE. Copying that to Google causes Google
    # to re-expand the series on every instance — an explosion. Treating each
    # Lark event as a one-off is safe and matches what the user actually sees.

    return body


def sync_once(cfg: dict, log: logging.Logger, dry_run: bool = False) -> Dict[str, int]:
    lark_auth = auth.LarkAuth(
        domain=cfg["lark"]["domain"],
        app_id=cfg["lark"]["app_id"],
        app_secret=cfg["lark"]["app_secret"],
        token_path=str(PROJECT_DIR / "tokens" / "lark_token.json"),
    )
    google_service = auth.get_google_service(
        credentials_path=str(PROJECT_DIR / cfg["google"]["credentials_path"]),
        token_path=str(PROJECT_DIR / cfg["google"]["token_path"]),
    )

    lark = LarkClient(lark_auth, log)
    google = GoogleClient(google_service, cfg["google"]["calendar_id"], log)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=cfg["sync"]["window_past_days"])
    window_end = now + timedelta(days=cfg["sync"]["window_future_days"])
    start_ts = int(window_start.timestamp())
    end_ts = int(window_end.timestamp())
    log.info(f"Window: {window_start.isoformat()} → {window_end.isoformat()}")

    primary_cal_id = lark.primary_calendar_id()
    log.info(f"Lark primary calendar: {primary_cal_id}")

    lark_events = lark.list_events(primary_cal_id, start_ts, end_ts)
    active = [e for e in lark_events if e.get("status") != "cancelled"]
    log.info(f"Lark events in window: {len(lark_events)} ({len(active)} active)")

    # Lark's list-events doesn't expand recurring series into their occurrences;
    # it returns the base event (with a `recurrence` rule) plus any override
    # instances. Expand each recurring series into its real occurrences via the
    # /instances endpoint. Overrides are skipped here because the expansion
    # returns them too (with their modified data).
    expanded: List[Dict] = []
    expanded_series_count = 0
    for ev in active:
        if ev.get("recurring_event_id"):
            continue  # override; expansion of the base will include this instance
        if ev.get("recurrence"):
            parent_id = ev["event_id"]
            instances = lark.list_event_instances(
                primary_cal_id, parent_id, start_ts, end_ts
            )
            instances = [i for i in instances if i.get("status") != "cancelled"]
            if instances:
                # Lark's /instances response doesn't include recurring_event_id,
                # so annotate each instance so attendee lookups route to the parent.
                for inst in instances:
                    inst["_parent_event_id"] = parent_id
                expanded.extend(instances)
                expanded_series_count += 1
            else:
                expanded.append(ev)
        else:
            expanded.append(ev)
    active = expanded
    log.info(
        f"After expanding recurring series: {len(active)} events "
        f"({expanded_series_count} series expanded)"
    )

    conn = init_db(str(PROJECT_DIR / cfg["paths"]["state_db"]))
    counts = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
    seen_lark_ids = set()
    sync_ts = int(time.time())

    # Lark attendees live on the parent (series) event, not on each instance.
    # Cache per-parent so we fetch once per series, not once per occurrence.
    attendee_cache: Dict[str, List[str]] = {}

    def attendees_for(event: dict) -> List[str]:
        if event.get("visibility") == "private":
            return []
        source_id = (
            event.get("_parent_event_id")
            or event.get("recurring_event_id")
            or event["event_id"]
        )
        if source_id not in attendee_cache:
            attendee_cache[source_id] = lark.list_attendees(primary_cal_id, source_id)
        return attendee_cache[source_id]

    for ev in active:
        lark_id = ev.get("event_id")
        if not lark_id:
            continue
        seen_lark_ids.add(lark_id)
        current_hash = lark_event_hash(ev)

        row = conn.execute(
            "SELECT google_event_id, content_hash FROM mappings WHERE lark_event_id = ?",
            (lark_id,),
        ).fetchone()

        try:
            needs_create = row is None
            needs_update = row is not None and row[1] != current_hash

            if not needs_create and not needs_update:
                counts["skipped"] += 1
                continue

            # Only fetch attendees when we're actually going to write. Keeps no-op
            # syncs fast (otherwise every event triggers an extra round-trip).
            body = normalize_lark_event(ev, attendees_for(ev), cfg)

            if needs_create:
                if dry_run:
                    log.info(f"[dry-run] Would CREATE: {lark_id}  {body.get('summary')}")
                else:
                    g_ev = google.insert(body)
                    conn.execute(
                        "INSERT INTO mappings (lark_event_id, google_event_id, content_hash, last_sync_at) VALUES (?, ?, ?, ?)",
                        (lark_id, g_ev["id"], current_hash, sync_ts),
                    )
                    conn.commit()
                    log.info(f"Created: {lark_id} → {g_ev['id']}  {body.get('summary')}")
                counts["created"] += 1
            else:
                if dry_run:
                    log.info(f"[dry-run] Would UPDATE: {lark_id} → {row[0]}  {body.get('summary')}")
                else:
                    google.update(row[0], body)
                    conn.execute(
                        "UPDATE mappings SET content_hash = ?, last_sync_at = ? WHERE lark_event_id = ?",
                        (current_hash, sync_ts, lark_id),
                    )
                    conn.commit()
                    log.info(f"Updated: {lark_id} → {row[0]}  {body.get('summary')}")
                counts["updated"] += 1
        except Exception:
            counts["errors"] += 1
            log.exception(f"Error syncing Lark event {lark_id}")

    # Orphan deletion: anything in state.db with no matching Lark event in the current window.
    orphans = conn.execute(
        "SELECT lark_event_id, google_event_id FROM mappings"
    ).fetchall()
    for lark_id, google_id in orphans:
        if lark_id in seen_lark_ids:
            continue
        try:
            if dry_run:
                log.info(f"[dry-run] Would DELETE: {lark_id} → {google_id}")
            else:
                google.delete(google_id)
                conn.execute("DELETE FROM mappings WHERE lark_event_id = ?", (lark_id,))
                conn.commit()
                log.info(f"Deleted: {lark_id} → {google_id}")
            counts["deleted"] += 1
        except Exception:
            counts["errors"] += 1
            log.exception(f"Error deleting Google event {google_id}")

    conn.close()
    log.info(
        "Sync complete: created=%d updated=%d deleted=%d skipped=%d errors=%d",
        counts["created"], counts["updated"], counts["deleted"],
        counts["skipped"], counts["errors"],
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Lark → Google Calendar one-way sync.")
    parser.add_argument("--dry-run", action="store_true", help="Log what would happen; make no changes.")
    args = parser.parse_args()

    cfg = load_config()
    log = setup_logging(str(PROJECT_DIR / cfg["paths"]["log_file"]))

    log.info("=== sync start ===")
    try:
        sync_once(cfg, log, dry_run=args.dry_run)
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)
    log.info("=== sync end ===")


if __name__ == "__main__":
    main()
