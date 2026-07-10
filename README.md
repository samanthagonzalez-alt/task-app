# todo-bar

A macOS menu bar app that pulls action items from Gmail, Google Docs meeting notes, Slack, and Google Tasks into a single unified to-do list.

## Features

- Menu bar panel (via PyObjC / NSStatusItem) — click the icon to open your task list
- Syncs action items from:
  - Gmail (via Google API)
  - Google Docs meeting notes (detects "Action items" sections, matches by name alias)
  - Slack starred/saved items (currently blocked — see note below)
  - Google Tasks (requires Tasks API enabled on the linked GCP project)
- Settings panel for name aliases, sync interval, and reconnecting Google/Slack
- Demo mode with fake data for presentations, isolated from your real tasks

## Requirements

- macOS
- Python 3.9+
- PyObjC (`pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`)
- `google-api-python-client`, `google-auth`, `google-auth-oauthlib`

Install dependencies:

```bash
pip3 install pyobjc-framework-Cocoa pyobjc-framework-Quartz google-api-python-client google-auth google-auth-oauthlib
```

## Setup

### Google (Gmail / Docs / Tasks)

1. Create a GCP project and OAuth client credentials (Desktop app type).
2. Save the client secret as `~/.gmail-mcp/gcp-oauth.keys.json`.
3. On first run, the app will open a browser to complete OAuth. The resulting token is saved to `~/.gmail-mcp/credentials.json`.
4. To pull from Google Tasks, the Tasks API must be enabled on the GCP project (separate from OAuth scopes, requires console access).

### Slack (optional)

1. Create a Slack app with a user token (`xoxp-`) and the relevant scopes.
2. Add the token via the app's Settings panel — it's saved to `~/.todo-bar/slack_token.json`.

None of these credential files are tracked in this repo — they live under your home directory.

## Running

```bash
python3 app.py
```

### Run at login (recommended)

A LaunchAgent plist (`com.todobar.app.plist`) is included. Install it with:

```bash
cp com.todobar.app.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.todobar.app.plist
```

This launches the app at login and automatically restarts it if the process dies.

### Demo mode

Run with fake sample data, isolated from your real tasks:

```bash
python3 app.py --demo
```

Demo data lives in `~/.todo-bar/demo_tasks.json` and is generated/refreshed via `demo_data.py`:

```bash
python3 demo_data.py          # preserves any edits made in a prior demo run
python3 demo_data.py --reset  # full reset to the base demo task set
```

## Data storage

- Real tasks: `~/.todo-bar/tasks.json`
- Demo tasks: `~/.todo-bar/demo_tasks.json`
- App logs: `~/todo-bar/launch.log`

## Known limitations

- Slack's "Later" (saved items) API is locked down by Slack for third-party apps — `stars.list` and `saved.list` are not accessible. An emoji-reaction-based alternative is being explored.
- Google Tasks sync requires the Tasks API to be enabled on the GCP project, which may require admin/console access you don't have.
