#!/usr/bin/env python3
"""
My Tasks — macOS menu bar to-do app
Click the ☑ icon to open/close a persistent side panel.
Syncs action items assigned to "Sam" / "Samantha Gonzalez" from Gemini meeting notes in Gmail.
"""

import json
import os
import re
import signal
import subprocess
import threading
import uuid
import base64
import hashlib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import AppKit
import objc
from Foundation import NSMakeRect

try:
    import WebKit
    HAS_WEBKIT = True
except ImportError:
    HAS_WEBKIT = False

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE         = Path.home() / ".todo-bar" / "tasks.json"
PID_FILE          = Path.home() / ".todo-bar" / "app.pid"
TOKEN_PATH        = Path.home() / ".gmail-mcp" / "credentials.json"
KEYS_PATH         = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
SYNC_INTERVAL_SEC = 30 * 60  # 30 minutes

PANEL_WIDTH = 360

# Scopes needed when Drive / Docs access is added
DOCS_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ── Single-instance enforcement ───────────────────────────────────────────────
def acquire_instance():
    """Kill any running instance via PID file, then write our PID."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip())
            if old != os.getpid():
                os.kill(old, signal.SIGKILL)   # SIGKILL — works even if SIGTERM is ignored
                import time; time.sleep(1.0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    PID_FILE.write_text(str(os.getpid()))

def release_instance():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"tasks": [], "lastSync": None, "deletedIds": []}

def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))

def make_task(text, group="today", from_sync=False, source=None, source_id=None,
              category="", project="", status="todo", today=False):
    return {
        "id":       source_id or str(uuid.uuid4()),
        "text":     text.strip(),
        "done":     False,
        "group":    group,
        "fromSync": from_sync,
        "source":   source,
        "sourceId": source_id,
        "addedAt":  datetime.now(timezone.utc).isoformat(),
        "category": category,
        "project":  project,
        "status":   status,
        "today":    today,
        "notes":    [],
    }

# ── Google auth helpers ───────────────────────────────────────────────────────
def _load_creds():
    """Build Google credentials from stored token; refresh if expired."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds_raw = json.loads(TOKEN_PATH.read_text())
    keys_raw  = json.loads(KEYS_PATH.read_text()) if KEYS_PATH.exists() else {}
    installed = keys_raw.get("installed") or keys_raw.get("web") or {}

    creds = Credentials(
        token=creds_raw.get("access_token"),
        refresh_token=creds_raw.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds_raw.get("client_id") or installed.get("client_id"),
        client_secret=creds_raw.get("client_secret") or installed.get("client_secret"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        updated = {**creds_raw, "access_token": creds.token,
                   "expiry_date": int(creds.expiry.timestamp() * 1000) if creds.expiry else None}
        TOKEN_PATH.write_text(json.dumps(updated, indent=2))
    return creds

def has_docs_scope():
    """Return True if stored credentials include Drive / Docs scope."""
    try:
        raw   = json.loads(TOKEN_PATH.read_text())
        scope = raw.get("scope", "") or ""
        return "documents" in scope or "drive" in scope
    except Exception:
        return False

def run_drive_oauth():
    """Open a browser OAuth consent flow to add Drive/Docs scopes. Blocks until done."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    keys_raw = json.loads(KEYS_PATH.read_text())
    flow     = InstalledAppFlow.from_client_config(keys_raw, scopes=DOCS_SCOPES)
    creds    = flow.run_local_server(port=0, open_browser=True)

    # Merge into the existing credentials file
    existing = json.loads(TOKEN_PATH.read_text()) if TOKEN_PATH.exists() else {}
    updated  = {
        **existing,
        "access_token":  creds.token,
        "refresh_token": creds.refresh_token,
        "scope":         " ".join(creds.scopes) if creds.scopes else "",
        "expiry_date":   int(creds.expiry.timestamp() * 1000) if creds.expiry else None,
    }
    TOKEN_PATH.write_text(json.dumps(updated, indent=2))

# ── Gmail sync ────────────────────────────────────────────────────────────────
SAM_PATTERNS = [
    r"^Samantha Gonzalez will\s+",
    r"^Sam will\s+",
    r"^\[Samantha[^\]]*\]\s*",
    r"^\[Sam\]\s*",
    r"^Samantha Gonzalez\s*",
    r"^Sam\s+",
]

def clean_task_text(line):
    text = line.strip().lstrip("-•* ")
    for pat in SAM_PATTERNS:
        new = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
        if new != text:
            text = new
            break
    return text.rstrip(".")

def clean_subject(subject):
    m = re.match(r'Notes:\s*"([^"]+)"\s*(.*)', subject)
    if m:
        title    = m.group(1).strip()
        date_part = re.sub(r",\s*\d{4}", "", m.group(2)).strip()
        return f"{title} — {date_part}" if date_part else title
    return subject

def decode_body(payload):
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        raw = body["data"].replace("-", "+").replace("_", "/")
        return base64.b64decode(raw + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = decode_body(part)
        if text:
            return text
    return ""

def extract_sam_tasks(body, message_id, subject):
    tasks = []
    m = re.search(r"Suggested next steps(.*?)(?=\n\n\n|Meeting records|$)", body,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return tasks
    section = m.group(1)

    # Email bodies soft-wrap long lines. Rejoin them by splitting on blank lines
    # (each paragraph = one action item) then collapsing internal newlines.
    raw_items = re.split(r'\n\s*\n', section)
    items = []
    for block in raw_items:
        joined = ' '.join(l.strip() for l in block.splitlines() if l.strip())
        if joined:
            items.append(joined)

    for item in items:
        if re.match(r"^(Suggested next steps|Meeting records)", item, re.I):
            continue
        # Only include tasks where Sam is the PRIMARY assignee (starts the item),
        # not merely mentioned as a collaborator mid-sentence.
        if not re.search(
            r"^-?\s*(\[Samantha|\[Sam\]|Sam\b|Samantha Gonzalez|\[[^\]]*Samantha)",
            item.strip(), re.I
        ):
            continue
        text = clean_task_text(item)
        if len(text) < 5:
            continue
        h         = hashlib.md5(text.encode()).hexdigest()[:8]
        source_id = f"{message_id}::{h}"
        tasks.append(make_task(
            text=text, group="carryover", from_sync=True,
            source=clean_subject(subject), source_id=source_id,
        ))
    return tasks

def extract_docs_tasks(body, message_id, subject):
    """Extract tasks from Google Docs/Sheets assignment and @mention notification emails."""
    tasks = []

    is_assignment = "assigned you a task" in body
    is_mention    = "mentioned you in a comment" in body
    if not is_assignment and not is_mention:
        return tasks

    # Derive doc name from subject
    # "X mentioned you in a comment in [Doc Name] - Google Docs/Sheets"
    m = re.search(r'mentioned you in a comment in (.+?)(?:\s*[-–]\s*Google\b|\s*$)', subject, re.I)
    if m:
        doc_name = m.group(1).strip()
    elif " - " in subject:
        doc_name = subject.split(" - ")[0].strip()
    else:
        doc_name = subject

    lines = body.splitlines()

    # Refine doc name: line just before the first URL in parentheses
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("(https://") or stripped.startswith("(http://"):
            if i > 0:
                candidate = lines[i - 1].strip()
                if candidate and not candidate.startswith("("):
                    doc_name = candidate
            break

    def add_task(text):
        text = text.strip().rstrip(".")
        if len(text) < 5:
            return
        h         = hashlib.md5(text.encode()).hexdigest()[:8]
        source_id = f"{message_id}::{h}"
        tasks.append(make_task(
            text=text, group="carryover", from_sync=True,
            source=doc_name, source_id=source_id,
        ))

    if is_assignment:
        # Pipe lines = task text
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            text = stripped.lstrip("| ").strip()
            if re.match(r'^[A-Z][a-z]+ \d{1,2},?\s*\d{4}$', text):
                continue
            text = re.sub(r'^Samantha\s*Gonzalez\s*', '', text, flags=re.IGNORECASE).strip()
            text = re.sub(r'^Sam\b\s*', '', text, flags=re.IGNORECASE).strip()
            add_task(text)

    else:
        # Mention email:
        #   |  lines = CONTEXT quoted from the doc → skip
        #   Non-pipe content lines = the actual comment text / action items
        FOOTER_STARTS = (
            "Google LLC",
            "You have received",
            "You are receiving",
            "You cannot reply",
            "Change what Google",
            "Open (",
            "Open\xa0(",  # non-breaking space variant
        )
        EMAIL_RE      = re.compile(r'@[\w.+\-]+@[\w.\-]+\.\w+\s*')
        URL_RE        = re.compile(r'^\(?https?://')   # catches (https://... too
        NAME_RE       = re.compile(r'^[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}$')
        TIMESTAMP     = re.compile(r'\b\d{1,2}:\d{2}\s*[AP]M\b', re.I)
        RESOLVED      = re.compile(r'^_?(Marked as resolved|Re-opened)_?$', re.I)
        COUNT_LINE    = re.compile(r'^\d+ (comment|task|resolved)', re.I)
        TRAILING_CONJ = re.compile(
            r'\b(and|or|the|with|of|for|to|in|at|a|an|by|from|as|but|nor|yet|so)\s*$', re.I
        )

        in_comment = False
        raw = []  # list of str or None (None = blank line)

        for line in lines:
            s = line.strip()

            if s == '.':
                in_comment = True
                raw.append(None)
                continue
            if not in_comment:
                continue

            if any(s.startswith(p) for p in FOOTER_STARTS):
                break

            if s.startswith("|"):
                continue

            if not s:
                raw.append(None)
                continue

            if URL_RE.match(s) or TIMESTAMP.search(s) or RESOLVED.match(s) or COUNT_LINE.match(s):
                continue

            if NAME_RE.match(s):
                continue

            raw.append(s)

        chunks = []
        current = None
        for item in raw:
            if item is None:
                if current is not None:
                    chunks.append(current)
                    current = None
            elif current is None:
                current = item
            elif item and (
                item[0].islower() or
                (TRAILING_CONJ.search(current) and len(item) < 50
                 and not re.match(r'^[-•*]', item))
            ):
                current = current.rstrip() + " " + item
            else:
                chunks.append(current)
                current = item
        if current is not None:
            chunks.append(current)

        for chunk in chunks:
            text = EMAIL_RE.sub('', chunk).strip()
            add_task(text)

    return tasks


def run_gmail_sync(last_sync=None):
    try:
        from googleapiclient.discovery import build

        creds = _load_creds()
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

        # Only fetch emails newer than last sync (default 30d on first run)
        newer = "newer_than:30d"
        if last_sync:
            try:
                last_dt  = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                days_ago = max(1, (datetime.now(timezone.utc) - last_dt).days + 1)
                newer    = f"newer_than:{days_ago}d"
            except Exception:
                pass

        all_tasks = []

        # 1. Gemini auto-generated meeting notes
        result = gmail.users().messages().list(
            userId="me", q=f"from:gemini-notes@google.com {newer}", maxResults=50
        ).execute()
        for msg in result.get("messages", []):
            full    = gmail.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
            subject = headers.get("Subject", "")
            body    = decode_body(full["payload"])
            all_tasks.extend(extract_sam_tasks(body, msg["id"], subject))

        # 2. Google Docs / Sheets task assignment notifications
        result2 = gmail.users().messages().list(
            userId="me", q=f"from:comments-noreply@docs.google.com {newer}", maxResults=50
        ).execute()
        for msg in result2.get("messages", []):
            full    = gmail.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
            subject = headers.get("Subject", "")
            body    = decode_body(full["payload"])
            all_tasks.extend(extract_docs_tasks(body, msg["id"], subject))

        return all_tasks, None
    except Exception as e:
        return [], str(e)

# ── Google Docs sync (ICS-based, no Drive / Docs API needed) ─────────────────

def _unfold_ical(text):
    """Unfold RFC 5545 line continuations (CRLF or LF followed by whitespace)."""
    return re.sub(r'\r?\n[ \t]', '', text)

def _iter_all_parts(payload):
    """Recursively yield every MIME part in a Gmail message payload."""
    yield payload
    for part in payload.get("parts", []):
        yield from _iter_all_parts(part)

def _find_notes_docs_from_gmail(gmail, known_docs=None, since_days=None):
    """
    Search Gmail for calendar invite emails with ICS attachments.
    Parse each ICS for ATTACH properties pointing to Google Docs.
    Return {doc_id: doc_name}.

    If known_docs is provided (cache from previous run), only scan
    emails newer than since_days to find NEW docs and merge with cache.
    """
    docs      = dict(known_docs or {})
    seen_uids = set()
    seen_msgs = set()
    DOC_ID_RE = re.compile(r'docs\.google\.com/document/d/([A-Za-z0-9_\-]+)')
    UID_RE    = re.compile(r'^UID:(.+)$', re.M)
    ICS_MIMES = {"text/calendar", "application/ics", "application/octet-stream"}

    if known_docs is not None and since_days is not None:
        # Incremental: only scan recent emails for new docs
        days = max(1, int(since_days) + 1)
        QUERIES = [
            (f'has:attachment filename:ics newer_than:{days}d', 50),
        ]
    else:
        # Full scan on first run
        QUERIES = [
            ('has:attachment filename:ics subject:"1on1" newer_than:730d', 50),
            ('has:attachment filename:ics subject:"1:1"  newer_than:730d', 50),
            ('has:attachment filename:ics newer_than:730d',                200),
        ]

    all_msg_ids = []
    for q, max_r in QUERIES:
        try:
            result = gmail.users().messages().list(
                userId="me", q=q, maxResults=max_r,
            ).execute()
            for m in result.get("messages", []):
                if m["id"] not in seen_msgs:
                    seen_msgs.add(m["id"])
                    all_msg_ids.append(m["id"])
        except Exception as e:
            print(f"ICS search error: {e}", flush=True)

    print(f"  Scanning {len(all_msg_ids)} calendar emails for Notes docs…", flush=True)

    for msg_id in all_msg_ids:
        try:
            full = gmail.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
            for part in _iter_all_parts(full["payload"]):
                fname = (part.get("filename") or "").lower()
                mime  = part.get("mimeType", "")
                if not (fname.endswith(".ics") or mime in ICS_MIMES):
                    continue
                body   = part.get("body", {})
                att_id = body.get("attachmentId")
                if att_id:
                    att  = gmail.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    data = att.get("data", "")
                else:
                    data = body.get("data", "")
                if not data:
                    continue
                raw      = data.replace("-", "+").replace("_", "/")
                ics_text = base64.b64decode(raw + "==").decode("utf-8", errors="replace")
                unfolded = _unfold_ical(ics_text)

                uid_m = UID_RE.search(unfolded)
                if uid_m:
                    uid = uid_m.group(1).strip()
                    if uid in seen_uids:
                        break
                    seen_uids.add(uid)

                for line in unfolded.splitlines():
                    if not line.startswith("ATTACH"):
                        continue
                    m = DOC_ID_RE.search(line)
                    if not m:
                        continue
                    doc_id  = m.group(1)
                    fname_m = re.search(r'FILENAME=([^:;\r\n]+)', line, re.I)
                    raw_name = fname_m.group(1).strip().strip('"\'') if fname_m else "Meeting Notes"
                    docs[doc_id] = raw_name
        except Exception as e:
            print(f"  ICS parse error for msg {msg_id}: {e}", flush=True)

    return docs

def _fetch_doc_text(doc_id, token):
    """Fetch plain-text export of a Google Doc directly (no Docs API needed)."""
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")

# Detects "Action Items" section headers (any capitalisation, optional colon/dash)
_ACTION_ITEMS_HDR = re.compile(
    r'^[-•*\s#]*action\s+items?\s*[:\-]?\s*$',
    re.I,
)
# Detects the START of a new non-empty section (ends an Action Items block)
_SECTION_HDR = re.compile(
    r'^[-•*\s#]*[A-Z][A-Za-z\s]{2,}[:\-]\s*$|'   # "Discussion:", "Notes:", etc.
    r'^#+\s+\S',                                    # markdown headings: ## Foo
    re.I,
)
# Within an Action Items block: line assigned to Sam/Samantha
_SAM_ITEM = re.compile(
    r'^[-•*\s]*(?:'
    r'(?:Samantha(?:\s+Gonzalez)?|Sam)\s*[:–\-]\s*|'   # "Sam: …" / "Sam – …"
    r'(?:Samantha(?:\s+Gonzalez)?|Sam)\s+(?:to|will)\s+'  # "Sam to …" / "Sam will …"
    r')',
    re.I,
)
# Section date headers: "Apr 6, 2026 | …"
_SECTION_DATE_RE = re.compile(r'^([A-Za-z]+ \d{1,2},?\s*\d{4})\s*[|–\-]')
_DATE_FMTS = [
    "%b %d, %Y", "%B %d, %Y",
    "%b %d %Y",  "%B %d %Y",
]

def _parse_section_date(date_str):
    date_str = re.sub(r'\s+', ' ', date_str.strip())
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return None

def extract_doc_sam_tasks(text, doc_name, doc_id, days_back=60):
    """Return Sam's action items listed under 'Action Items' in a meeting notes doc."""
    from datetime import timedelta
    tasks    = []
    seen     = set()
    cutoff   = datetime.now(timezone.utc) - timedelta(days=days_back)
    in_range = True   # content before first date header is always included
    in_action_items = False

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # Date-based section boundary (e.g. "Apr 6, 2026 | Meeting Title")
        dm = _SECTION_DATE_RE.match(s)
        if dm:
            parsed = _parse_section_date(dm.group(1))
            if parsed:
                in_range = parsed.replace(tzinfo=timezone.utc) >= cutoff
            in_action_items = False
            continue

        if not in_range:
            continue

        # Entering an "Action Items" block
        if _ACTION_ITEMS_HDR.match(s):
            in_action_items = True
            continue

        # A new section header ends the Action Items block
        if in_action_items and _SECTION_HDR.match(s):
            in_action_items = False
            continue

        if not in_action_items:
            continue

        # Inside Action Items — match lines assigned to Sam
        ms = _SAM_ITEM.match(s)
        if ms:
            task_text = s[ms.end():].strip().rstrip(".")
            if task_text and len(task_text) >= 5:
                key = hashlib.md5(task_text.lower().encode()).hexdigest()[:8]
                if key not in seen:
                    seen.add(key)
                    tasks.append(make_task(
                        text=task_text, group="carryover", from_sync=True,
                        source=doc_name, source_id=f"doc::{doc_id}::{key}",
                    ))

    return tasks

def run_docs_sync(known_docs_cache=None, last_sync=None):
    """Find meeting notes docs via ICS attachments and extract Sam's action items."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request as GRequest

        creds = _load_creds()
        if creds.refresh_token:
            try:
                creds.refresh(GRequest())
            except Exception as e:
                print(f"  Token refresh warning: {e}", flush=True)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        token = creds.token
        if not token:
            return [], "No access token — try restarting the app", known_docs_cache

        # Work out how many days since last sync for incremental ICS scan
        since_days = None
        if known_docs_cache is not None and last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                since_days = max(1, (datetime.now(timezone.utc) - last_dt).days + 1)
            except Exception:
                pass

        doc_map = _find_notes_docs_from_gmail(gmail,
                                              known_docs=known_docs_cache,
                                              since_days=since_days)
        if not doc_map:
            print("Docs sync: no meeting notes docs found.", flush=True)
            return [], None, {}

        print(f"Docs sync: found {len(doc_map)} doc(s)", flush=True)

        # Fetch all docs in parallel
        all_tasks = []
        def _fetch_one(item):
            doc_id, doc_name = item
            text  = _fetch_doc_text(doc_id, token)
            tasks = extract_doc_sam_tasks(text, doc_name, doc_id)
            print(f"  '{doc_name}' → {len(tasks)} task(s)", flush=True)
            return tasks

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_one, item): item for item in doc_map.items()}
            for fut in as_completed(futures):
                try:
                    all_tasks.extend(fut.result())
                except Exception as e:
                    print(f"  Skipping doc: {e}", flush=True)

        return all_tasks, None, doc_map
    except Exception as e:
        return [], str(e), known_docs_cache

# ── HTML panel ────────────────────────────────────────────────────────────────
def _e(s):
    """HTML-escape."""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def _j(s):
    """JS string escape."""
    return str(s).replace("\\","\\\\").replace("'","\\'").replace("\n","\\n")

def format_sync(iso):
    if not iso:
        return "never"
    try:
        dt   = datetime.fromisoformat(iso)
        diff = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
        mins = int(diff.total_seconds() / 60)
        if mins < 1:  return "just now"
        if mins < 60: return f"{mins}m ago"
        return f"{mins // 60}h ago"
    except Exception:
        return "?"

def render_html(data, new_task_count=0):
    import json as _json
    tasks      = data["tasks"]
    sync_lbl   = _e(format_sync(data.get("lastSync")))
    tasks_json = _json.dumps(tasks)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
:root {{
  --navy:   #3B4878;
  --pink:   #E84B6C;
  --yellow: #F2B024;
  --green:  #5BBD59;
  --bg:     #F4F6F9;
  --white:  #FFFFFF;
  --border: #DDE3EC;
  --border2:#C4CDD8;
  --text1:  #3D3D3D;
  --text2:  #4A5568;
  --text3:  #8898AA;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
select, select option {{ -webkit-appearance: none; appearance: none; font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif; }}
html, body {{
  height: 100vh;
  font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif;
  font-size: 14px; background: var(--bg); color: var(--text1);
  display: flex; flex-direction: column;
  -webkit-font-smoothing: antialiased; overflow: hidden;
}}
.slide-wrap {{ position: relative; flex: 1; min-height: 0; overflow: hidden; }}
#view-main, #view-detail {{
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  display: flex; flex-direction: column;
  transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  will-change: transform; background: var(--bg);
}}
#view-main {{ transform: translateX(0); }}
#view-main.slide-out {{ transform: translateX(-100%); pointer-events: none; }}
#view-detail {{ transform: translateX(100%); pointer-events: none; }}
#view-detail.slide-in {{ transform: translateX(0); pointer-events: auto; }}

/* Header */
header {{
  background: var(--white); padding: 13px 18px 11px;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
.header-row {{ display: flex; align-items: center; justify-content: space-between; }}
header h1 {{ font-size: 15px; font-weight: 700; color: var(--navy); letter-spacing: -0.01em; }}
.sync-btn {{
  display: flex; align-items: center; gap: 5px; background: none;
  border: 1.5px solid var(--border); border-radius: 7px; padding: 5px 12px;
  font-size: 12px; font-weight: 600; font-family: inherit; color: var(--text2);
  cursor: pointer; transition: border-color 0.15s, color 0.15s;
}}
.sync-btn:hover {{ border-color: var(--navy); color: var(--navy); }}
.close-btn {{
  width: 22px; height: 22px; border-radius: 50%;
  background: #E2E6ED; border: none;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; font-family: inherit;
  color: var(--text2); cursor: pointer; flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}}
.close-btn:hover {{ background: var(--pink); color: #fff; }}
.last-sync {{ font-size: 11px; color: var(--text3); margin-top: 4px; }}

/* Tab bar */
.tab-bar {{
  display: flex; background: var(--white);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
/* Sync toast */
.sync-toast {{
  position: absolute; top: 0; left: 0; right: 0; z-index: 500;
  background: var(--navy); color: #fff;
  padding: 9px 14px 9px 16px;
  display: flex; align-items: center; justify-content: space-between;
  font-size: 12px; font-weight: 600;
  transform: translateY(-100%);
  transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
  border-radius: 0 0 8px 8px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}}
.sync-toast.show {{ transform: translateY(0); }}
.sync-toast-dismiss {{
  background: none; border: none; color: rgba(255,255,255,0.7);
  cursor: pointer; font-size: 16px; line-height: 1; padding: 0 0 0 10px;
  transition: color 0.12s;
}}
.sync-toast-dismiss:hover {{ color: #fff; }}
/* View toggle bar */
.view-bar {{
  display: flex; align-items: center; gap: 5px;
  padding: 7px 14px; background: var(--bg);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
.view-btn {{
  padding: 4px 13px; border-radius: 20px; border: none;
  font-size: 11px; font-weight: 600; font-family: inherit;
  cursor: pointer; background: none; color: var(--text3);
  transition: background 0.12s, color 0.12s;
}}
.view-btn.active {{ background: var(--navy); color: #fff; }}
.tab {{
  flex: 1; padding: 9px 0; background: none; border: none;
  border-bottom: 2px solid transparent;
  font-size: 13px; font-weight: 600; font-family: inherit;
  color: var(--text2); cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}}
.tab.active {{ color: var(--navy); border-bottom-color: var(--navy); }}
.filter-icon-btn {{
  flex: 0 0 auto; padding: 9px 14px; position: relative;
  border-left: 1px solid var(--border);
}}
.filter-icon-btn.active {{ color: var(--navy); }}
.filter-badge {{
  position: absolute; top: 4px; right: 5px;
  background: var(--navy); color: #fff;
  font-size: 9px; font-weight: 700; border-radius: 8px;
  padding: 1px 4px; line-height: 1.4; pointer-events: none;
}}
.search-icon-btn {{
  flex: 0 0 auto; padding: 9px 14px; position: relative;
  border-left: 1px solid var(--border);
}}
.search-icon-btn.active {{ color: var(--navy); }}
/* Search bar */
.search-bar {{
  display: none; align-items: center; gap: 8px;
  padding: 8px 14px; background: var(--white);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
.search-bar.open {{ display: flex; }}
.search-input {{
  flex: 1; border: 1.5px solid var(--border); border-radius: 6px;
  padding: 6px 10px; font-size: 12px; font-family: inherit;
  color: var(--text1); background: var(--bg); outline: none;
  transition: border-color 0.15s;
}}
.search-input:focus {{ border-color: var(--navy); }}
.search-clear {{
  background: none; border: none; font-size: 15px; line-height: 1;
  color: var(--text3); cursor: pointer; padding: 2px 4px; flex-shrink: 0;
  transition: color 0.12s;
}}
.search-clear:hover {{ color: var(--pink); }}
.search-count {{
  font-size: 11px; color: var(--text3); flex-shrink: 0; white-space: nowrap;
}}

/* Filter bar */
.filters {{
  background: var(--white); border-bottom: 1px solid var(--border);
  padding: 9px 14px 8px; flex-shrink: 0; display: none;
}}
.filters.open {{ display: block; }}
.filter-row {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin-bottom: 7px; }}
/* Custom dropdowns */
.cs-wrap {{ position: relative; }}
.cs-btn {{
  width: 100%; border: 1.5px solid var(--border); border-radius: 6px;
  padding: 5px 7px; font-size: 11px; font-weight: 500;
  font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif;
  color: var(--text2); background: var(--bg); cursor: pointer;
  display: flex; align-items: center; justify-content: space-between;
  user-select: none; transition: border-color 0.15s; box-sizing: border-box;
}}
.cs-btn:hover {{ border-color: var(--border2); }}
.cs-btn.active {{ border-color: var(--navy); background: #EEF1FB; color: var(--navy); font-weight: 600; }}
.cs-arrow {{ font-size: 8px; color: var(--text3); margin-left: 3px; flex-shrink: 0; }}
.cs-list {{
  position: absolute; top: calc(100% + 3px); left: 0; min-width: 100%; z-index: 200;
  background: var(--white); border: 1.5px solid var(--border); border-radius: 7px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.12); display: none; overflow: hidden;
}}
.cs-list.open {{ display: block; }}
.cs-opt {{
  padding: 7px 10px 7px 26px; font-size: 11px; font-weight: 500; cursor: pointer;
  font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif;
  color: var(--text2); white-space: nowrap; position: relative;
}}
.cs-opt:hover {{ background: var(--bg); }}
.cs-opt.sel {{ color: var(--navy); font-weight: 700; }}
.cs-opt.sel::before {{ content: '✓'; position: absolute; left: 8px; font-size: 10px; color: var(--navy); }}
.filter-meta {{ display: flex; align-items: center; gap: 8px; }}
.filter-count {{ font-size: 11px; color: var(--text3); flex: 1; }}
.filter-clear {{
  background: none; border: none; font-size: 11px; font-weight: 600;
  font-family: inherit; color: var(--navy); cursor: pointer; padding: 0; display: none;
}}
.filter-clear.visible {{ display: block; }}
.filter-clear:hover {{ color: var(--pink); }}

/* Scroll */
.scroll {{ flex: 1; overflow-y: auto; padding: 10px 14px 6px; }}
.scroll::-webkit-scrollbar {{ width: 3px; }}
.scroll::-webkit-scrollbar-track {{ background: transparent; }}
.scroll::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

/* Section headers */
.sec-hdr {{
  display: flex; align-items: center; gap: 7px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--text3); padding: 12px 0 5px;
}}
.sec-dot {{ width: 6px; height: 6px; border-radius: 50%; }}
.dot-rapid  {{ background: var(--green); }}
.dot-focus  {{ background: var(--yellow); }}
.dot-sprint {{ background: var(--navy); }}
.dot-other  {{ background: var(--border2); }}
.sec-count {{
  font-size: 10px; font-weight: 700; letter-spacing: 0;
  text-transform: none; padding: 1px 6px; border-radius: 4px;
}}
.cnt-rapid  {{ background: #E7F8EB; color: #26793A; }}
.cnt-focus  {{ background: #FEF5DC; color: #A07000; }}
.cnt-sprint {{ background: #ECEFFE; color: var(--navy); }}
.cnt-other  {{ background: var(--border); color: var(--text2); }}

/* Task card */
.task {{
  display: flex; align-items: flex-start; gap: 10px;
  background: var(--white); border-radius: 8px;
  padding: 11px 6px 11px 0; margin-bottom: 4px;
  border: 1px solid var(--border); border-left: 3px solid transparent;
  cursor: pointer; transition: box-shadow 0.12s;
}}
.task:hover {{ box-shadow: 0 1px 6px rgba(0,0,0,0.07); }}
.task.dragging {{ opacity: 0.35; }}
.task.drop-before {{ box-shadow: 0 -2.5px 0 var(--navy); }}
.task.drop-after  {{ box-shadow: 0  2.5px 0 var(--navy); }}
.sec-hdr.drop-target {{ background: rgba(59,72,120,0.07); border-radius: 6px; }}
.task.done {{ opacity: 0.38; }}
.task.done .tx {{ text-decoration: line-through; color: var(--text3); }}
.task.cat-rapid_response  {{ border-left-color: var(--green); }}
.task.cat-focus_time      {{ border-left-color: var(--yellow); }}
.task.cat-project_sprint  {{ border-left-color: var(--navy); }}

/* Checkbox */
.chk {{
  width: 17px; height: 17px; border: 1.5px solid var(--border2); border-radius: 4px;
  flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  cursor: pointer; margin-top: 2px; margin-left: 11px;
  transition: border-color 0.12s, background 0.12s; background: var(--white);
}}
.chk:hover {{ border-color: var(--navy); }}
.task.done .chk {{ background: var(--navy); border-color: var(--navy); }}

/* Task body */
.bd {{ flex: 1; min-width: 0; }}
.tx {{ font-size: 13px; font-weight: 500; line-height: 1.4; word-break: break-word; color: var(--text1); }}

/* Star button */
.star-btn {{
  background: none; border: none; font-size: 16px; line-height: 1;
  cursor: pointer; padding: 1px 3px; flex-shrink: 0;
  color: var(--border2); margin-top: 1px; transition: color 0.12s;
}}
.star-btn:hover {{ color: var(--yellow); }}
.star-btn.starred {{ color: var(--yellow); }}

/* Triage selects */
.task-meta {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; align-items: center; }}
/* Inline task dropdowns */
.ti-wrap {{ position: relative; display: inline-block; }}
.ti-btn {{
  border: none; border-radius: 20px; padding: 3px 10px;
  font-size: 10px; font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif; font-weight: 600;
  background: var(--bg); color: var(--text3); cursor: pointer;
  display: inline-flex; align-items: center; gap: 3px; white-space: nowrap;
  transition: background 0.12s, color 0.12s; user-select: none;
}}
/* Status pills — standard, subtle */
.ti-btn.sel-todo {{ background: #F1F3F5; color: #64748B; }}
.ti-btn.sel-prog {{ background: #DBEAFE; color: #1D4ED8; }}
.ti-btn.sel-done {{ background: #DCFCE7; color: #15803D; }}
.ti-btn.sel-wait {{ background: #FFEDD5; color: #C2410C; }}
/* Category pills — subtle tints */
.ti-btn.sel-cat-rapid  {{ background: #DCFCE7; color: #15803D; }}
.ti-btn.sel-cat-focus  {{ background: #FEF9C3; color: #854D0E; }}
.ti-btn.sel-cat-sprint {{ background: #E0E7FF; color: #3730A3; }}
.ti-arrow {{ font-size: 7px; opacity: 0.6; }}
.ti-list {{
  position: absolute; top: calc(100% + 2px); left: 0; z-index: 300;
  background: var(--white); border: 1.5px solid var(--border); border-radius: 6px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.12); display: none; min-width: 100%; overflow: hidden;
}}
.ti-list.open {{ display: block; }}
.ti-opt {{
  padding: 5px 9px; font-size: 10px; font-weight: 500; cursor: pointer; white-space: nowrap;
  font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif;
  color: var(--text2);
}}
.ti-opt:hover {{ background: var(--bg); }}
.ti-opt.sel {{ font-weight: 700; color: var(--navy); }}
.ti-proj {{
  border: 1.5px solid var(--border); border-radius: 5px; padding: 2px 6px;
  font-size: 10px; font-weight: 500; width: 88px;
  font-family: 'proxima-nova', 'Proxima Nova', -apple-system, 'Helvetica Neue', sans-serif;
  background: var(--bg); color: var(--text2); outline: none;
}}
.ti-proj:focus {{ border-color: var(--navy); background: var(--white); }}
.ti-proj:not(:placeholder-shown) {{ border-color: var(--navy); background: #EEF1FB; color: var(--navy); font-weight: 600; }}
.src {{ display: block; font-size: 10px; color: var(--text3); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.comp-ts {{ display: block; font-size: 10px; color: var(--green); font-weight: 600; margin-top: 4px; }}
.note-badge {{
  display: flex; align-items: center; gap: 3px; flex-shrink: 0;
  font-size: 10px; font-weight: 600; color: var(--text3);
  margin-top: 1px; padding: 2px 4px;
  transition: color 0.12s;
}}
.note-badge:hover {{ color: var(--navy); }}

/* Delete */
.del {{
  background: none; border: none; color: var(--border); font-size: 13px; line-height: 1;
  cursor: pointer; padding: 2px 8px 2px 2px; flex-shrink: 0;
  transition: color 0.12s; margin-top: 2px;
}}
.del:hover {{ color: var(--pink); }}
.empty {{ color: var(--text3); font-size: 13px; padding: 8px 2px; }}

/* Footer */
footer {{
  padding: 11px 14px 13px; background: var(--white);
  border-top: 1px solid var(--border); flex-shrink: 0;
}}
.form-label {{ font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text3); margin-bottom: 7px; }}
.form-row1 {{ margin-bottom: 6px; }}
.form-row2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 6px; }}
.form-row3 {{ display: grid; grid-template-columns: 1fr auto; gap: 6px; }}
.fi {{
  width: 100%; border: 1.5px solid var(--border); border-radius: 7px; padding: 7px 10px;
  font-size: 13px; font-family: inherit; font-weight: 400;
  outline: none; color: var(--text1); background: var(--bg);
  transition: border-color 0.12s, background 0.12s;
}}
.fi::placeholder {{ color: var(--text3); }}
.fi:focus {{ border-color: var(--navy); background: var(--white); }}
.footer-meta {{ display: flex; justify-content: flex-end; margin-top: 7px; }}
.clr {{ background: none; border: none; color: var(--text3); font-size: 12px; font-family: inherit; cursor: pointer; transition: color 0.12s; }}
.clr:hover {{ color: var(--pink); }}
.ab {{ background: var(--navy); color: white; border: none; border-radius: 7px; padding: 7px 18px; font-size: 13px; font-weight: 700; font-family: inherit; cursor: pointer; transition: background 0.12s; white-space: nowrap; }}
.ab:hover {{ background: #4A5A97; }}

/* ── Detail panel ── */
.dp-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; background: var(--white);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
.dp-back {{
  background: none; border: none; color: var(--navy); cursor: pointer;
  padding: 4px 8px 4px 2px; border-radius: 6px;
  display: flex; align-items: center; transition: background 0.12s;
}}
.dp-back:hover {{ background: var(--bg); }}
.dp-header-right {{ display: flex; align-items: center; gap: 4px; }}
.dp-icon-btn {{
  background: none; border: none; cursor: pointer; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; font-size: 17px;
  color: var(--text3); transition: background 0.12s, color 0.12s;
}}
.dp-icon-btn:hover {{ background: var(--bg); color: var(--navy); }}
.dp-icon-btn.dp-starred {{ color: #F2B024; }}
.dp-icon-btn.dp-trash:hover {{ color: var(--pink); }}
.dp-title-area {{
  padding: 14px 16px 10px; flex-shrink: 0; background: var(--white);
}}
.dp-title {{
  font-size: 16px; font-weight: 700; color: var(--text1);
  line-height: 1.4; word-break: break-word;
  width: 100%; border: none; background: transparent; resize: none;
  font-family: inherit; padding: 0; outline: none;
  border-bottom: 1.5px solid transparent; transition: border-color 0.15s;
}}
.dp-title:focus {{ border-bottom-color: var(--navy); }}
.dp-desc {{
  width: 100%; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); font-family: inherit; font-size: 12.5px;
  color: var(--text1); line-height: 1.5; padding: 8px 10px;
  resize: none; outline: none; min-height: 60px;
  transition: border-color 0.15s; box-sizing: border-box;
}}
.dp-desc:focus {{ border-color: var(--navy); background: var(--white); }}
.dp-desc::placeholder {{ color: var(--text3); }}
.dp-desc-wrap {{
  padding: 0 16px 12px; flex-shrink: 0; background: var(--white);
}}
.dp-section-label {{
  font-size: 11px; font-weight: 600; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 5px;
}}
.dp-props {{
  padding: 4px 16px 14px; flex-shrink: 0; background: var(--white);
  position: relative; z-index: 10; overflow: visible;
}}
.dp-prop-row {{
  display: flex; align-items: center; gap: 12px; margin-bottom: 6px;
}}
.dp-prop-label {{
  font-size: 11px; font-weight: 600; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.05em; width: 62px; flex-shrink: 0;
}}
.dp-sep {{
  border: none; border-top: 1px solid var(--border); margin: 0; flex-shrink: 0;
}}
.dp-added-at {{
  font-size: 11px; color: var(--text3); padding: 6px 16px; flex-shrink: 0;
  display: flex; align-items: center;
}}
.dp-activity {{
  flex: 1; overflow-y: auto; padding: 14px 16px 16px;
  display: flex; flex-direction: column;
}}
.dp-activity::-webkit-scrollbar {{ width: 3px; }}
.dp-activity::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}
.dp-activity-heading {{
  font-size: 11px; font-weight: 700; color: var(--text3);
  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 10px;
}}
.dp-note-input {{
  width: 100%; border: 1.5px solid var(--border); border-radius: 8px;
  padding: 8px 11px; font-size: 13px; font-family: inherit;
  resize: none; outline: none; line-height: 1.45; min-height: 62px;
  background: var(--bg); color: var(--text1);
  transition: border-color 0.12s, background 0.12s; margin-bottom: 8px;
}}
.dp-note-input:focus {{ border-color: var(--navy); background: var(--white); }}
.dp-note-input::placeholder {{ color: var(--text3); }}
.dp-add-btn {{
  align-self: flex-end; background: var(--navy); color: #fff;
  border: none; border-radius: 6px; padding: 6px 16px;
  font-size: 12px; font-weight: 600; font-family: inherit;
  cursor: pointer; transition: background 0.12s; margin-bottom: 16px;
}}
.dp-add-btn:hover {{ background: #4A5A97; }}
.dp-notes-list {{ display: flex; flex-direction: column; gap: 14px; }}
.dp-note-item.system {{ padding-right: 0; flex-direction: row; align-items: center; gap: 8px; }}
.dp-note-item.system .dp-note-ts {{ font-weight: 400; color: var(--text3); white-space: nowrap; }}
.dp-note-item.system .dp-note-text {{
  font-size: 11.5px; color: var(--text3); font-style: italic;
}}
.dp-note-item.system::before {{
  content: ''; display: block; width: 6px; height: 6px; border-radius: 50%;
  background: var(--navy); opacity: 0.35; flex-shrink: 0;
}}
.dp-note-item {{
  display: flex; flex-direction: column; gap: 3px;
  position: relative; padding-right: 22px;
}}
.dp-note-ts {{ font-size: 11px; font-weight: 700; color: var(--text2); }}
.dp-note-text {{
  font-size: 13px; color: var(--text1); line-height: 1.5;
  word-break: break-word; white-space: pre-wrap;
}}
.dp-note-del {{
  position: absolute; top: 0; right: 0;
  background: none; border: none; cursor: pointer;
  color: var(--text3); font-size: 13px; padding: 0 2px;
  opacity: 0; transition: opacity 0.12s, color 0.12s;
  line-height: 1;
}}
.dp-note-item:hover .dp-note-del {{ opacity: 1; }}
.note-link {{ color: var(--navy); text-decoration: underline; cursor: pointer; word-break: break-all; }}
.note-link:hover {{ color: var(--pink); }}
.dp-note-del:hover {{ color: var(--pink); }}
.dp-no-notes {{ color: var(--text3); font-size: 13px; }}
</style>
</head><body>
<div class="slide-wrap">

<!-- MAIN VIEW -->
<div id="view-main">
<div class="sync-toast" id="sync-toast">
  <span id="sync-toast-msg"></span>
  <button class="sync-toast-dismiss" onclick="document.getElementById('sync-toast').classList.remove('show')" title="Dismiss">&#x2715;</button>
</div>
<header>
  <div class="header-row">
    <h1>My Tasks</h1>
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="sync-btn" id="sync-now-btn" onclick="startSync()">&#8635; Sync</button>
      <button class="close-btn" onclick="go('close')" title="Close">&#x2715;</button>
    </div>
  </div>
  <div class="last-sync">Last synced {sync_lbl}</div>
</header>

<div class="tab-bar">
  <button id="tab-today" class="tab active" onclick="setTab('today')">Today&#8217;s Plan</button>
  <button id="tab-all"   class="tab"        onclick="setTab('all')">All Tasks</button>
  <button class="tab filter-icon-btn" id="filter-toggle-btn" onclick="toggleFilters()" title="Filters">
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style="display:block;pointer-events:none"><path d="M1 2h11l-4 4.8V11L5 9.5V6.8L1 2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>
    <span class="filter-badge" id="filter-badge" style="display:none">0</span>
  </button>
  <button class="tab search-icon-btn" id="search-toggle-btn" onclick="toggleSearch()" title="Search">
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style="display:block;pointer-events:none"><circle cx="5.5" cy="5.5" r="4" stroke="currentColor" stroke-width="1.5"/><path d="M8.5 8.5L12 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
  </button>
</div>

<div class="search-bar" id="search-bar">
  <input class="search-input" id="search-input" type="text" placeholder="Search tasks, details, notes&#x2026;"
    oninput="onSearchInput(this.value)">
  <span class="search-count" id="search-count"></span>
  <button class="search-clear" id="search-clear-btn" onclick="clearSearch()" title="Clear search" style="display:none">&#x2715;</button>
</div>

<div class="view-bar">
  <button id="view-btn-all"       class="view-btn active"  onclick="setView('all')">All</button>
  <button id="view-btn-active"    class="view-btn"         onclick="setView('active')">Active</button>
  <button id="view-btn-waiting"   class="view-btn"         onclick="setView('waiting')">Waiting</button>
  <button id="view-btn-completed" class="view-btn"         onclick="setView('completed')">Completed</button>
</div>

<div class="filters" id="filters-panel">
  <div class="filter-row">
    <div class="cs-wrap" id="cs-cat">
      <div class="cs-btn" onclick="csToggle('cs-cat')">
        <span class="cs-val">Category</span><span class="cs-arrow">&#9660;</span>
      </div>
      <div class="cs-list">
        <div class="cs-opt" data-value="rapid_response" onclick="csSelect('cs-cat','rapid_response')">Rapid Response</div>
        <div class="cs-opt" data-value="focus_time" onclick="csSelect('cs-cat','focus_time')">Focus Time</div>
        <div class="cs-opt" data-value="project_sprint" onclick="csSelect('cs-cat','project_sprint')">Project Sprint</div>
      </div>
    </div>
    <div class="cs-wrap" id="cs-proj">
      <div class="cs-btn" onclick="csToggle('cs-proj')">
        <span class="cs-val">Project</span><span class="cs-arrow">&#9660;</span>
      </div>
      <div class="cs-list"></div>
    </div>
    <div class="cs-wrap" id="cs-status">
      <div class="cs-btn" onclick="csToggle('cs-status')">
        <span class="cs-val">Status</span><span class="cs-arrow">&#9660;</span>
      </div>
      <div class="cs-list">
        <div class="cs-opt" data-value="todo" onclick="csSelect('cs-status','todo')">To Do</div>
        <div class="cs-opt" data-value="in_progress" onclick="csSelect('cs-status','in_progress')">In Progress</div>
        <div class="cs-opt" data-value="waiting" onclick="csSelect('cs-status','waiting')">Waiting</div>
        <div class="cs-opt" data-value="done" onclick="csSelect('cs-status','done')">Done</div>
      </div>
    </div>
  </div>
  <div class="filter-meta">
    <span class="filter-count" id="filter-count"></span>
    <button class="filter-clear" id="filter-clear" onclick="clearFilters()">&#10005; Clear all</button>
  </div>
</div>

<div class="scroll" id="task-list"></div>

<footer>
  <div class="form-label">Add Task</div>
  <div class="form-row1">
    <input class="fi" id="f-text" placeholder="Task description&#8230;"
           onkeydown="if(event.key==='Enter')submitAdd()">
  </div>
  <div class="form-row2">
    <select class="fi" id="f-cat-add">
      <option value="">Category</option>
      <option value="rapid_response">Rapid Response</option>
      <option value="focus_time">Focus Time</option>
      <option value="project_sprint">Project Sprint</option>
    </select>
    <input class="fi" id="f-proj-add" placeholder="Project / Topic">
  </div>
  <div class="form-row3">
    <select class="fi" id="f-status-add">
      <option value="todo">To Do</option>
      <option value="in_progress">In Progress</option>
      <option value="waiting">Waiting</option>
      <option value="done">Done</option>
    </select>
    <button class="ab" onclick="submitAdd()">Add</button>
  </div>
  <div class="footer-meta">
    <button class="clr" onclick="go('clear_done')">Clear completed</button>
  </div>
</footer>
</div><!-- /view-main -->

<!-- DETAIL VIEW -->
<div id="view-detail">
  <!-- Header -->
  <div class="dp-header">
    <button class="dp-back" onclick="closeDetail()">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M12.5 5L7.5 10l5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="dp-header-right">
      <button class="dp-icon-btn" id="dp-star-btn" onclick="dpToggleStar()" title="Add to Today&#39;s Plan">&#9734;</button>
      <button class="dp-icon-btn dp-trash" onclick="dpDelete()" title="Delete task">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1.5 3.5h11M5 3.5V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5v1M2.5 3.5l.75 8a.5.5 0 00.5.5h6.5a.5.5 0 00.5-.5l.75-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>
  </div>
  <!-- Title -->
  <div class="dp-title-area">
    <textarea class="dp-title" id="dp-title" rows="1"
      placeholder="Task title"
      onkeydown="if(event.key==='Enter'){{event.preventDefault();this.blur();}}"
      oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px';"
      onblur="dpSaveTitle(this.value)"></textarea>
  </div>
  <!-- Properties -->
  <div class="dp-props">
    <div class="dp-prop-row">
      <span class="dp-prop-label">Status</span>
      <div id="dp-status-wrap"></div>
    </div>
    <div class="dp-prop-row">
      <span class="dp-prop-label">Category</span>
      <div id="dp-cat-wrap"></div>
    </div>
  </div>
  <!-- Description -->
  <div class="dp-desc-wrap">
    <div class="dp-section-label">Details</div>
    <textarea class="dp-desc" id="dp-desc"
      onblur="dpSaveDesc(this.value)"></textarea>
  </div>
  <div class="dp-added-at" id="dp-added-at"></div>
  <!-- Separator -->
  <hr class="dp-sep">
  <!-- Activity log -->
  <div class="dp-activity">
    <div class="dp-activity-heading">Activity Log</div>
    <textarea class="dp-note-input" id="dp-note-input" placeholder="Add a note&#8230;"></textarea>
    <button class="dp-add-btn" onclick="dpSubmitNote()">Add Note</button>
    <div class="dp-notes-list" id="dp-notes-list"></div>
  </div>
</div><!-- /view-detail -->

</div><!-- /slide-wrap -->

<script>
var allTasks     = {tasks_json};
var currentTab   = 'today';
var currentView  = 'all';
var detailTaskId = null;
var searchQuery  = '';

var CAT_OPTS = [
  ['', 'Category\u2026'],
  ['rapid_response', 'Rapid Response'],
  ['focus_time', 'Focus Time'],
  ['project_sprint', 'Project Sprint']
];
var STATUS_OPTS = [
  ['todo', 'To Do'],
  ['in_progress', 'In Progress'],
  ['waiting', 'Waiting'],
  ['done', 'Done']
];
var CHECK = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><polyline points="1.5,5 4,7.5 8.5,2" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function openLink(e) {{
  var url = e.currentTarget.getAttribute('data-href');
  go('open-url', null, {{url: url}});
  return false;
}}
function linkify(s) {{
  var urlRe = /(https?:\/\/[^\s<>"]+)/g;
  var out = '', last = 0, m;
  while ((m = urlRe.exec(s)) !== null) {{
    out += esc(s.slice(last, m.index));
    out += '<a class="note-link" href="#" data-href="' + esc(m[1]) +
           '" onclick="return openLink(event);">' + esc(m[1]) + '</a>';
    last = m.index + m[0].length;
  }}
  return out + esc(s.slice(last));
}}
function isDone(t) {{ return t.done || t.status === 'done'; }}
function catSelCls(cat) {{
  if (cat === 'rapid_response') return 'sel-cat-rapid';
  if (cat === 'focus_time')     return 'sel-cat-focus';
  if (cat === 'project_sprint') return 'sel-cat-sprint';
  return '';
}}
function stSelCls(st) {{
  if (st === 'done')        return 'sel-done';
  if (st === 'in_progress') return 'sel-prog';
  if (st === 'waiting')     return 'sel-wait';
  if (st === 'todo')        return 'sel-todo';
  return '';
}}

function makeSelect(tid, field, opts, val, extraCls, uidPrefix) {{
  var uid = (uidPrefix||'ti-') + tid.replace(/[^a-zA-Z0-9_-]/g, '-') + '-' + field;
  var selLabel = opts[0][1];
  for (var k=0; k<opts.length; k++) {{
    if (opts[k][0]===val) {{ selLabel=opts[k][1]; break; }}
  }}
  var btnCls = 'ti-btn' + (extraCls ? ' '+extraCls : '');
  var html = '<div class="ti-wrap" id="' + uid + '">';
  html += '<div class="' + btnCls + '" onclick="event.stopPropagation();tiToggle(&#39;' + uid + '&#39;)">';
  html += '<span class="ti-val">' + esc(selLabel) + '</span><span class="ti-arrow">&#9660;</span></div>';
  html += '<div class="ti-list">';
  opts.forEach(function(o) {{
    html += '<div class="ti-opt' + (o[0]===val?' sel':'') + '" data-value="' + esc(o[0]) + '" onclick="event.stopPropagation();tiSelect(&#39;' + uid + '&#39;,&#39;' + tid + '&#39;,&#39;' + field + '&#39;,&#39;' + esc(o[0]) + '&#39;,&#39;' + esc(o[1]) + '&#39;)">' + esc(o[1]) + '</div>';
  }});
  html += '</div></div>';
  return html;
}}

function tiToggle(wid) {{
  var list = document.querySelector('#'+wid+' .ti-list');
  if (!list) return;
  var isOpen = list.classList.contains('open');
  document.querySelectorAll('.ti-list').forEach(function(el) {{ el.classList.remove('open'); }});
  if (!isOpen) list.classList.add('open');
}}

function tiSelect(wid, tid, field, value, label) {{
  var btn = document.querySelector('#'+wid+' .ti-btn');
  if (btn) {{
    document.querySelector('#'+wid+' .ti-val').textContent = label;
    btn.className = 'ti-btn';
    if (field==='category') {{
      if (value==='rapid_response') btn.classList.add('sel-cat-rapid');
      else if (value==='focus_time')     btn.classList.add('sel-cat-focus');
      else if (value==='project_sprint') btn.classList.add('sel-cat-sprint');
    }}
    if (field==='status') {{
      if (value==='done')             btn.classList.add('sel-done');
      else if (value==='in_progress') btn.classList.add('sel-prog');
      else if (value==='waiting')     btn.classList.add('sel-wait');
      else if (value==='todo')        btn.classList.add('sel-todo');
    }}
    document.querySelectorAll('#'+wid+' .ti-opt').forEach(function(o) {{
      o.classList.toggle('sel', o.dataset.value===value);
    }});
    document.querySelector('#'+wid+' .ti-list').classList.remove('open');
  }}
  updateTask(tid, field, value);
}}

function renderTask(t) {{
  var done    = isDone(t) ? ' done' : '';
  var catCls  = t.category ? ' cat-' + t.category : '';
  var tid     = t.id;
  var chk     = isDone(t) ? CHECK : '';
  var catVal  = t.category || '';
  var stVal   = t.status || (t.done ? 'done' : 'todo');
  var starred = t.today ? true : false;
  var starCls = starred ? ' starred' : '';
  var starIco = starred ? '&#9733;' : '&#9734;';
  var noteBadge = (t.notes && t.notes.length)
    ? '<span class="note-badge" title="'+t.notes.length+' note'+(t.notes.length!==1?'s':'')+'">'+
      '<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M1 1.5h10c.3 0 .5.2.5.5v6c0 .3-.2.5-.5.5H3.5L1 10.5V2c0-.3.2-.5.5-.5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>'+
      t.notes.length+'</span>'
    : '';
  var catSel = makeSelect(tid,'category',CAT_OPTS,catVal,catSelCls(catVal));
  var stSel  = makeSelect(tid,'status',STATUS_OPTS,stVal,stSelCls(stVal));
  var projInput = (t.category === 'project_sprint')
    ? '<input class="ti-proj" value="'+esc(t.project||'')+'" placeholder="Project\u2026"'+
      ' onclick="event.stopPropagation()"'+
      ' onkeydown="if(event.key===&#39;Enter&#39;)this.blur()"'+
      ' onblur="updateTask(&#39;'+tid+'&#39;,&#39;project&#39;,this.value)">'
    : '';
  var src    = (t.source && t.fromSync) ? '<span class="src">&#8599; '+esc(t.source)+'</span>' : '';
  var compTs = isDone(t) ? '<span class="comp-ts">&#10003; '+fmtCompleted(t.completedAt||null)+'</span>' : '';

  return ('<div class="task'+done+catCls+'" data-tid="'+tid+'" draggable="true"' +
    ' ondragstart="onDragStart(event,&#39;'+tid+'&#39;)"' +
    ' ondragend="onDragEnd(event)"' +
    ' ondragover="onDragOver(event,&#39;'+tid+'&#39;)"' +
    ' ondrop="onDrop(event,&#39;'+tid+'&#39;)"' +
    ' onclick="openDetail(&#39;'+tid+'&#39;)">') +
    '<div class="chk" onclick="event.stopPropagation();toggleDone(&#39;'+tid+'&#39;)">'+chk+'</div>' +
    '<div class="bd">' +
      '<span class="tx">'+esc(t.text)+'</span>' +
      '<div class="task-meta" onclick="event.stopPropagation()">'+catSel+stSel+projInput+'</div>' +
      src+compTs +
    '</div>' +
    noteBadge +
    '<button class="star-btn'+starCls+'" onclick="event.stopPropagation();toggleToday(&#39;'+tid+'&#39;)" title="Add to Today&#39;s Plan">'+starIco+'</button>' +
    '<button class="del" onclick="event.stopPropagation();deleteTask(&#39;'+tid+'&#39;)">&#x2715;</button>' +
    '</div>';
}}

var SECTIONS = [
  {{ key:'',               label:'Other',          dot:'other',  cnt:'cnt-other'  }},
  {{ key:'rapid_response', label:'Rapid Response', dot:'rapid',  cnt:'cnt-rapid'  }},
  {{ key:'focus_time',     label:'Focus Time',     dot:'focus',  cnt:'cnt-focus'  }},
  {{ key:'project_sprint', label:'Project Sprint', dot:'sprint', cnt:'cnt-sprint' }}
];

function renderGrouped(tasks) {{
  var html = '';
  SECTIONS.forEach(function(s) {{
    var group = tasks.filter(function(t) {{ return (t.category||'')===s.key; }});
    if (!group.length) return;
    html += '<div class="sec-hdr" data-cat="'+s.key+'" ondragover="onDragOverHeader(event,&#39;'+s.key+'&#39;)" ondrop="onDropHeader(event,&#39;'+s.key+'&#39;)"><span class="sec-dot dot-'+s.dot+'"></span><span>'+s.label+'</span><span class="sec-count '+s.cnt+'">'+group.length+'</span></div>';
    html += group.map(renderTask).join('');
  }});
  return html || '<p class="empty">No tasks yet</p>';
}}

var CS_DEFAULTS = {{ 'cs-cat':'Category', 'cs-proj':'Project', 'cs-status':'Status' }};

function fmtCompleted(iso) {{
  if (!iso) return 'Completed';
  var d = new Date(iso);
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var mo = months[d.getMonth()], day = d.getDate();
  var h = d.getHours(), m = d.getMinutes();
  var ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return mo + ' ' + day + ' at ' + h + ':' + (m<10?'0':'')+m + ' ' + ampm;
}}

function taskMatchesSearch(t, q) {{
  if (!q) return true;
  if ((t.text || '').toLowerCase().indexOf(q) !== -1) return true;
  if ((t.description || '').toLowerCase().indexOf(q) !== -1) return true;
  if (t.notes && t.notes.length) {{
    for (var i = 0; i < t.notes.length; i++) {{
      if ((t.notes[i].text || '').toLowerCase().indexOf(q) !== -1) return true;
    }}
  }}
  return false;
}}
function applyFilters() {{
  var fCat     = csValues('cs-cat');
  var fProj    = csValues('cs-proj');
  var fStatus  = csValues('cs-status');
  var showComp = (currentView === 'completed');
  var anyFilter = fCat.length || fProj.length || fStatus.length;
  var q = searchQuery;

  var filtered = allTasks.filter(function(t) {{
    if (!q) {{
      // normal tab filter only when not searching
      if (currentTab === 'today' && !t.today) return false;
    }}
    if (showComp) {{
      if (!isDone(t)) return false;
    }} else {{
      if (isDone(t)) return false;
      var vst = t.status || 'todo';
      if (currentView === 'active'  && vst !== 'todo' && vst !== 'in_progress') return false;
      if (currentView === 'waiting' && vst !== 'waiting') return false;
    }}
    if (fCat.length    && fCat.indexOf(t.category||'')    === -1) return false;
    if (fProj.length   && fProj.indexOf(t.project||'')    === -1) return false;
    if (fStatus.length) {{
      var st = t.status || (t.done ? 'done' : 'todo');
      if (fStatus.indexOf(st) === -1) return false;
    }}
    if (q && !taskMatchesSearch(t, q)) return false;
    return true;
  }});

  if (showComp) {{
    filtered.sort(function(a, b) {{
      var ta = a.completedAt ? new Date(a.completedAt).getTime() : 0;
      var tb = b.completedAt ? new Date(b.completedAt).getTime() : 0;
      return tb - ta;
    }});
  }}

  var scrollEl = document.querySelector('.scroll');
  var savedScroll = scrollEl ? scrollEl.scrollTop : 0;
  var list = document.getElementById('task-list');
  list.innerHTML = renderGrouped(filtered);
  if (scrollEl) scrollEl.scrollTop = savedScroll;

  var countEl = document.getElementById('filter-count');
  var clearEl = document.getElementById('filter-clear');
  if (anyFilter) {{
    countEl.textContent = filtered.length + ' task' + (filtered.length!==1?'s':'');
    clearEl.classList.add('visible');
  }} else {{
    countEl.textContent = '';
    clearEl.classList.remove('visible');
  }}
  updateFilterBadge();

  // update search result count
  var scountEl = document.getElementById('search-count');
  if (scountEl) {{
    scountEl.textContent = q ? (filtered.length + ' result' + (filtered.length!==1?'s':'')) : '';
  }}
}}

function clearFilters() {{
  csState['cs-cat']    = [];
  csState['cs-proj']   = [];
  csState['cs-status'] = [];
  updateCsBtn('cs-cat');
  updateCsBtn('cs-proj');
  updateCsBtn('cs-status');
  applyFilters();
}}

var csState = {{}};
function csValues(id) {{ return csState[id] || []; }}
function csToggle(id) {{
  var list = document.querySelector('#'+id+' .cs-list');
  var isOpen = list.classList.contains('open');
  document.querySelectorAll('.cs-list').forEach(function(el) {{ el.classList.remove('open'); }});
  if (!isOpen) list.classList.add('open');
}}
function csSelect(id, value) {{
  var arr = csState[id] || [];
  var idx = arr.indexOf(value);
  if (idx === -1) arr.push(value); else arr.splice(idx, 1);
  csState[id] = arr;
  updateCsBtn(id);
  applyFilters();
}}
function updateCsBtn(id) {{
  var arr = csState[id] || [];
  var btn = document.querySelector('#'+id+' .cs-btn');
  var val = document.querySelector('#'+id+' .cs-val');
  if (!btn || !val) return;
  if (arr.length === 0) {{
    val.textContent = CS_DEFAULTS[id] || '';
    btn.classList.remove('active');
  }} else if (arr.length === 1) {{
    var opt = document.querySelector('#'+id+' .cs-opt[data-value="'+arr[0]+'"]');
    val.textContent = opt ? opt.textContent.trim() : arr[0];
    btn.classList.add('active');
  }} else {{
    val.textContent = arr.length + ' selected';
    btn.classList.add('active');
  }}
  document.querySelectorAll('#'+id+' .cs-opt').forEach(function(o) {{
    o.classList.toggle('sel', arr.indexOf(o.dataset.value) !== -1);
  }});
}}
function updateFilterBadge() {{
  var count = (csState['cs-cat']||[]).length + (csState['cs-proj']||[]).length + (csState['cs-status']||[]).length;
  var badge = document.getElementById('filter-badge');
  if (!badge) return;
  if (count > 0) {{ badge.textContent = count; badge.style.display = ''; }}
  else {{ badge.style.display = 'none'; }}
}}
function toggleFilters() {{
  var panel = document.getElementById('filters-panel');
  var btn   = document.getElementById('filter-toggle-btn');
  panel.classList.toggle('open');
  btn.classList.toggle('active', panel.classList.contains('open'));
}}
function toggleSearch() {{
  var bar = document.getElementById('search-bar');
  var btn = document.getElementById('search-toggle-btn');
  var open = bar.classList.toggle('open');
  btn.classList.toggle('active', open);
  if (open) {{
    document.getElementById('search-input').focus();
  }} else {{
    clearSearch();
  }}
}}
function onSearchInput(val) {{
  searchQuery = val.trim().toLowerCase();
  var clearBtn  = document.getElementById('search-clear-btn');
  clearBtn.style.display = searchQuery ? '' : 'none';
  applyFilters();
}}
function clearSearch() {{
  searchQuery = '';
  var inp = document.getElementById('search-input');
  if (inp) inp.value = '';
  var clearBtn = document.getElementById('search-clear-btn');
  if (clearBtn) clearBtn.style.display = 'none';
  var countEl = document.getElementById('search-count');
  if (countEl) countEl.textContent = '';
  applyFilters();
}}
document.addEventListener('click', function(e) {{
  if (!e.target.closest('.cs-wrap')) {{
    document.querySelectorAll('.cs-list').forEach(function(el) {{ el.classList.remove('open'); }});
  }}
  if (!e.target.closest('.ti-wrap')) {{
    document.querySelectorAll('.ti-list').forEach(function(el) {{ el.classList.remove('open'); }});
  }}
}});

function setTab(tab) {{
  currentTab = tab;
  document.getElementById('tab-all').classList.toggle('active', tab==='all');
  document.getElementById('tab-today').classList.toggle('active', tab==='today');
  applyFilters();
}}

function setView(view) {{
  currentView = view;
  document.getElementById('view-btn-all').classList.toggle('active', view==='all');
  document.getElementById('view-btn-active').classList.toggle('active', view==='active');
  document.getElementById('view-btn-waiting').classList.toggle('active', view==='waiting');
  document.getElementById('view-btn-completed').classList.toggle('active', view==='completed');
  applyFilters();
}}

function buildProjectFilter() {{
  var projects = [];
  allTasks.forEach(function(t) {{
    if (t.project && projects.indexOf(t.project)===-1) projects.push(t.project);
  }});
  projects.sort();
  var arr  = csValues('cs-proj');
  var list = document.querySelector('#cs-proj .cs-list');
  var html = '';
  projects.forEach(function(p) {{
    html += '<div class="cs-opt'+(arr.indexOf(p)!==-1?' sel':'')+'" data-value="'+esc(p)+'" onclick="csSelect(&#39;cs-proj&#39;,&#39;'+esc(p)+'&#39;)">'+esc(p)+'</div>';
  }});
  list.innerHTML = html;
}}

function updateTask(id, field, value) {{
  for (var i=0; i<allTasks.length; i++) {{
    if (allTasks[i].id===id) {{
      var oldStatus = (field==='status') ? (allTasks[i].status || 'todo') : null;
      allTasks[i][field] = value;
      if (field==='status') {{
        var wasDone = allTasks[i].done;
        allTasks[i].done = (value==='done');
        if (allTasks[i].done && !wasDone) {{
          allTasks[i].completedAt = new Date().toISOString();
        }} else if (!allTasks[i].done) {{
          allTasks[i].completedAt = null;
        }}
        // Record status change in the activity log
        if (oldStatus !== value) {{
          var statusLabels = {{todo:'To Do',in_progress:'In Progress',waiting:'Waiting',done:'Done'}};
          var now = new Date();
          var mo = now.getMonth()+1, day = now.getDate(), yr = now.getFullYear();
          var h = now.getHours(), m = now.getMinutes();
          var ampm = h>=12?'PM':'AM'; h = h%12||12;
          var ts = mo+'/'+day+'/'+yr+' '+h+':'+(m<10?'0':'')+m+' '+ampm;
          var note = {{
            text: 'Status changed from '+(statusLabels[oldStatus]||oldStatus)+' → '+(statusLabels[value]||value),
            timestamp: ts,
            system: true
          }};
          if (!allTasks[i].notes) allTasks[i].notes = [];
          allTasks[i].notes.push(note);
          go('add-note', id, {{text: note.text, timestamp: ts, system: true}});
        }}
        // In Today's Plan view, move waiting tasks to the bottom of their category
        if (value === 'waiting' && currentTab === 'today') {{
          var task = allTasks.splice(i, 1)[0];
          var cat  = task.category || '';
          // Find the last task in the same category and insert after it
          var lastIdx = allTasks.length;
          for (var j = allTasks.length - 1; j >= 0; j--) {{
            if ((allTasks[j].category || '') === cat) {{ lastIdx = j + 1; break; }}
          }}
          allTasks.splice(lastIdx, 0, task);
          var orderIds = allTasks.map(function(t) {{ return t.id; }});
          go('reorder', null, {{ order: orderIds }});
        }}
      }}
      break;
    }}
  }}
  var payload = {{}};
  payload[field] = value;
  window.webkit.messageHandlers.bridge.postMessage({{action:'update',id:id,extra:payload}});
  buildProjectFilter();
  applyFilters();
  if (detailTaskId===id) {{
    var t = allTasks.find(function(t) {{ return t.id===id; }});
    if (t) renderDetail(t);
  }}
}}

function toggleDone(tid) {{
  for (var i=0; i<allTasks.length; i++) {{
    if (allTasks[i].id===tid) {{
      allTasks[i].done = !allTasks[i].done;
      allTasks[i].status = allTasks[i].done ? 'done'
        : (allTasks[i].status==='done' ? 'todo' : allTasks[i].status);
      if (allTasks[i].done) {{
        allTasks[i].completedAt = new Date().toISOString();
      }} else {{
        allTasks[i].completedAt = null;
      }}
      break;
    }}
  }}
  window.webkit.messageHandlers.bridge.postMessage({{action:'toggle',id:tid,extra:null}});
  applyFilters();
  if (detailTaskId===tid) {{
    var t = allTasks.find(function(x) {{ return x.id===tid; }});
    if (t) renderDetail(t);
  }}
}}

function deleteTask(tid) {{
  for (var i=0; i<allTasks.length; i++) {{
    if (allTasks[i].id===tid) {{ allTasks.splice(i,1); break; }}
  }}
  window.webkit.messageHandlers.bridge.postMessage({{action:'delete',id:tid,extra:null}});
  applyFilters();
}}

function toggleToday(tid) {{
  for (var i=0; i<allTasks.length; i++) {{
    if (allTasks[i].id===tid) {{ allTasks[i].today = !allTasks[i].today; break; }}
  }}
  window.webkit.messageHandlers.bridge.postMessage({{action:'toggle-today',id:tid,extra:null}});
  applyFilters();
}}

function generateId() {{
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {{
    var r = Math.random()*16|0, v = c==='x' ? r : (r&0x3|0x8);
    return v.toString(16);
  }});
}}

function submitAdd() {{
  var text   = document.getElementById('f-text').value.trim();
  var cat    = document.getElementById('f-cat-add').value;
  var proj   = document.getElementById('f-proj-add').value.trim();
  var status = document.getElementById('f-status-add').value;
  if (!text) return;
  var newId = generateId();
  var newTask = {{
    id: newId, text: text, done: false, group: 'today',
    fromSync: false, source: null, sourceId: newId,
    addedAt: new Date().toISOString(),
    category: cat, project: proj, status: status,
    today: currentTab === 'today', notes: []
  }};
  allTasks.unshift(newTask);
  go('add', newId, {{text:text, category:cat, project:proj, status:status, today:newTask.today}});
  document.getElementById('f-text').value='';
  document.getElementById('f-proj-add').value='';
  document.getElementById('f-cat-add').value='';
  document.getElementById('f-status-add').value='todo';
  buildProjectFilter();
  applyFilters();
}}

function go(action,id,extra) {{
  window.webkit.messageHandlers.bridge.postMessage({{action:action,id:id||null,extra:extra||null}});
}}

/* ── Drag-and-drop reorder ────────────────────────────────────────────── */
var _dragTid = null;
var _dropTid = null;
var _dropPos  = null;  // 'before' | 'after'
var _dropCat  = null;  // section key when hovering a header

function onDragStart(e, tid) {{
  _dragTid = tid;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', tid);
  setTimeout(function() {{
    var el = document.querySelector('.task[data-tid="'+tid+'"]');
    if (el) el.classList.add('dragging');
  }}, 0);
}}

function _clearDrop() {{
  document.querySelectorAll('.drop-before,.drop-after').forEach(function(el) {{
    el.classList.remove('drop-before','drop-after');
  }});
  document.querySelectorAll('.sec-hdr.drop-target').forEach(function(el) {{
    el.classList.remove('drop-target');
  }});
}}

function onDragOver(e, tid) {{
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  if (tid === _dragTid) return;
  _clearDrop();
  _dropTid = tid; _dropCat = null;
  var el = document.querySelector('.task[data-tid="'+tid+'"]');
  if (!el) return;
  var rect = el.getBoundingClientRect();
  _dropPos = (e.clientY < rect.top + rect.height / 2) ? 'before' : 'after';
  el.classList.add(_dropPos === 'before' ? 'drop-before' : 'drop-after');
}}

function onDragOverHeader(e, cat) {{
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  _clearDrop();
  _dropTid = null; _dropCat = cat; _dropPos = 'top';
  var el = document.querySelector('.sec-hdr[data-cat="'+cat+'"]');
  if (el) el.classList.add('drop-target');
}}

function onDrop(e, tid)       {{ e.preventDefault(); if (_dragTid) _executeDrop(); }}
function onDropHeader(e, cat) {{ e.preventDefault(); if (_dragTid) _executeDrop(); }}

function onDragEnd(e) {{
  var el = document.querySelector('.task[data-tid="'+(_dragTid||'')+'"]');
  if (el) el.classList.remove('dragging');
  _clearDrop();
  _dragTid = null; _dropTid = null; _dropPos = null; _dropCat = null;
}}

function _executeDrop() {{
  var tid = _dragTid;
  var srcIdx = -1;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === tid) {{ srcIdx = i; break; }}
  }}
  if (srcIdx === -1) return;

  var task = allTasks.splice(srcIdx, 1)[0];
  var catChange = null;

  if (_dropCat !== null) {{
    // Dropped on section header — insert at top of that section
    if ((task.category || '') !== _dropCat) {{
      catChange = {{ tid: tid, cat: _dropCat }};
      task.category = _dropCat;
    }}
    var insertAt = allTasks.length;
    for (var i = 0; i < allTasks.length; i++) {{
      if ((allTasks[i].category || '') === _dropCat) {{ insertAt = i; break; }}
    }}
    allTasks.splice(insertAt, 0, task);
  }} else if (_dropTid) {{
    var targetIdx = -1;
    for (var i = 0; i < allTasks.length; i++) {{
      if (allTasks[i].id === _dropTid) {{ targetIdx = i; break; }}
    }}
    if (targetIdx === -1) {{
      allTasks.push(task);
    }} else {{
      var newCat = allTasks[targetIdx].category || '';
      if ((task.category || '') !== newCat) {{
        catChange = {{ tid: tid, cat: newCat }};
        task.category = newCat;
      }}
      allTasks.splice(_dropPos === 'before' ? targetIdx : targetIdx + 1, 0, task);
    }}
  }} else {{
    allTasks.push(task);
  }}

  _clearDrop();
  applyFilters();
  var orderIds = allTasks.map(function(t) {{ return t.id; }});
  var payload = {{ order: orderIds }};
  if (catChange) payload.cat = catChange;
  go('reorder', null, payload);
}}

function startSync() {{
  var btn = document.getElementById('sync-now-btn');
  if (btn) {{ btn.textContent = '\u21BB Syncing\u2026'; btn.disabled = true; btn.style.opacity = '0.6'; }}
  go('sync');
}}

/* ── Detail panel ── */
function openDetail(tid) {{
  var t = null;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === tid) {{ t = allTasks[i]; break; }}
  }}
  if (!t) return;
  detailTaskId = tid;
  renderDetail(t);
  document.getElementById('view-main').classList.add('slide-out');
  document.getElementById('view-detail').classList.add('slide-in');
}}

function closeDetail() {{
  detailTaskId = null;
  document.getElementById('view-main').classList.remove('slide-out');
  document.getElementById('view-detail').classList.remove('slide-in');
}}

function renderDetail(t) {{
  var tid    = t.id;
  var stVal  = t.status || (t.done ? 'done' : 'todo');
  var catVal = t.category || '';

  var titleEl = document.getElementById('dp-title');
  titleEl.value = t.text;
  titleEl.style.height = 'auto';
  titleEl.style.height = titleEl.scrollHeight + 'px';

  var descEl = document.getElementById('dp-desc');
  descEl.value = t.description || '';

  var starBtn = document.getElementById('dp-star-btn');
  starBtn.innerHTML = t.today ? '&#9733;' : '&#9734;';
  starBtn.classList.toggle('dp-starred', !!t.today);

  document.getElementById('dp-status-wrap').innerHTML =
    makeSelect(tid, 'status', STATUS_OPTS, stVal, stSelCls(stVal), 'di-');
  document.getElementById('dp-cat-wrap').innerHTML =
    makeSelect(tid, 'category', CAT_OPTS, catVal, catSelCls(catVal), 'di-');

  var addedEl = document.getElementById('dp-added-at');
  if (t.addedAt) {{
    var d  = new Date(t.addedAt);
    var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
    var hr = d.getHours(), mn = d.getMinutes();
    var ampm = hr >= 12 ? 'PM' : 'AM';
    hr = hr % 12 || 12;
    addedEl.textContent = 'Added ' + mo + ' ' + d.getDate() + ', ' + d.getFullYear() +
      ' at ' + hr + ':' + (mn < 10 ? '0' : '') + mn + ' ' + ampm;
  }} else {{
    addedEl.textContent = '';
  }}

  var notesList = document.getElementById('dp-notes-list');
  var notes = (t.notes || []);
  if (!notes.length) {{
    notesList.innerHTML = '<p class="dp-no-notes">No notes yet.</p>';
  }} else {{
    var html = '';
    for (var ni = notes.length - 1; ni >= 0; ni--) {{
      var n = notes[ni];
      if (n.system) {{
        html += '<div class="dp-note-item system">' +
          '<div class="dp-note-ts">' + esc(n.timestamp) + '</div>' +
          '<div class="dp-note-text">' + esc(n.text) + '</div>' +
          '</div>';
      }} else {{
        html += '<div class="dp-note-item">' +
          '<div class="dp-note-ts">' + esc(n.timestamp) + '</div>' +
          '<div class="dp-note-text">' + linkify(n.text) + '</div>' +
          '<button class="dp-note-del" onclick="dpDeleteNote(' + ni + ')" title="Delete note">&#x2715;</button>' +
          '</div>';
      }}
    }}
    notesList.innerHTML = html;
  }}
}}

function dpToggleStar() {{
  if (!detailTaskId) return;
  toggleToday(detailTaskId);
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === detailTaskId) {{
      var starBtn = document.getElementById('dp-star-btn');
      starBtn.innerHTML = allTasks[i].today ? '&#9733;' : '&#9734;';
      starBtn.classList.toggle('dp-starred', !!allTasks[i].today);
      break;
    }}
  }}
}}

function dpSaveTitle(val) {{
  val = val.trim();
  if (!val || !detailTaskId) return;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === detailTaskId) {{
      allTasks[i].text = val;
      break;
    }}
  }}
  go('update', detailTaskId, {{text: val}});
  applyFilters();
}}

function dpSaveDesc(val) {{
  if (!detailTaskId) return;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === detailTaskId) {{
      allTasks[i].description = val;
      break;
    }}
  }}
  go('update', detailTaskId, {{description: val}});
}}

function dpDelete() {{
  if (!detailTaskId) return;
  deleteTask(detailTaskId);
  closeDetail();
}}

function dpSubmitNote() {{
  var inp  = document.getElementById('dp-note-input');
  var text = inp.value.trim();
  if (!text || !detailTaskId) return;
  var now  = new Date();
  var mo   = now.getMonth() + 1;
  var day  = now.getDate();
  var yr   = now.getFullYear();
  var h    = now.getHours(), m = now.getMinutes();
  var ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  var ts = mo + '/' + day + '/' + yr + ' ' + h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === detailTaskId) {{
      if (!allTasks[i].notes) allTasks[i].notes = [];
      allTasks[i].notes.push({{ text: text, timestamp: ts }});
      renderDetail(allTasks[i]);
      break;
    }}
  }}
  go('add-note', detailTaskId, {{ text: text, timestamp: ts }});
  inp.value = '';
  inp.focus();
}}

function dpDeleteNote(noteIndex) {{
  if (!detailTaskId) return;
  for (var i = 0; i < allTasks.length; i++) {{
    if (allTasks[i].id === detailTaskId) {{
      if (allTasks[i].notes && noteIndex >= 0 && noteIndex < allTasks[i].notes.length) {{
        allTasks[i].notes.splice(noteIndex, 1);
        renderDetail(allTasks[i]);
        go('delete-note', detailTaskId, {{ index: noteIndex }});
      }}
      break;
    }}
  }}
}}

document.getElementById('dp-note-input').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); dpSubmitNote(); }}
}});

buildProjectFilter();
applyFilters();

(function() {{
  var n = {new_task_count};
  if (n > 0) {{
    var toast = document.getElementById('sync-toast');
    document.getElementById('sync-toast-msg').textContent =
      n === 1 ? '1 new task synced' : n + ' new tasks synced';
    toast.classList.add('show');
    setTimeout(function() {{ toast.classList.remove('show'); }}, 5000);
  }}
}})();
</script>
</body></html>"""

# ── WKScriptMessageHandler ────────────────────────────────────────────────────
if HAS_WEBKIT:
    class BridgeHandler(AppKit.NSObject):
        def initWithApp_(self, app):
            self = objc.super(BridgeHandler, self).init()
            if self is None: return None
            self._app = app
            return self

        def userContentController_didReceiveScriptMessage_(self, controller, message):
            raw    = message.body()
            action = raw.get("action") if raw else None
            tid    = raw.get("id")    if raw else None
            extra  = raw.get("extra") if raw else None
            app    = self._app

            # guard against NSNull
            if tid and type(tid).__name__ == "NSNull":
                tid = None
            if extra and type(extra).__name__ == "NSNull":
                extra = None

            if action == "toggle" and tid:
                for t in app.data["tasks"]:
                    if t["id"] == tid:
                        t["done"] = not t["done"]
                        if t["done"]:
                            t["status"] = "done"
                            t["completedAt"] = datetime.now(timezone.utc).isoformat()
                        else:
                            if t.get("status") == "done":
                                t["status"] = "todo"
                            t["completedAt"] = None
                        break
                save_data(app.data)
                app._update_badge()  # JS handles re-render, no page reload

            elif action == "delete" and tid:
                # Record sourceId so sync never re-adds this task
                for t in app.data["tasks"]:
                    if t["id"] == tid and t.get("sourceId"):
                        if "deletedIds" not in app.data:
                            app.data["deletedIds"] = []
                        if t["sourceId"] not in app.data["deletedIds"]:
                            app.data["deletedIds"].append(t["sourceId"])
                        break
                app.data["tasks"] = [t for t in app.data["tasks"] if t["id"] != tid]
                save_data(app.data)
                app._update_badge()  # JS handles re-render, no page reload

            elif action == "add" and extra:
                text     = (extra.get("text") or "").strip()
                group    = extra.get("group") or "today"
                category = (extra.get("category") or "").strip()
                project  = (extra.get("project") or "").strip()
                status   = extra.get("status") or "todo"
                today    = bool(extra.get("today", False))
                task_id  = tid  # JS generates and passes the UUID
                if text:
                    app.data["tasks"].append(make_task(
                        text, group=group, category=category,
                        project=project, status=status,
                        source_id=task_id, today=today,
                    ))
                    save_data(app.data)
                    app._update_badge()  # JS handles re-render, no page reload

            elif action == "update" and tid and extra:
                # Inline triage edit — save to disk, no full page reload
                for t in app.data["tasks"]:
                    if t["id"] == tid:
                        for field in ("category", "project", "status", "text", "description"):
                            val = extra.get(field)
                            if val is not None:
                                t[field] = val
                        if extra.get("status") == "done":
                            if not t.get("done"):
                                t["completedAt"] = datetime.now(timezone.utc).isoformat()
                            t["done"] = True
                        elif "status" in (extra.keys() if hasattr(extra, "keys") else {}):
                            if extra.get("status") != "done":
                                t["done"] = False
                                t["completedAt"] = None
                        break
                save_data(app.data)
                app._update_badge()

            elif action == "toggle-today" and tid:
                for t in app.data["tasks"]:
                    if t["id"] == tid:
                        t["today"] = not t.get("today", False)
                        break
                save_data(app.data)
                app._update_badge()

            elif action == "close":
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: app.panel.orderOut_(None)
                )

            elif action == "add-note" and tid and extra:
                note_text = (extra.get("text") or "").strip()
                note_ts   = (extra.get("timestamp") or "").strip()
                if note_text:
                    for t in app.data["tasks"]:
                        if t["id"] == tid:
                            if not isinstance(t.get("notes"), list):
                                t["notes"] = []
                            note = {"text": note_text, "timestamp": note_ts}
                            if extra.get("system"):
                                note["system"] = True
                            t["notes"].append(note)
                            break
                    save_data(app.data)

            elif action == "delete-note" and tid and extra is not None:
                note_index = extra.get("index")
                if isinstance(note_index, (int, float)):
                    note_index = int(note_index)
                    for t in app.data["tasks"]:
                        if t["id"] == tid:
                            notes = t.get("notes") or []
                            if 0 <= note_index < len(notes):
                                notes.pop(note_index)
                            break
                    save_data(app.data)

            elif action == "reorder" and extra:
                new_order  = extra.get("order") or []
                cat_change = extra.get("cat")
                if cat_change:
                    cat_tid = cat_change.get("tid")
                    cat_val = cat_change.get("cat", "")
                    for t in app.data["tasks"]:
                        if t["id"] == cat_tid:
                            t["category"] = cat_val
                            break
                if new_order:
                    id_to_task = {t["id"]: t for t in app.data["tasks"]}
                    reordered  = [id_to_task[i] for i in new_order if i in id_to_task]
                    seen       = {t["id"] for t in reordered}
                    reordered += [t for t in app.data["tasks"] if t["id"] not in seen]
                    app.data["tasks"] = reordered
                save_data(app.data)

            elif action == "open-url" and extra:
                url_str = (extra.get("url") or "").strip()
                if url_str.startswith(("http://", "https://")):
                    ns_url = AppKit.NSURL.URLWithString_(url_str)
                    if ns_url:
                        AppKit.NSWorkspace.sharedWorkspace().openURL_(ns_url)

            elif action == "sync":
                threading.Thread(target=app._do_sync, daemon=True).start()

            elif action == "clear_done":
                app.data["tasks"] = [t for t in app.data["tasks"] if not t["done"]]
                save_data(app.data)
                app._refresh(); app._update_badge()

# ── Main app delegate ─────────────────────────────────────────────────────────
class KeyablePanel(AppKit.NSPanel):
    """Borderless panel that becomes key on click so Cmd+C/V/X reach the WKWebView."""
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def sendEvent_(self, event):
        # NSNonactivatingPanel never activates the app; we do it on every click
        # so that Cmd+C/V/X reach the WKWebView as normal key equivalents.
        if event.type() == AppKit.NSEventTypeLeftMouseDown:
            if not self.isKeyWindow():
                self.makeKeyWindow()
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        AppKit.NSPanel.sendEvent_(self, event)

class TodoBarApp(AppKit.NSObject):

    def init(self):
        self = objc.super(TodoBarApp, self).init()
        if self is None: return None
        self.data        = load_data()
        self.status_item = None
        self.panel       = None
        self.webview     = None
        self._handler    = None
        self._sync_timer = None
        return self

    def run(self):
        acquire_instance()
        app = AppKit.NSApplication.sharedApplication()
        app.setDelegate_(self)
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        app.run()
        release_instance()

    def applicationWillTerminate_(self, notif):
        release_instance()

    def applicationDidFinishLaunching_(self, notif):
        self._setup_status_item()
        self._setup_panel()
        self._setup_drag_monitor()
        threading.Thread(target=self._do_sync, daemon=True).start()
        self._schedule_sync()

    def _setup_drag_monitor(self):
        """Use NSEvent local monitors to drag the borderless panel by its header."""
        HEADER_H = 68  # pixels from top of panel content area
        self._dragging        = False
        self._drag_start_mouse  = None
        self._drag_start_origin = None

        panel = self.panel

        def on_down(event):
            try:
                if event.window() is panel:
                    loc = event.locationInWindow()
                    if loc.y > panel.frame().size.height - HEADER_H:
                        ml = AppKit.NSEvent.mouseLocation()
                        self._drag_start_mouse  = (ml.x, ml.y)
                        self._drag_start_origin = (panel.frame().origin.x,
                                                   panel.frame().origin.y)
                        self._dragging = True
            except Exception:
                pass
            return event

        def on_drag(event):
            try:
                if self._dragging:
                    ml = AppKit.NSEvent.mouseLocation()
                    dx = ml.x - self._drag_start_mouse[0]
                    panel.setFrameOrigin_((
                        self._drag_start_origin[0] + dx,
                        self._drag_start_origin[1],  # lock Y — keep full screen height
                    ))
            except Exception:
                pass
            return event

        def on_up(event):
            self._dragging = False
            try:
                screen = panel.screen()
                if screen:
                    vf = screen.visibleFrame()
                    w  = PANEL_WIDTH
                    h  = int(vf.size.height)
                    x  = int(vf.origin.x + vf.size.width) - w
                    y  = int(vf.origin.y)
                    panel.setFrame_display_(NSMakeRect(x, y, w, h), True)
            except Exception:
                pass
            return event

        # NSEventMask values: LeftMouseDown=2, LeftMouseUp=4, LeftMouseDragged=64
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(2,  on_down)
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(64, on_drag)
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(4,  on_up)

    # ── Status bar ────────────────────────────────────────────────────────────
    def _make_menu_icon(self):
        """Clipboard icon: navy body, clip tab, pink/yellow/green lines."""
        size = AppKit.NSMakeSize(22, 22)
        img  = AppKit.NSImage.alloc().initWithSize_(size)
        img.lockFocus()

        navy   = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.231, 0.282, 0.471, 1.0)
        pink   = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.910, 0.294, 0.424, 1.0)
        yellow = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.949, 0.690, 0.141, 1.0)
        green  = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.357, 0.741, 0.349, 1.0)
        white  = AppKit.NSColor.whiteColor()

        # Clipboard body
        navy.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(2, 1, 18, 19), 2.5, 2.5
        ).fill()

        # Clip tab cutout on top
        white.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(7, 18, 8, 3), 1.5, 1.5
        ).fill()
        navy.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(8.5, 18.2, 5, 2.2), 1.0, 1.0
        ).fill()

        # Lines: pink (full), yellow (3/4), green (1/2)
        r = 1.2
        for col, w, y in [(pink, 12, 13), (yellow, 8, 9), (green, 5, 5)]:
            col.setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                AppKit.NSMakeRect(5, y, w, 2.5), r, r
            ).fill()

        img.unlockFocus()
        img.setTemplate_(False)
        return img

    def _setup_status_item(self):
        sb               = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = sb.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        btn              = self.status_item.button()
        btn.setImage_(self._make_menu_icon())
        btn.setTarget_(self)
        btn.setAction_("togglePanel:")

    def togglePanel_(self, sender):
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        else:
            self.panel.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)

    def _update_badge(self):
        pass  # count removed — icon is static

    # ── Panel ─────────────────────────────────────────────────────────────────
    def _setup_panel(self):
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        w  = PANEL_WIDTH
        h  = int(vf.size.height)
        x  = int(vf.origin.x + vf.size.width) - w   # flush to right edge
        y  = int(vf.origin.y)                        # bottom of visible frame

        # Style mask 0 = borderless, activating panel — app gains focus on click so
        # Cmd+V/C/X reach the WKWebView correctly.  NSNonactivatingPanel (128) prevented
        # app activation even when we called activateIgnoringOtherApps_, breaking paste.
        self.panel = KeyablePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h),
            0,     # NSWindowStyleMaskBorderless — no title bar, activates on click
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.panel.setMovable_(True)
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        # CanJoinAllSpaces | Stationary | FullScreenAuxiliary
        self.panel.setCollectionBehavior_(1 | 16 | 256)
        # Stay visible when the user switches to another app
        self.panel.setHidesOnDeactivate_(False)

        if HAS_WEBKIT:
            self._setup_webview()
        else:
            # Fallback: plain text label telling user to install WebKit
            lbl = AppKit.NSTextField.alloc().initWithFrame_(
                self.panel.contentView().bounds()
            )
            lbl.setStringValue_(
                "WebKit not available.\n\nRun:\n  pip3 install pyobjc-framework-WebKit\n\nthen restart the app."
            )
            lbl.setEditable_(False)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
            self.panel.contentView().addSubview_(lbl)

    def _setup_webview(self):
        config = WebKit.WKWebViewConfiguration.alloc().init()
        ctrl   = config.userContentController()
        self._handler = BridgeHandler.alloc().initWithApp_(self)
        ctrl.addScriptMessageHandler_name_(self._handler, "bridge")

        frame = self.panel.contentView().bounds()

        wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, config)
        wv.setAutoresizingMask_(2 | 16)
        self.panel.contentView().addSubview_(wv)
        self.webview = wv

        # Clip everything to rounded corners at the OS level
        cv = self.panel.contentView()
        cv.setWantsLayer_(True)
        cv.layer().setCornerRadius_(14.0)
        cv.layer().setMasksToBounds_(True)

        self._load_html()

    def _load_html(self):
        if not self.webview:
            return
        count = getattr(self, '_new_task_count', 0)
        self._new_task_count = 0
        html = render_html(self.data, new_task_count=count)
        self.webview.loadHTMLString_baseURL_(html, None)

    def _refresh(self):
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(self._load_html)

    # ── Gmail + Docs sync ─────────────────────────────────────────────────────
    def _do_sync(self):
        # Reload from disk first so we work with the freshest data
        self.data = load_data()
        last_sync      = self.data.get("lastSync")
        known_docs     = self.data.get("_known_docs") or None

        new_tasks, err = run_gmail_sync(last_sync=last_sync)
        if err:
            print(f"Gmail sync error: {err}", flush=True)
            new_tasks = []

        doc_tasks, doc_err, updated_docs = run_docs_sync(
            known_docs_cache=known_docs, last_sync=last_sync
        )
        if doc_err:
            print(f"Docs sync error: {doc_err}", flush=True)
        else:
            new_tasks.extend(doc_tasks)
            if updated_docs:
                self.data["_known_docs"] = updated_docs

        existing_ids = {t["sourceId"] for t in self.data["tasks"] if t.get("sourceId")}
        deleted_ids  = set(self.data.get("deletedIds") or [])
        added = 0
        for t in new_tasks:
            if t.get("sourceId") and t["sourceId"] not in existing_ids and t["sourceId"] not in deleted_ids:
                self.data["tasks"].append(t)
                added += 1
        self.data["lastSync"] = datetime.now(timezone.utc).isoformat()
        save_data(self.data)
        self._new_task_count = added
        self._refresh()
        self._update_badge()

    def _do_drive_auth(self):
        """Run OAuth flow on background thread; browser opens automatically."""
        try:
            run_drive_oauth()
            print("Drive auth complete — running docs sync", flush=True)
            self._do_sync()
        except Exception as e:
            print(f"Drive auth error: {e}", flush=True)
        self._refresh()

    def _schedule_sync(self):
        self._sync_timer = threading.Timer(SYNC_INTERVAL_SEC, self._auto_sync)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _auto_sync(self):
        self._do_sync()
        self._schedule_sync()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not HAS_WEBKIT:
        print("WebKit not found — install it for the full UI:")
        print("  pip3 install pyobjc-framework-WebKit")
        print("Continuing with fallback text panel...\n")

    try:
        import googleapiclient  # noqa
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                               "google-api-python-client", "google-auth-httplib2",
                               "google-auth-oauthlib"])

    TodoBarApp.alloc().init().run()
