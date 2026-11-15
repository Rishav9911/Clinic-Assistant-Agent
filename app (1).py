"""Clinic No-Show Reducer - version 1.

Upload tomorrow's appointments as a CSV, call every patient through a Bolna
agent, and see the outcome of each call on one page.

Run:  python app.py   then open http://localhost:8000
"""
import asyncio
import csv
import html
import io
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import phonenumbers
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

API_KEY = os.getenv("BOLNA_API_KEY", "")
AGENT_ID = os.getenv("BOLNA_AGENT_ID", "")
BASE_URL = os.getenv("BOLNA_BASE_URL", "https://api.bolna.ai")
CLINIC_NAME = os.getenv("CLINIC_NAME", "Sunrise Clinic")
WINDOW_START = int(os.getenv("CALL_WINDOW_START", "10"))
WINDOW_END = int(os.getenv("CALL_WINDOW_END", "20"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))      # first call + 2 retries
RETRY_MINUTES = float(os.getenv("RETRY_MINUTES", "45"))  # wait between tries
DB_PATH = os.getenv("DB_PATH", str(HERE / "clinic.db"))
IST = ZoneInfo("Asia/Kolkata")

REQUIRED = ["patient_name", "phone", "doctor_name", "appointment_date", "appointment_time"]
OUTCOMES = ["confirmed", "cancelled", "needs_human", "unreachable", "do_not_call"]
# Bolna statuses that mean the call is over and nobody had a conversation.
NOT_REACHED = {"no-answer", "busy", "failed", "canceled", "cancelled", "stopped", "error", "balance-low"}


# ---------- database ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT, phone TEXT, doctor_name TEXT,
                appointment_date TEXT, appointment_time TEXT, preferred_language TEXT,
                status TEXT DEFAULT 'pending',      -- pending, calling, retry_wait, or one of OUTCOMES
                answered_by TEXT, callback_note TEXT, summary TEXT,
                execution_id TEXT UNIQUE, call_status TEXT,
                attempts INTEGER DEFAULT 0, last_call_at TEXT,
                duration REAL, transcript TEXT, recording_url TEXT, error TEXT,
                created_at TEXT,
                UNIQUE (phone, appointment_date, appointment_time)
            );
            CREATE TABLE IF NOT EXISTS do_not_call (phone TEXT PRIMARY KEY, added_at TEXT);
            """
        )
        have = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
        if "next_call_at" not in have:  # added in version 2
            c.execute("ALTER TABLE appointments ADD COLUMN next_call_at TEXT")


# ---------- helpers ----------

def now():
    return datetime.now(IST)


def in_calling_window(when=None):
    return WINDOW_START <= (when or now()).hour < WINDOW_END


def to_e164(raw):
    """Turn a phone number as typed into +91... form, or return None if invalid."""
    try:
        n = phonenumbers.parse(str(raw).strip(), "IN")
        if phonenumbers.is_valid_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


def spoken_date(s):
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d")
        return f"{d.strftime('%A')}, {d.day} {d.strftime('%B')}"
    except ValueError:
        return s


def spoken_time(s):
    try:
        t = datetime.strptime(s.strip(), "%H:%M")
    except ValueError:
        return s
    part = "in the morning" if t.hour < 12 else "in the afternoon" if t.hour < 17 else "in the evening"
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {part}" if t.minute else f"{hour} o'clock {part}"


def find_field(data, name):
    """Find an extraction by name anywhere inside Bolna's extracted_data."""
    want = name.lower().replace(" ", "_")
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower().replace(" ", "_") == want:
                if isinstance(value, dict):
                    return value.get("objective") or value.get("subjective")
                return value
        for value in data.values():
            found = find_field(value, name)
            if found is not None:
                return found
    return None


# ---------- Bolna ----------

def headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def place_call(client, appt):
    body = {
        "agent_id": AGENT_ID,
        "recipient_phone_number": appt["phone"],
        "user_data": {
            "clinic_name": CLINIC_NAME,
            "patient_name": appt["patient_name"],
            "doctor_name": appt["doctor_name"],
            "appointment_date": spoken_date(appt["appointment_date"]),
            "appointment_time": spoken_time(appt["appointment_time"]),
            "appointment_id": str(appt["id"]),
        },
    }
    r = await client.post(f"{BASE_URL}/call", json=body, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()["execution_id"]


async def call_one(client, appt):
    """Place one call and record that it is in progress. Errors are stored on the row."""
    try:
        execution_id = await place_call(client, appt)
        with db() as c:
            c.execute(
                "UPDATE appointments SET status='calling', execution_id=?, call_status='queued', "
                "attempts=attempts+1, last_call_at=?, next_call_at=NULL, error=NULL WHERE id=?",
                (execution_id, now().isoformat(timespec="seconds"), appt["id"]),
            )
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "") or str(e)
        with db() as c:
            c.execute("UPDATE appointments SET error=? WHERE id=?", (detail[:300], appt["id"]))


async def start_calls():
    with db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE status='pending' "
            "AND phone NOT IN (SELECT phone FROM do_not_call)"
        ).fetchall()
    async with httpx.AsyncClient() as client:
        for appt in rows:
            await call_one(client, appt)
            await asyncio.sleep(1)


async def place_due_retries():
    """Call again anyone whose waiting time is over. Give up if the calling window has closed."""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE status='retry_wait' AND next_call_at <= ?",
            (now().isoformat(timespec="seconds"),),
        ).fetchall()
        if rows and not in_calling_window():
            c.execute("UPDATE appointments SET status='unreachable', next_call_at=NULL WHERE status='retry_wait'")
            return
    if rows:
        async with httpx.AsyncClient() as client:
            for appt in rows:
                await call_one(client, appt)
                await asyncio.sleep(1)


def apply_result(appt_id, ex):
    """Write one finished (or still running) Bolna execution onto its appointment."""
    status = (ex.get("status") or "").lower()
    duration = ex.get("conversation_duration") or 0
    final = None
    if status == "completed":
        extracted = ex.get("extracted_data") or {}
        outcome = str(find_field(extracted, "outcome") or "").strip().lower()
        if ex.get("answered_by_voice_mail") or not duration:
            final = "unreachable"
        elif outcome in OUTCOMES:
            final = outcome
        else:
            final = "needs_human"  # someone answered but the result is unclear
    elif status in NOT_REACHED:
        final = "unreachable"

    with db() as c:
        if final is None:
            c.execute("UPDATE appointments SET call_status=? WHERE id=?", (status, appt_id))
            return
        extracted = ex.get("extracted_data") or {}
        telephony = ex.get("telephony_data") or {}
        next_at = None
        if final == "unreachable":
            attempts = c.execute("SELECT attempts FROM appointments WHERE id=?", (appt_id,)).fetchone()["attempts"]
            due = now() + timedelta(minutes=RETRY_MINUTES)
            if attempts < MAX_ATTEMPTS and in_calling_window(due):
                final, next_at = "retry_wait", due.isoformat(timespec="seconds")
        c.execute(
            "UPDATE appointments SET status=?, call_status=?, duration=?, transcript=?, summary=?, "
            "answered_by=?, callback_note=?, recording_url=?, error=?, next_call_at=? WHERE id=? AND status='calling'",
            (
                final, status, duration, ex.get("transcript"),
                ex.get("summary") or find_field(extracted, "call summary"),
                find_field(extracted, "answered_by"), find_field(extracted, "callback_note"),
                telephony.get("recording_url"), ex.get("error_message") or None, next_at, appt_id,
            ),
        )
        if final == "do_not_call":
            phone = c.execute("SELECT phone FROM appointments WHERE id=?", (appt_id,)).fetchone()["phone"]
            c.execute("INSERT OR IGNORE INTO do_not_call VALUES (?, ?)", (phone, now().isoformat(timespec="seconds")))


async def poll_once():
    with db() as c:
        rows = c.execute("SELECT id, execution_id FROM appointments WHERE status='calling'").fetchall()
    if not rows:
        return
    async with httpx.AsyncClient() as client:
        for row in rows:
            try:
                r = await client.get(f"{BASE_URL}/executions/{row['execution_id']}", headers=headers(), timeout=30)
                r.raise_for_status()
                apply_result(row["id"], r.json())
            except Exception:
                pass  # try again on the next round


async def poll_forever():
    while True:
        await poll_once()
        await place_due_retries()
        await asyncio.sleep(POLL_SECONDS)


# ---------- web pages ----------

@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(poll_forever())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
e = html.escape

STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f5f6f8;color:#1c2330}
main{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:22px;margin:0 0 4px} .sub{color:#5b6472;margin:0 0 18px}
.card{background:#fff;border:1px solid #e1e4ea;border-radius:10px;padding:16px;margin-bottom:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.tile{background:#fff;border:1px solid #e1e4ea;border-radius:10px;padding:12px}
.tile b{display:block;font-size:26px} .tile span{color:#5b6472;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px} th,td{text-align:left;padding:8px;border-bottom:1px solid #eceef2;vertical-align:top}
th{color:#5b6472;font-weight:600;font-size:12px;text-transform:uppercase}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:#e8eaef}
.confirmed{background:#d9f2e3;color:#12643a}.cancelled{background:#fde2e0;color:#8f1d15}
.needs_human{background:#fff0cc;color:#7a5200}.unreachable{background:#e3e6ec;color:#3d4654}
.do_not_call{background:#ead9f7;color:#5a1e8a}.calling,.retry_wait{background:#dbe9ff;color:#124a9c}
button.small{padding:4px 10px;font-size:12px;margin-top:4px}
button{background:#1d4ed8;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer}
button.grey{background:#6b7280} button:disabled{background:#b6bcc8;cursor:not-allowed}
.warn{background:#fff0cc;border:1px solid #f0d58a;border-radius:8px;padding:10px;margin-bottom:16px}
.err{color:#8f1d15;font-size:12px} .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details summary{cursor:pointer;color:#1d4ed8} pre{white-space:pre-wrap;font-size:12px;background:#f5f6f8;padding:8px;border-radius:6px}
"""


def page(body, refresh=False):
    meta = '<meta http-equiv="refresh" content="10">' if refresh else ""
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>Clinic confirmations</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(msg: str = ""):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM appointments ORDER BY CASE status WHEN 'needs_human' THEN 0 WHEN 'unreachable' THEN 1 "
            "ELSE 2 END, appointment_date, appointment_time"
        ).fetchall()
    counts = {k: 0 for k in ["pending", "calling", "retry_wait"] + OUTCOMES}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    calling = counts["calling"] > 0
    busy = calling or counts["retry_wait"] > 0

    out = [f"<h1>{e(CLINIC_NAME)}: appointment confirmations</h1>",
           f"<p class='sub'>Updated {now().strftime('%d %b, %I:%M:%S %p')}"
           + (" · refreshing every 10 seconds while calls run" if busy else "") + "</p>"]
    if not API_KEY or not AGENT_ID or "paste-your" in API_KEY + AGENT_ID:
        out.append("<div class='warn'>Bolna is not connected yet. Put your API key and agent ID in the .env file, then restart.</div>")
    if msg:
        out.append(f"<div class='warn'>{e(msg)}</div>")

    labels = {"pending": "Not called yet", "calling": "Calling now", "retry_wait": "Will retry", "confirmed": "Confirmed", "cancelled": "Cancelled",
              "needs_human": "Needs a human", "unreachable": "Unreachable", "do_not_call": "Do not call"}
    out.append("<div class='tiles'>" + "".join(
        f"<div class='tile'><b>{counts[k]}</b><span>{labels[k]}</span></div>" for k in labels) + "</div>")

    window_ok = in_calling_window()
    can_call = counts["pending"] > 0 and window_ok and not calling
    note = "" if window_ok else f"<span class='err'>Calls are only allowed {WINDOW_START}:00 to {WINDOW_END}:00 India time.</span>"
    out.append(
        "<div class='card'><div class='row'>"
        "<form method='post' action='/upload' enctype='multipart/form-data' class='row'>"
        "<input type='file' name='file' accept='.csv' required><button class='grey'>Upload CSV</button></form>"
        f"<form method='post' action='/start' onsubmit=\"return confirm('Call {counts['pending']} patient(s) now?')\">"
        f"<button {'' if can_call else 'disabled'}>Start calls ({counts['pending']})</button></form>"
        "<form method='post' action='/clear' onsubmit=\"return confirm('Remove every appointment from this list?')\">"
        f"<button class='grey'>Clear list</button></form>{note}</div></div>"
    )

    if rows:
        out.append("<div class='card'><table><tr><th>Time</th><th>Patient</th><th>Doctor</th><th>Outcome</th>"
                   "<th>Notes</th><th>Tries</th><th>Last call</th></tr>")
        for r in rows:
            notes = []
            if r["answered_by"] == "other":
                notes.append("Answered by someone else.")
            if r["callback_note"] and str(r["callback_note"]).strip().lower() != "none":
                notes.append(e(str(r["callback_note"])))
            if r["status"] == "calling":
                notes.append(f"Call status: {e(r['call_status'] or '')}")
            if r["status"] == "retry_wait":
                notes.append(f"No answer. Next try at {r['next_call_at'][11:16]}.")
            if r["status"] == "unreachable":
                notes.append(f"No answer after {r['attempts']} tries. Please phone this patient.")
                if window_ok:
                    notes.append(f"<form method='post' action='/retry/{r['id']}'><button class='small grey'>Call again now</button></form>")
            if r["error"]:
                notes.append(f"<span class='err'>{e(r['error'])}</span>")
            detail = ""
            if r["summary"] or r["transcript"]:
                rec = f"<p><a href='{e(r['recording_url'])}' target='_blank'>Recording</a></p>" if r["recording_url"] else ""
                detail = (f"<details><summary>Call details</summary><p>{e(r['summary'] or '')}</p>{rec}"
                          f"<pre>{e(r['transcript'] or '')}</pre></details>")
            last = r["last_call_at"][11:16] if r["last_call_at"] else ""
            out.append(
                f"<tr><td>{e(r['appointment_date'])}<br>{e(r['appointment_time'])}</td>"
                f"<td>{e(r['patient_name'])}<br><small>{e(r['phone'])}</small></td><td>{e(r['doctor_name'])}</td>"
                f"<td><span class='pill {e(r['status'])}'>{labels.get(r['status'], r['status'])}</span></td>"
                f"<td>{' '.join(notes)}{detail}</td><td>{r['attempts']}</td><td>{last}</td></tr>"
            )
        out.append("</table></div>")
    else:
        out.append("<div class='card'>No appointments yet. Upload a CSV with these columns: "
                   "patient_name, phone, doctor_name, appointment_date (YYYY-MM-DD), appointment_time (HH:MM), preferred_language.</div>")
    return page("".join(out), refresh=busy)


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip().lower() for c in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        return RedirectResponse(f"/?msg=CSV is missing these columns: {', '.join(missing)}", status_code=303)

    added, problems = 0, []
    with db() as c:
        blocked = {r["phone"] for r in c.execute("SELECT phone FROM do_not_call")}
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            phone = to_e164(row["phone"])
            empty = [k for k in REQUIRED if not row.get(k)]
            if empty:
                problems.append(f"row {i}: missing {', '.join(empty)}")
            elif not phone:
                problems.append(f"row {i}: phone number '{row['phone']}' is not valid")
            elif phone in blocked:
                problems.append(f"row {i}: {row['patient_name']} asked not to be called")
            else:
                try:
                    datetime.strptime(row["appointment_date"], "%Y-%m-%d")
                    datetime.strptime(row["appointment_time"], "%H:%M")
                except ValueError:
                    problems.append(f"row {i}: date must be YYYY-MM-DD and time HH:MM")
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO appointments (patient_name, phone, doctor_name, appointment_date, "
                    "appointment_time, preferred_language, created_at) VALUES (?,?,?,?,?,?,?)",
                    (row["patient_name"], phone, row["doctor_name"], row["appointment_date"],
                     row["appointment_time"], row.get("preferred_language", ""), now().isoformat(timespec="seconds")),
                )
                if cur.rowcount:
                    added += 1
                else:
                    problems.append(f"row {i}: already in the list")
    msg = f"Added {added} appointment(s)."
    if problems:
        msg += " Skipped: " + "; ".join(problems[:15])
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@app.post("/start")
async def start():
    if not in_calling_window():
        return RedirectResponse("/?msg=Outside the allowed calling hours. No calls were placed.", status_code=303)
    await start_calls()
    return RedirectResponse("/", status_code=303)


@app.post("/retry/{appt_id}")
async def retry(appt_id: int):
    """Receptionist asks for one more call to an unreachable patient."""
    if not in_calling_window():
        return RedirectResponse("/?msg=Outside the allowed calling hours. No call was placed.", status_code=303)
    with db() as c:
        appt = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status='unreachable' "
            "AND phone NOT IN (SELECT phone FROM do_not_call)", (appt_id,)
        ).fetchone()
    if appt:
        async with httpx.AsyncClient() as client:
            await call_one(client, appt)
    return RedirectResponse("/", status_code=303)


@app.post("/clear")
def clear():
    with db() as c:
        c.execute("DELETE FROM appointments")
    return RedirectResponse("/?msg=List cleared.", status_code=303)


if __name__ == "__main__":
    print("\nOpen this in your browser:  http://localhost:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
