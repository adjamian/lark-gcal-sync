# lark-gcal-sync

One-way calendar sync from **Lark / Feishu Calendar** to **Google Calendar**. 
Mirrors events from your primary Lark calendar into a dedicated Google calendar
on a configurable schedule via macOS `launchd`. The mirror is one-directional:
edits made directly in Google get overwritten on the next sync.

## Who is this for?
This tool is for individuals who use Lark internally and Google Calendar externally, and want their Google booking pages to reflect Lark unavailability, and are comfortable running a Python service on their MacBook.

## Background

Lark and Google Calendar both serve as calendar systems in environments where teams use Lark for internal collaboration but Google Calendar for external scheduling and booking links. Without a sync between them, a booking page offered to external parties has no visibility into internal Lark commitments or personal calendars, so external bookers can schedule over meetings the user already has. The user is forced to either manually block time in Google to mirror their Lark and personal commitments, or accept that booked meetings will conflict. Lark's CalDAV endpoint is read-only and not consumable by Google, Lark does not publish ICS feeds, and Lark's native Google integration runs in the opposite direction (Google into Lark, primary calendar only).

## What this tool does, in plain terms

It copies your Lark events into a separate Google calendar every X minutes so your Google booking page knows when you are actually busy. Your colleagues at Lark do not see anything change within Lark. External people booking time with you stop scheduling on top of your Lark meetings. Event details stay private if you mark them private, showing only as "Busy" with no description. Runs on your MacBook in the background. You set it up once and forget it.

## What this tool does, technically

A Python service that polls the Lark Open Platform calendar API every X minutes (configurable) via launchd, reads events from the user's primary Lark calendar across a rolling window of (now minus 7 days) to (now plus 180 days), and reconciles them against a dedicated mirror calendar in the user's Google Workspace account using the Google Calendar API. State is persisted in a local SQLite database mapping Lark event IDs to Google event IDs with last-modified timestamps, enabling correct propagation of creates, updates, and deletes. Mirrored events are title-prefixed for visual clarity, attendees are serialized as plain text in the description field rather than as Google attendees (preventing spurious invitations to Lark internal users), and Lark events with private visibility are mirrored as "Busy" with start and end times only. OAuth tokens for both platforms are stored locally and gitignored. No server infrastructure required; each user runs their own isolated deployment with their own credentials. One-way Lark to Google only.

## Behavior

- **Sync window**: `now − 7 days` to `now + 180 days`. Configurable in
  `config.yaml`.
- **Title prefix**: every mirrored event is prefixed `[Lark] ` so it's never
  confused with a native Google event.
- **Private Lark events** are mirrored as `[Lark] Busy` with only start/end —
  no description, no location, no attendees.
- **Lark attendees** are rendered as text in the Google event description
  (`Lark attendees: name1, name2, ...`). They are **not** added as Google
  attendees, and **no invitations are sent** — every Google API call uses
  `sendUpdates=none`.
- **Lark VC (video) links** are preserved in the description with a
  `[Lark VC]` label.
- **Recurring events** are expanded into individual instances on the mirror.
  Each occurrence becomes its own Google event.
- **Deletions** in Lark trigger deletes on the Google mirror via the local
  state database.

## Requirements

- **macOS** (the scheduler is `launchd`-based; for Linux see
  [Linux notes](#linux-notes) below).
- **Python 3.11 or newer** 
- A **Feishu / Lark** account with permission to create custom apps.
- A **Google** account where you can create a dedicated mirror calendar
  (do not use a calendar that holds events you care about — see below).

## Project files

Source-controlled:

| File | Purpose |
|---|---|
| `auth.py` | Lark user-token OAuth + Google OAuth, with refresh-token caching. |
| `lark_client.py` | Reads events from Lark/Feishu, including recurring-series expansion. |
| `google_client.py` | CRUD on the dedicated Google mirror calendar. |
| `sync.py` | Entry point. Reconciliation loop: fetch → diff → apply → log. |
| `config.example.yaml` | Config template. Copy to `config.yaml` and fill in. |
| `com.example.lark-gcal-sync.plist.example` | `launchd` job template. |
| `requirements.txt` | Python dependencies. |
| `LICENSE` | MIT. |

Generated at runtime, all gitignored:

| File | Purpose |
|---|---|
| `config.yaml` | Your real config (Lark credentials, Google calendar ID). |
| `tokens/credentials.json` | Google OAuth client downloaded from GCP. |
| `tokens/google_token.json` | Cached Google access + refresh tokens. |
| `tokens/lark_token.json` | Cached Lark access + refresh tokens. |
| `state.db` | SQLite mapping Lark event IDs → Google event IDs + content hashes. |
| `sync.log` | Rolling log of each sync run. |
| `launchd.out.log`, `launchd.err.log` | stdout/stderr captured by launchd. |
| `com.<your-username>.lark-gcal-sync.plist` | Your edited launchd job. |

## Setup

### 1. Clone and build a virtualenv

```sh
git clone <repo-url> lark-gcal-sync
cd lark-gcal-sync
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If `python3` resolves to something older than 3.11, install a newer Python
from https://www.python.org/downloads/macos/ and use it explicitly:

```sh
/usr/local/bin/python3.13 -m venv .venv
```

### 2. Create a Lark / Feishu custom app

First, identify your platform. Lark and Feishu are the same product under
different brands; which one you have depends on your organization's region.
You will use this `domain` value in `config.yaml` later (step 5).

| If your work app is | `domain` value | Developer console |
|---|---|---|
| **Lark** (international) | `larksuite.com` | https://open.larksuite.com/app |
| **Feishu** (China region) | `feishu.cn` | https://open.feishu.cn/app |

In your platform's developer console:

1. **Create Custom App.** Name and icon are for your reference only.
2. From *Credentials & Basic Info*, copy the **App ID** (`cli_xxxxxxxxxxxx`)
   and **App Secret**.
3. Open **Permissions & Scopes** and add **all four** of these:
   - `calendar:calendar:readonly`
   - `calendar:calendar.event:read`
   - `calendar:calendar.free_busy:read`
   - `offline_access` ← **required** for refresh tokens; without it, OAuth
     re-prompts every two hours.
4. Open **Security Settings** and add a redirect URI of
   `http://localhost:8765/`.
5. **Version Management & Release**: create a version, release to yourself
   only (specific members, not whole org). Scope changes do not take effect
   until a version is released.

   > **May require admin approval**: many Lark/Feishu organizations gate
   > custom-app releases behind a workspace administrator. If your
   > submission shows "Pending review" (or stays in an unreleased state),
   > contact your IT / Lark / Feishu admin and ask them to approve the app.
   > Wait time varies — minutes to days depending on the org. The rest of
   > setup (Google side, code) can proceed in parallel; the sync simply
   > won't work end-to-end until the app is released.

### 3. Create a Google Cloud project for OAuth

1. Open https://console.cloud.google.com/ and create a project (any name).
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** (in the newer UI, the
   sub-pages are: Branding, Audience, Data access, Clients):
   - User type: **External**, publishing status: **Testing**.
   - Add **yourself** as a Test user.
4. **Clients → Create Client → Desktop app** → download the JSON.
5. Move the downloaded JSON into the project as `tokens/credentials.json`.

### 4. Create the destination Google Calendar

In Google Calendar, create a brand-new calendar (e.g., "Lark Mirror"). Do
**not** use your primary calendar — the sync deletes mirror events that
disappear from Lark.

Open the new calendar's *Settings and sharing* → *Integrate calendar* and
copy the **Calendar ID** (looks like `xxxxxxxx@group.calendar.google.com`).

> **Heads-up on visibility in Lark or Feishu**: If you have your Google
> account connected inside Lark/Feishu (so it shows your Google calendars in
> its sidebar), the new "Lark Mirror" calendar will appear there too. After
> the first sync, **uncheck "Lark Mirror" in your Lark/Feishu sidebar** —
> otherwise every event will show twice in your Lark/Feishu UI (once
> natively, once round-tripped through Google). In Google Calendar, leave
> it checked: that's where you want to see the mirrored events.

### 5. Configure

```sh
cp config.example.yaml config.yaml
```

Edit `config.yaml`: paste your Lark App ID/Secret, your Google Calendar ID,
and optionally adjust the `window_*_days` values.

### 6. First manual sync

```sh
.venv/bin/python sync.py
```

You will see two browser prompts in sequence:

- **Google OAuth.** On the unverified-app warning, click *Advanced → Go to
  ... (unsafe)* (it's your own app), then approve.
- **Lark OAuth.** Approve the listed scopes.

You may also see a one-time **macOS "Python wants to find devices on your
local network"** prompt. Click **Allow**: Python needs this to bind a
temporary local server during OAuth.

Verify the mirror in Google Calendar. The first run will create one Google
event per Lark event in the window — possibly tens to hundreds depending on
how many recurring series you have.

### 7. Install the launchd job

```sh
cp com.example.lark-gcal-sync.plist.example com.<your-username>.lark-gcal-sync.plist
```

Edit the new file and replace `__YOUR_USERNAME__` and `__PROJECT_PATH__`
with your actual values, then install:

```sh
cp com.<your-username>.lark-gcal-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.<your-username>.lark-gcal-sync.plist
launchctl list | grep lark-gcal-sync
tail -10 sync.log
```

If the job is registered and `sync.log` shows a fresh `=== sync end ===`,
the scheduler is working.

## Operations

### Health check

```sh
launchctl list | grep lark-gcal-sync   # job registered + last exit code
tail -20 sync.log                       # recent activity
wc -c launchd.err.log                   # should stay 0 in normal operation
```

### Force a sync immediately

```sh
.venv/bin/python sync.py
```

### Stop the scheduler

```sh
launchctl unload ~/Library/LaunchAgents/com.<your-username>.lark-gcal-sync.plist
```

### Reset state

After stopping the scheduler:

```sh
rm state.db tokens/google_token.json tokens/lark_token.json
```

Delete events from the mirror calendar manually, then re-run `sync.py` to
rebuild from scratch.

## Configuration knobs

In `config.yaml` under `sync`:

- `window_past_days` — how far into the past to fetch (default 7).
- `window_future_days` — how far into the future to fetch (default 180).
  Wider windows mean more API calls and slower syncs but more lookahead for
  far-future invites.
- `title_prefix` — prepended to mirrored event titles.
- `private_title` — title used for private Lark events.

In your `.plist`, `StartInterval` controls how often the sync fires
(in seconds; default 120 = 2 minutes; reasonable range 60–600).

## Token lifetimes

- **Google** access tokens are short-lived but refreshed automatically. No
  re-auth needed in normal operation.
- **Lark** access tokens last 2 hours. Refresh tokens last 7 days and rotate:
  every refresh issues a new 7-day refresh token.
- Bottom line: as long as the sync runs at least once every 7 days, you
  never re-authenticate. If the laptop is offline longer, the next sync
  will trigger Lark OAuth in the browser.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser prompts every ~2 hours | `offline_access` scope missing on Lark app | Add the scope, release a new version, re-auth once |
| Lark OAuth shows "error code 20043" | Scope mismatch between code and Lark app | Reconcile `LARK_SCOPES` in `auth.py` with the Lark app's enabled scopes |
| `Could not find a primary Lark calendar` | Token missing calendar scopes, or version not released | Verify all four scopes are granted in the released version |
| `404 Client Error` on attendees endpoint | Looking up attendees on a recurring instance ID | Expected — sync handles this; attendees come from the parent event |
| `launchd.err.log` growing | Python tracebacks at startup | Read the file; usually a config or token issue |
| First sync hangs after a fresh Python install | macOS Local Network permission prompt waiting | Click Allow on the popup |
| Mirror events disappear unexpectedly | Lark event moved outside the sync window | Widen `window_future_days` |

## Privacy

- Token files (`tokens/*.json`) and `config.yaml` contain credentials that
  grant calendar access. Treat as secrets. Both are gitignored.
- `state.db` and `sync.log` may contain Lark event IDs, Google event IDs,
  and attendee names. Also gitignored.
- The sync uses `sendUpdates=none` on every Google write, so no invitations
  are ever sent through this tool.

## Linux notes

To run on Linux, replace launchd with `cron`. After completing setup steps
1–6, add this line to `crontab -e`:

```
*/2 * * * * cd /absolute/path/to/lark-gcal-sync && .venv/bin/python sync.py >> launchd.out.log 2>> launchd.err.log
```

Everything else (Python, OAuth flow, scopes) is identical.

## License

MIT — see [LICENSE](LICENSE).
