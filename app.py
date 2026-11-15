"""Clinic No-Show Reducer - version 3.

Upload tomorrow's appointments as a CSV, call every patient through a Bolna
agent, retry the ones who do not answer, let patients move to a free slot,
and offer cancelled slots to the waitlist.

Run:  python app.py   then open http://localhost:8000
"""
import asyncio
import csv
import html
import io
import os
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import phonenumbers
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
AUTO_START_TIME = os.getenv("AUTO_START_TIME", "").strip()  # e.g. 17:00, empty = off
MAX_CANDIDATES = int(os.getenv("WAITLIST_CANDIDATES_PER_SLOT", "5"))
STOP_HOURS_BEFORE = float(os.getenv("WAITLIST_STOP_HOURS_BEFORE", "2"))
OFFER_TIMEOUT_MINUTES = float(os.getenv("WAITLIST_OFFER_TIMEOUT_MINUTES", "10"))
DB_PATH = os.getenv("DB_PATH", str(HERE / "clinic.db"))
IST = ZoneInfo("Asia/Kolkata")

REQUIRED = ["patient_name", "phone", "doctor_name", "appointment_date", "appointment_time"]
OUTCOMES = ["confirmed", "rescheduled", "cancelled", "needs_human", "unreachable", "do_not_call"]
MANUAL_CHOICES = ["confirmed", "cancelled", "needs_human", "unreachable"]
# Bolna statuses that mean the call is over and nobody had a conversation.
NOT_REACHED = {"no-answer", "busy", "failed", "canceled", "cancelled", "stopped", "error", "balance-low"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------- database ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_name TEXT COLLATE NOCASE, slot_date TEXT, slot_time TEXT,
                status TEXT DEFAULT 'free',          -- free, held, booked
                origin TEXT DEFAULT 'manual',        -- manual (added by staff) or freed (a patient cancelled or moved)
                held_for TEXT,                       -- 'appt:<id>' or 'wait:<id>' while a call is offering it
                backfill TEXT DEFAULT '',            -- '', running, filled, open
                tried INTEGER DEFAULT 0, filled_by TEXT, created_at TEXT,
                UNIQUE (doctor_name, slot_date, slot_time)
            );
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_name TEXT, phone TEXT, doctor_name TEXT COLLATE NOCASE,
                days TEXT DEFAULT 'any', preferred_language TEXT,
                status TEXT DEFAULT 'active',        -- active, booked, removed
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER, waitlist_id INTEGER, execution_id TEXT UNIQUE,
                status TEXT DEFAULT 'calling',       -- calling, accepted, declined, unreachable, expired
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS call_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, ref_id INTEGER, execution_id TEXT UNIQUE, started_at TEXT,
                status TEXT, duration REAL, outcome TEXT, recording_url TEXT, cost REAL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT, actor TEXT, action TEXT, record TEXT, before TEXT, after TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        have = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
        for col, kind in [("next_call_at", "TEXT"), ("moved_from", "TEXT"),
                          ("offered_slots", "TEXT"), ("source", "TEXT DEFAULT 'csv'"),
                          ("visit_notes", "TEXT"), ("urgent", "INTEGER DEFAULT 0")]:
            if col not in have:
                c.execute(f"ALTER TABLE appointments ADD COLUMN {col} {kind}")


# ---------- helpers ----------

def now():
    return datetime.now(IST)


def stamp():
    return now().isoformat(timespec="seconds")


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


def valid_date_time(d, t):
    try:
        datetime.strptime(d, "%Y-%m-%d")
        datetime.strptime(t, "%H:%M")
        return True
    except ValueError:
        return False


def slot_dt(d, t):
    return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


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


def visit_info(extracted):
    """Visit notes in the patient's own words, and whether they described something urgent."""
    notes = find_field(extracted, "visit_notes")
    notes = str(notes).strip() if notes else ""
    if notes.lower() in ("none", "null", "not asked", "n/a"):
        notes = ""
    urgent = str(find_field(extracted, "urgent") or "").strip().lower() == "yes"
    return notes, urgent


def audit(c, actor, action, record, before="", after=""):
    c.execute("INSERT INTO audit_log (at, actor, action, record, before, after) VALUES (?,?,?,?,?,?)",
              (stamp(), actor, action, record, before or "", after or ""))


def appt_label(a):
    return f"{a['patient_name']} ({a['doctor_name']}, {a['appointment_date']} {a['appointment_time']})"


# ---------- slots ----------

def free_slot(c, doctor, d, t):
    """A patient cancelled or moved: the slot becomes free and the waitlist is tried."""
    c.execute(
        "INSERT INTO slots (doctor_name, slot_date, slot_time, status, origin, backfill, created_at) "
        "VALUES (?,?,?,'free','freed','running',?) "
        "ON CONFLICT(doctor_name, slot_date, slot_time) DO UPDATE SET status='free', origin='freed', "
        "backfill='running', tried=0, held_for=NULL, filled_by=NULL",
        (doctor.strip(), d, t, stamp()),
    )
    audit(c, "system", "slot freed", f"{doctor}, {d} {t}")


def hold_slots_for(c, appt):
    """Reserve up to 3 free slots with the same doctor so this call can offer them."""
    cutoff = now() + timedelta(hours=STOP_HOURS_BEFORE)
    rows = c.execute(
        "SELECT * FROM slots WHERE status='free' AND doctor_name=? "
        "AND NOT (slot_date=? AND slot_time=?) ORDER BY slot_date, slot_time",
        (appt["doctor_name"].strip(), appt["appointment_date"], appt["appointment_time"]),
    ).fetchall()
    usable = [r for r in rows if slot_dt(r["slot_date"], r["slot_time"]) > cutoff]
    # Share the free slots fairly between everyone still to be called for this doctor,
    # so the first call does not take them all. Each slot is offered on one call at a time.
    others = c.execute(
        "SELECT COUNT(*) n FROM appointments WHERE status IN ('pending','retry_wait') AND id!=? "
        "AND doctor_name=? COLLATE NOCASE", (appt["id"], appt["doctor_name"].strip())).fetchone()["n"]
    picked = usable[:min(3, max(1, len(usable) // (others + 1)))]
    for r in picked:
        c.execute("UPDATE slots SET status='held', held_for=? WHERE id=?", (f"appt:{appt['id']}", r["id"]))
    return picked


def release_holds(c, held_for):
    c.execute("UPDATE slots SET status='free', held_for=NULL WHERE held_for=? AND status='held'", (held_for,))


# ---------- Bolna ----------

def headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def place_call(client, phone, user_data):
    body = {"agent_id": AGENT_ID, "recipient_phone_number": phone, "user_data": user_data}
    r = await client.post(f"{BASE_URL}/call", json=body, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()["execution_id"]


async def fetch_execution(client, execution_id):
    r = await client.get(f"{BASE_URL}/executions/{execution_id}", headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def log_attempt_start(c, kind, ref_id, execution_id):
    c.execute("INSERT OR IGNORE INTO call_attempts (kind, ref_id, execution_id, started_at, status) VALUES (?,?,?,?,'queued')",
              (kind, ref_id, execution_id, stamp()))


def log_attempt_end(c, ex, outcome):
    telephony = ex.get("telephony_data") or {}
    c.execute("UPDATE call_attempts SET status=?, duration=?, outcome=?, recording_url=?, cost=? WHERE execution_id=?",
              ((ex.get("status") or "").lower(), ex.get("conversation_duration") or 0, outcome,
               telephony.get("recording_url"), ex.get("total_cost"), ex.get("id")))


# ---------- confirmation calls ----------

async def call_one(client, appt):
    """Place one confirmation call and record that it is in progress."""
    with db() as c:
        offered = hold_slots_for(c, appt)
    options = " ".join(
        f"Option {i}: {spoken_date(s['slot_date'])} at {spoken_time(s['slot_time'])}."
        for i, s in enumerate(offered, start=1)
    ) or "none"
    user_data = {
        "clinic_name": CLINIC_NAME,
        "call_type": "confirmation",
        "patient_name": appt["patient_name"],
        "doctor_name": appt["doctor_name"],
        "appointment_date": spoken_date(appt["appointment_date"]),
        "appointment_time": spoken_time(appt["appointment_time"]),
        "appointment_id": str(appt["id"]),
        "preferred_language": appt["preferred_language"] or "en",
        "slot_options": options,
    }
    try:
        execution_id = await place_call(client, appt["phone"], user_data)
        with db() as c:
            c.execute(
                "UPDATE appointments SET status='calling', execution_id=?, call_status='queued', "
                "attempts=attempts+1, last_call_at=?, next_call_at=NULL, error=NULL, offered_slots=? WHERE id=?",
                (execution_id, stamp(), ",".join(str(s["id"]) for s in offered), appt["id"]),
            )
            log_attempt_start(c, "appointment", appt["id"], execution_id)
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "") or str(e)
        with db() as c:
            release_holds(c, f"appt:{appt['id']}")
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
    return len(rows)


async def place_due_retries():
    """Call again anyone whose waiting time is over. Give up if the calling window has closed."""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE status='retry_wait' AND next_call_at <= ?", (stamp(),)
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
    extracted = ex.get("extracted_data") or {}
    final = None
    if status == "completed":
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
        appt = c.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not appt or appt["status"] != "calling":
            return
        if final is None:
            c.execute("UPDATE appointments SET call_status=? WHERE id=?", (status, appt_id))
            return

        note = find_field(extracted, "callback_note")
        new_date, new_time, moved_from = appt["appointment_date"], appt["appointment_time"], appt["moved_from"]
        visit_notes, urgent = visit_info(extracted)
        visit_notes = visit_notes or appt["visit_notes"]

        if final == "rescheduled":
            # Which of the offered options did the patient pick?
            choice = str(find_field(extracted, "chosen_slot") or "").lower()
            digits = "".join(ch for ch in choice if ch.isdigit())
            ids = [int(x) for x in (appt["offered_slots"] or "").split(",") if x]
            slot = None
            if digits and 1 <= int(digits) <= len(ids):
                slot = c.execute("SELECT * FROM slots WHERE id=? AND status='held' AND held_for=?",
                                 (ids[int(digits) - 1], f"appt:{appt_id}")).fetchone()
            if slot:
                c.execute("UPDATE slots SET status='booked', held_for=NULL, filled_by=?, "
                          "backfill=CASE WHEN backfill='running' THEN 'filled' ELSE backfill END WHERE id=?",
                          (appt["patient_name"], slot["id"]))
                moved_from = f"{appt['appointment_date']} {appt['appointment_time']}"
                new_date, new_time = slot["slot_date"], slot["slot_time"]
                free_slot(c, appt["doctor_name"], appt["appointment_date"], appt["appointment_time"])
            else:
                final = "needs_human"
                note = note if note and str(note).lower() != "none" else "Asked to move, but no slot was booked. Please call back."
        elif final == "cancelled":
            free_slot(c, appt["doctor_name"], appt["appointment_date"], appt["appointment_time"])

        release_holds(c, f"appt:{appt_id}")

        if urgent and final in ("confirmed", "rescheduled", "needs_human"):
            # The appointment stands, but a person must phone this patient now.
            final = "needs_human"
            note = "URGENT: the patient described symptoms that may need attention now. Please phone them."

        next_at = None
        if final == "unreachable":
            due = now() + timedelta(minutes=RETRY_MINUTES)
            if appt["attempts"] < MAX_ATTEMPTS and in_calling_window(due):
                final, next_at = "retry_wait", due.isoformat(timespec="seconds")

        telephony = ex.get("telephony_data") or {}
        c.execute(
            "UPDATE appointments SET status=?, call_status=?, duration=?, transcript=?, summary=?, "
            "answered_by=?, callback_note=?, recording_url=?, error=?, next_call_at=?, "
            "appointment_date=?, appointment_time=?, moved_from=?, visit_notes=?, urgent=? WHERE id=?",
            (
                final, status, duration, ex.get("transcript"),
                ex.get("summary") or find_field(extracted, "call summary"),
                find_field(extracted, "answered_by"), note,
                telephony.get("recording_url"), ex.get("error_message") or None, next_at,
                new_date, new_time, moved_from, visit_notes, 1 if urgent else 0, appt_id,
            ),
        )
        log_attempt_end(c, ex, final)
        if final != "retry_wait":
            audit(c, "system", "call outcome", appt_label(appt), "calling", final)
        if final == "do_not_call":
            c.execute("INSERT OR IGNORE INTO do_not_call VALUES (?, ?)", (appt["phone"], stamp()))


async def poll_once():
    with db() as c:
        rows = c.execute("SELECT id, execution_id FROM appointments WHERE status='calling'").fetchall()
    if not rows:
        return
    async with httpx.AsyncClient() as client:
        for row in rows:
            try:
                apply_result(row["id"], await fetch_execution(client, row["execution_id"]))
            except Exception:
                pass  # try again on the next round


# ---------- waitlist backfill ----------

async def run_backfill():
    """For each freed slot, offer it to one waitlisted patient at a time."""
    with db() as c:
        slots = c.execute("SELECT * FROM slots WHERE backfill='running' AND status='free'").fetchall()
    for slot in slots:
        when = slot_dt(slot["slot_date"], slot["slot_time"])
        with db() as c:
            if now() > when - timedelta(hours=STOP_HOURS_BEFORE) or slot["tried"] >= MAX_CANDIDATES:
                c.execute("UPDATE slots SET backfill='open' WHERE id=?", (slot["id"],))
                continue
            if not in_calling_window():
                continue
            day = DAYS[when.weekday()]
            today = now().date().isoformat()
            candidates = c.execute(
                "SELECT * FROM waitlist w WHERE status='active' AND doctor_name=? "
                "AND phone NOT IN (SELECT phone FROM do_not_call) "
                "AND id NOT IN (SELECT waitlist_id FROM offers WHERE slot_id=?) "
                "AND phone NOT IN (SELECT w2.phone FROM offers o JOIN waitlist w2 ON w2.id=o.waitlist_id "
                "                  WHERE substr(o.created_at,1,10)=?) "
                "ORDER BY created_at, id",
                (slot["doctor_name"], slot["id"], today),
            ).fetchall()
            candidates = [w for w in candidates if w["days"] in ("", "any") or day in w["days"].split(",")]
            if not candidates:
                # Nobody to try right now. Keep looking only if someone was skipped for today.
                waiting = c.execute(
                    "SELECT COUNT(*) n FROM waitlist WHERE status='active' AND doctor_name=? "
                    "AND id NOT IN (SELECT waitlist_id FROM offers WHERE slot_id=?)",
                    (slot["doctor_name"], slot["id"])).fetchone()["n"]
                if not waiting:
                    c.execute("UPDATE slots SET backfill='open' WHERE id=?", (slot["id"],))
                continue
            person = candidates[0]
            c.execute("UPDATE slots SET status='held', held_for=? WHERE id=? AND status='free'",
                      (f"wait:{person['id']}", slot["id"]))

        user_data = {
            "clinic_name": CLINIC_NAME,
            "call_type": "waitlist_offer",
            "patient_name": person["patient_name"],
            "doctor_name": slot["doctor_name"],
            "appointment_date": spoken_date(slot["slot_date"]),
            "appointment_time": spoken_time(slot["slot_time"]),
            "preferred_language": person["preferred_language"] or "en",
            "slot_options": "none",
        }
        try:
            async with httpx.AsyncClient() as client:
                execution_id = await place_call(client, person["phone"], user_data)
            with db() as c:
                c.execute("INSERT INTO offers (slot_id, waitlist_id, execution_id, created_at) VALUES (?,?,?,?)",
                          (slot["id"], person["id"], execution_id, stamp()))
                log_attempt_start(c, "waitlist", person["id"], execution_id)
        except Exception:
            with db() as c:
                release_holds(c, f"wait:{person['id']}")


def finish_offer(offer, ex=None, expired=False):
    status = ((ex or {}).get("status") or "").lower()
    extracted = (ex or {}).get("extracted_data") or {}
    duration = (ex or {}).get("conversation_duration") or 0
    outcome = str(find_field(extracted, "outcome") or "").strip().lower()
    if expired:
        result = "expired"
    elif status == "completed":
        if ex.get("answered_by_voice_mail") or not duration:
            result = "unreachable"
        elif outcome == "confirmed":
            result = "accepted"
        else:
            result = "declined"
    elif status in NOT_REACHED:
        result = "unreachable"
    else:
        return  # call still running

    with db() as c:
        slot = c.execute("SELECT * FROM slots WHERE id=?", (offer["slot_id"],)).fetchone()
        person = c.execute("SELECT * FROM waitlist WHERE id=?", (offer["waitlist_id"],)).fetchone()
        c.execute("UPDATE offers SET status=? WHERE id=?", (result, offer["id"]))
        if ex:
            log_attempt_end(c, ex, result)
        held = slot and slot["status"] == "held" and slot["held_for"] == f"wait:{person['id']}"
        if result == "accepted" and held:
            c.execute("UPDATE slots SET status='booked', held_for=NULL, backfill='filled', filled_by=? WHERE id=?",
                      (person["patient_name"], slot["id"]))
            c.execute("UPDATE waitlist SET status='booked' WHERE id=?", (person["id"],))
            telephony = ex.get("telephony_data") or {}
            visit_notes, urgent = visit_info(extracted)
            # If this same phone number held (and cancelled) this exact time, replace that old row.
            c.execute("DELETE FROM appointments WHERE phone=? AND appointment_date=? AND appointment_time=? AND status='cancelled'",
                      (person["phone"], slot["slot_date"], slot["slot_time"]))
            c.execute(
                "INSERT OR IGNORE INTO appointments (patient_name, phone, doctor_name, appointment_date, appointment_time, "
                "preferred_language, status, source, summary, transcript, recording_url, duration, execution_id, "
                "call_status, attempts, last_call_at, created_at, visit_notes, urgent, callback_note) "
                "VALUES (?,?,?,?,?,?,?,'waitlist',?,?,?,?,?,?,1,?,?,?,?,?)",
                (person["patient_name"], person["phone"], slot["doctor_name"], slot["slot_date"], slot["slot_time"],
                 person["preferred_language"], "needs_human" if urgent else "confirmed",
                 ex.get("summary") or find_field(extracted, "call summary"),
                 ex.get("transcript"), telephony.get("recording_url"), duration, ex.get("id"), status,
                 offer["created_at"], stamp(), visit_notes, 1 if urgent else 0,
                 "URGENT: the patient described symptoms that may need attention now. Please phone them." if urgent else None),
            )
            audit(c, "system", "slot refilled from waitlist",
                  f"{slot['doctor_name']}, {slot['slot_date']} {slot['slot_time']}", "free", person["patient_name"])
        elif held:
            c.execute("UPDATE slots SET status='free', held_for=NULL, tried=tried+1 WHERE id=?", (slot["id"],))
            audit(c, "system", "waitlist offer " + result,
                  f"{slot['doctor_name']}, {slot['slot_date']} {slot['slot_time']}", "", person["patient_name"])
        if outcome == "do_not_call":
            c.execute("INSERT OR IGNORE INTO do_not_call VALUES (?, ?)", (person["phone"], stamp()))
            c.execute("UPDATE waitlist SET status='removed' WHERE id=?", (person["id"],))


async def poll_offers():
    with db() as c:
        rows = c.execute("SELECT * FROM offers WHERE status='calling'").fetchall()
    if not rows:
        return
    async with httpx.AsyncClient() as client:
        for offer in rows:
            try:
                ex = await fetch_execution(client, offer["execution_id"])
                age = now() - datetime.fromisoformat(offer["created_at"])
                finish_offer(offer, ex)
                if age > timedelta(minutes=OFFER_TIMEOUT_MINUTES):
                    with db() as c:
                        still = c.execute("SELECT status FROM offers WHERE id=?", (offer["id"],)).fetchone()["status"]
                    if still == "calling":
                        finish_offer(offer, expired=True)
            except Exception:
                pass


# ---------- automatic evening start ----------

async def auto_start():
    if not AUTO_START_TIME:
        return
    try:
        start_at = datetime.strptime(AUTO_START_TIME, "%H:%M").time()
    except ValueError:
        return
    today = now().date().isoformat()
    if now().time() < start_at or not in_calling_window():
        return
    with db() as c:
        if (c.execute("SELECT value FROM kv WHERE key='last_auto_start'").fetchone() or [""])[0] == today:
            return
        if not c.execute("SELECT 1 FROM appointments WHERE status='pending'").fetchone():
            return  # nothing uploaded yet; keep waiting
        # Once per day. Anything uploaded after this run needs the Start calls button.
        c.execute("INSERT OR REPLACE INTO kv VALUES ('last_auto_start', ?)", (today,))
    placed = await start_calls()
    with db() as c:
        audit(c, "system", "automatic evening start", today, "", f"{placed} call(s) placed")


async def loop_forever():
    while True:
        for step in (poll_once, poll_offers, place_due_retries, run_backfill, auto_start):
            try:
                await step()
            except Exception as e:  # one failing step must not stop the others
                print("background step failed:", step.__name__, e)
        await asyncio.sleep(POLL_SECONDS)


# ---------- web pages ----------

@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(loop_forever())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
e = html.escape

STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f5f6f8;color:#1c2330}
main{max-width:1150px;margin:0 auto;padding:20px}
nav{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
nav a{padding:7px 14px;border-radius:8px;text-decoration:none;color:#1c2330;background:#e8eaef;font-size:14px}
nav a.on{background:#1d4ed8;color:#fff}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:0 0 10px} .sub{color:#5b6472;margin:0 0 18px}
.card{background:#fff;border:1px solid #e1e4ea;border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.tile{background:#fff;border:1px solid #e1e4ea;border-radius:10px;padding:12px;text-decoration:none;color:inherit;display:block}
.tile.on{border-color:#1d4ed8;box-shadow:0 0 0 2px #c7d7fb}
.tile b{display:block;font-size:26px} .tile span{color:#5b6472;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px} th,td{text-align:left;padding:8px;border-bottom:1px solid #eceef2;vertical-align:top}
th{color:#5b6472;font-weight:600;font-size:12px;text-transform:uppercase}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:#e8eaef;white-space:nowrap}
.confirmed,.filled{background:#d9f2e3;color:#12643a}.cancelled{background:#fde2e0;color:#8f1d15}
.needs_human,.open{background:#fff0cc;color:#7a5200}.unreachable{background:#e3e6ec;color:#3d4654}
.do_not_call{background:#ead9f7;color:#5a1e8a}.calling,.retry_wait,.offering,.running{background:#dbe9ff;color:#124a9c}
.rescheduled{background:#d5f0f3;color:#0b5560}
button{background:#1d4ed8;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer}
button.grey{background:#6b7280} button:disabled{background:#b6bcc8;cursor:not-allowed}
button.small{padding:4px 10px;font-size:12px;margin-top:4px}
input,select{padding:7px;border:1px solid #cbd0d9;border-radius:6px;font-size:14px}
select.small{padding:3px;font-size:12px}
.warn{background:#fff0cc;border:1px solid #f0d58a;border-radius:8px;padding:10px;margin-bottom:16px}
.err{color:#8f1d15;font-size:12px} .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.muted{color:#5b6472;font-size:12px}
.visit{background:#f5f6f8;border-radius:6px;padding:6px 8px;margin:4px 0;white-space:pre-wrap}
@media print{nav,.noprint{display:none} body{background:#fff} .card{border:0;padding:0}}
details summary{cursor:pointer;color:#1d4ed8} pre{white-space:pre-wrap;font-size:12px;background:#f5f6f8;padding:8px;border-radius:6px}
"""

LABELS = {"pending": "Not called yet", "calling": "Calling now", "retry_wait": "Will retry", "confirmed": "Confirmed",
          "rescheduled": "Rescheduled", "cancelled": "Cancelled", "needs_human": "Needs a human",
          "unreachable": "Unreachable", "do_not_call": "Do not call"}


def page(body, active="/", refresh=False):
    meta = '<meta http-equiv="refresh" content="10">' if refresh else ""
    links = [("/", "Dashboard"), ("/sheet", "Doctor's sheet"), ("/slots", "Free slots"), ("/waitlist", "Waitlist"), ("/log", "Activity log")]
    nav = "<nav>" + "".join(f"<a href='{u}' class='{'on' if u == active else ''}'>{t}</a>" for u, t in links) + "</nav>"
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>Clinic confirmations</title><style>{STYLE}</style></head><body><main>{nav}{body}</main></body></html>"
    )


def go(path, msg=""):
    return RedirectResponse(path + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


def freed_slots_panel(c):
    rows = c.execute("SELECT * FROM slots WHERE origin='freed' ORDER BY slot_date DESC, slot_time DESC LIMIT 30").fetchall()
    if not rows:
        return ""
    out = ["<div class='card'><h2>Freed slots</h2><table><tr><th>Slot</th><th>Doctor</th><th>Status</th><th>Waitlist tries</th></tr>"]
    for s in rows:
        if s["status"] == "booked":
            pill, text = "filled", f"Refilled by {e(s['filled_by'] or 'a patient')}"
        elif s["status"] == "held" and (s["held_for"] or "").startswith("wait:"):
            w = c.execute("SELECT patient_name FROM waitlist WHERE id=?", (s["held_for"][5:],)).fetchone()
            pill, text = "offering", f"Being offered to {e(w['patient_name'] if w else 'a waitlist patient')}"
        elif s["status"] == "held":
            pill, text = "offering", "Being offered to a patient who may move"
        elif s["backfill"] == "running":
            pill, text = "running", "Looking for a waitlist patient"
        else:
            pill, text = "open", "Still open"
        out.append(f"<tr><td>{e(s['slot_date'])} {e(s['slot_time'])}</td><td>{e(s['doctor_name'])}</td>"
                   f"<td><span class='pill {pill}'>{text}</span></td><td>{s['tried']}</td></tr>")
    out.append("</table></div>")
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def dashboard(msg: str = "", status: str = "", date: str = ""):
    with db() as c:
        all_rows = c.execute(
            "SELECT * FROM appointments ORDER BY urgent DESC, CASE status WHEN 'needs_human' THEN 0 WHEN 'unreachable' THEN 1 "
            "ELSE 2 END, appointment_date, appointment_time"
        ).fetchall()
        attempts = {}
        for a in c.execute("SELECT * FROM call_attempts WHERE kind='appointment' ORDER BY id"):
            attempts.setdefault(a["ref_id"], []).append(a)
        offers_running = c.execute("SELECT COUNT(*) n FROM offers WHERE status='calling'").fetchone()["n"]
        panel = freed_slots_panel(c)

    dates = sorted({r["appointment_date"] for r in all_rows})
    in_date = [r for r in all_rows if not date or r["appointment_date"] == date]
    counts = {k: 0 for k in LABELS}
    for r in in_date:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    rows = [r for r in in_date if not status or r["status"] == status]
    all_pending = sum(1 for r in all_rows if r["status"] == "pending")
    calling = any(r["status"] == "calling" for r in all_rows)
    busy = calling or offers_running or any(r["status"] == "retry_wait" for r in all_rows)

    out = [f"<h1>{e(CLINIC_NAME)}: appointment confirmations</h1>",
           f"<p class='sub'>Updated {now().strftime('%d %b, %I:%M:%S %p')}"
           + (" · refreshing every 10 seconds while calls run" if busy else "")
           + (f" · calls start automatically at {e(AUTO_START_TIME)}" if AUTO_START_TIME else "") + "</p>"]
    if not API_KEY or not AGENT_ID or "paste-your" in API_KEY + AGENT_ID:
        out.append("<div class='warn'>Bolna is not connected yet. Put your API key and agent ID in the .env file, then restart.</div>")
    if msg:
        out.append(f"<div class='warn'>{e(msg)}</div>")

    def link(s):
        q = "&".join(p for p in [f"status={s}" if s else "", f"date={quote(date)}" if date else ""] if p)
        return "/" + (f"?{q}" if q else "")

    out.append("<div class='tiles'>" + "".join(
        f"<a class='tile {'on' if status == k else ''}' href='{link('' if status == k else k)}'>"
        f"<b>{counts[k]}</b><span>{LABELS[k]}</span></a>" for k in LABELS) + "</div>")

    window_ok = in_calling_window()
    can_call = all_pending > 0 and window_ok and not calling
    note = "" if window_ok else f"<span class='err'>Calls are only allowed {WINDOW_START}:00 to {WINDOW_END}:00 India time.</span>"
    date_opts = "<option value=''>All dates</option>" + "".join(
        f"<option {'selected' if d == date else ''}>{e(d)}</option>" for d in dates)
    out.append(
        "<div class='card'><div class='row'>"
        "<form method='post' action='/upload' enctype='multipart/form-data' class='row'>"
        "<input type='file' name='file' accept='.csv' required><button class='grey'>Upload CSV</button></form>"
        f"<form method='post' action='/start' onsubmit=\"return confirm('Call {all_pending} patient(s) now?')\">"
        f"<button {'' if can_call else 'disabled'}>Start calls ({all_pending})</button></form>"
        "<form method='post' action='/clear' onsubmit=\"return confirm('Remove every appointment from this list?')\">"
        f"<button class='grey'>Clear list</button></form>{note}</div>"
        f"<div class='row' style='margin-top:12px'><form method='get' action='/' class='row'>"
        f"<select name='date' onchange='this.form.submit()'>{date_opts}</select></form>"
        f"<a href='/export.csv{'?date=' + quote(date) if date else ''}'>Download results as CSV</a></div></div>"
    )

    if rows:
        out.append("<div class='card'><table><tr><th>Time</th><th>Patient</th><th>Doctor</th><th>Outcome</th>"
                   "<th>Notes</th><th>Tries</th><th>Last call</th><th>Change</th></tr>")
        for r in rows:
            notes = []
            if r["source"] == "waitlist":
                notes.append("Booked from the waitlist.")
            if r["moved_from"]:
                notes.append(f"Moved from {e(r['moved_from'])}.")
            if r["answered_by"] == "other":
                notes.append("Answered by someone else.")
            if r["callback_note"] and str(r["callback_note"]).strip().lower() != "none":
                text = e(str(r["callback_note"]))
                notes.append(f"<b class='err'>{text}</b>" if r["urgent"] else text)
            if r["visit_notes"]:
                notes.append(f"<div class='visit'><span class='muted'>Visit notes (patient's words)</span><br>{e(r['visit_notes'])}</div>")
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
            history = attempts.get(r["id"], [])
            if r["summary"] or r["transcript"] or history:
                rec = f"<p><a href='{e(r['recording_url'])}' target='_blank'>Recording</a></p>" if r["recording_url"] else ""
                hist = "".join(
                    f"<li>{e((a['started_at'] or '')[11:16])}: {e(a['outcome'] or a['status'] or '')}"
                    f" <span class='muted'>({e(a['execution_id'])})</span></li>" for a in history)
                hist = f"<p class='muted'>Call history</p><ol>{hist}</ol>" if hist else ""
                detail = (f"<details><summary>Call details</summary><p>{e(r['summary'] or '')}</p>{rec}{hist}"
                          f"<pre>{e(r['transcript'] or '')}</pre></details>")
            last = r["last_call_at"][11:16] if r["last_call_at"] else ""
            change = ""
            if r["status"] != "calling":
                opts = "<option value=''>Change…</option>" + "".join(
                    f"<option value='{k}'>{LABELS[k]}</option>" for k in MANUAL_CHOICES if k != r["status"])
                change = (f"<form method='post' action='/edit/{r['id']}'>"
                          f"<select class='small' name='status' onchange=\"if(this.value&&confirm('Change this outcome?'))this.form.submit();else this.value=''\">{opts}</select></form>")
            out.append(
                f"<tr><td>{e(r['appointment_date'])}<br>{e(r['appointment_time'])}</td>"
                f"<td>{e(r['patient_name'])}<br><small>{e(r['phone'])}</small></td><td>{e(r['doctor_name'])}</td>"
                f"<td><span class='pill {e(r['status'])}'>{LABELS.get(r['status'], r['status'])}</span></td>"
                f"<td>{' '.join(notes)}{detail}</td><td>{r['attempts']}</td><td>{last}</td><td>{change}</td></tr>"
            )
        out.append("</table></div>")
    elif all_rows:
        out.append("<div class='card'>No appointments match this filter.</div>")
    else:
        out.append("<div class='card'>No appointments yet. Upload a CSV with these columns: "
                   "patient_name, phone, doctor_name, appointment_date (YYYY-MM-DD), appointment_time (HH:MM), preferred_language.</div>")
    out.append(panel)
    return page("".join(out), "/", refresh=bool(busy))


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip().lower() for c in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        return go("/", f"CSV is missing these columns: {', '.join(missing)}")

    added, problems = 0, []
    with db() as c:
        blocked = {r["phone"] for r in c.execute("SELECT phone FROM do_not_call")}
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            phone = to_e164(row.get("phone", ""))
            empty = [k for k in REQUIRED if not row.get(k)]
            if empty:
                problems.append(f"row {i}: missing {', '.join(empty)}")
            elif not phone:
                problems.append(f"row {i}: phone number '{row['phone']}' is not valid")
            elif phone in blocked:
                problems.append(f"row {i}: {row['patient_name']} asked not to be called")
            elif not valid_date_time(row["appointment_date"], row["appointment_time"]):
                problems.append(f"row {i}: date must be YYYY-MM-DD and time HH:MM")
            else:
                cur = c.execute(
                    "INSERT OR IGNORE INTO appointments (patient_name, phone, doctor_name, appointment_date, "
                    "appointment_time, preferred_language, created_at) VALUES (?,?,?,?,?,?,?)",
                    (row["patient_name"], phone, row["doctor_name"], row["appointment_date"],
                     row["appointment_time"], row.get("preferred_language", ""), stamp()),
                )
                if cur.rowcount:
                    added += 1
                    # This time is now taken, so it must not be offered to anyone else.
                    c.execute("UPDATE slots SET status='booked', backfill='', filled_by=? WHERE doctor_name=? "
                              "AND slot_date=? AND slot_time=? AND status='free'",
                              (row["patient_name"], row["doctor_name"], row["appointment_date"], row["appointment_time"]))
                else:
                    problems.append(f"row {i}: already in the list")
        if added:
            audit(c, "receptionist", "uploaded CSV", file.filename or "", "", f"{added} appointment(s) added")
    msg = f"Added {added} appointment(s)."
    if problems:
        msg += " Skipped: " + "; ".join(problems[:15])
    return go("/", msg)


@app.post("/start")
async def start():
    if not in_calling_window():
        return go("/", "Outside the allowed calling hours. No calls were placed.")
    await start_calls()
    return go("/")


@app.post("/retry/{appt_id}")
async def retry(appt_id: int):
    """Receptionist asks for one more call to an unreachable patient."""
    if not in_calling_window():
        return go("/", "Outside the allowed calling hours. No call was placed.")
    with db() as c:
        appt = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status='unreachable' "
            "AND phone NOT IN (SELECT phone FROM do_not_call)", (appt_id,)
        ).fetchone()
        if appt:
            audit(c, "receptionist", "asked for another call", appt_label(appt))
    if appt:
        async with httpx.AsyncClient() as client:
            await call_one(client, appt)
    return go("/")


@app.post("/edit/{appt_id}")
def edit(appt_id: int, status: str = Form(...)):
    """Receptionist changes an outcome by hand. Every change is written to the activity log."""
    if status not in MANUAL_CHOICES:
        return go("/")
    msg = ""
    with db() as c:
        appt = c.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not appt or appt["status"] in ("calling", status):
            return go("/")
        if status == "cancelled":
            free_slot(c, appt["doctor_name"], appt["appointment_date"], appt["appointment_time"])
            msg = "Marked cancelled. The slot is free and will be offered to the waitlist."
        elif appt["status"] == "cancelled":
            slot = c.execute("SELECT * FROM slots WHERE doctor_name=? AND slot_date=? AND slot_time=?",
                             (appt["doctor_name"], appt["appointment_date"], appt["appointment_time"])).fetchone()
            if slot and slot["status"] == "free":
                c.execute("DELETE FROM slots WHERE id=?", (slot["id"],))
            elif slot:
                msg = "Careful: this slot has already been offered or given to another patient. Please check."
        c.execute("UPDATE appointments SET status=?, next_call_at=NULL WHERE id=?", (status, appt_id))
        audit(c, "receptionist", "changed outcome by hand", appt_label(appt), appt["status"], status)
    return go("/", msg)


@app.post("/clear")
def clear():
    with db() as c:
        n = c.execute("SELECT COUNT(*) n FROM appointments").fetchone()["n"]
        c.execute("DELETE FROM appointments")
        c.execute("UPDATE slots SET status='free', held_for=NULL WHERE held_for LIKE 'appt:%'")
        audit(c, "receptionist", "cleared the appointment list", "", f"{n} appointment(s)", "0")
    return go("/", "List cleared.")


@app.get("/export.csv")
def export(date: str = ""):
    with db() as c:
        rows = c.execute(
            "SELECT appointment_date, appointment_time, patient_name, phone, doctor_name, status, attempts, "
            "last_call_at, answered_by, callback_note, moved_from, source, visit_notes, summary, execution_id "
            "FROM appointments WHERE (?='' OR appointment_date=?) ORDER BY appointment_date, appointment_time",
            (date, date)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["appointment_date", "appointment_time", "patient_name", "phone", "doctor_name", "outcome", "tries",
                "last_call_at", "answered_by", "callback_note", "moved_from", "source", "visit_notes", "summary", "bolna_execution_id"])
    for r in rows:
        w.writerow(list(r))
    name = f"results-{date or 'all'}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------- doctor's sheet ----------

@app.get("/sheet", response_class=HTMLResponse)
def sheet(date: str = ""):
    """One printable page per day: who is coming and what they said the visit is about."""
    with db() as c:
        dates = [r[0] for r in c.execute("SELECT DISTINCT appointment_date FROM appointments ORDER BY 1")]
        if not date:
            tomorrow = (now().date() + timedelta(days=1)).isoformat()
            date = tomorrow if tomorrow in dates else (dates[-1] if dates else "")
        rows = c.execute(
            "SELECT * FROM appointments WHERE appointment_date=? AND status NOT IN ('cancelled') "
            "ORDER BY doctor_name COLLATE NOCASE, appointment_time", (date,)).fetchall()
    opts = "".join(f"<option {'selected' if d == date else ''}>{e(d)}</option>" for d in dates)
    out = ["<h1>Doctor's sheet</h1><p class='sub'>Visit notes are what the patient said on the phone, in their own words. "
           "They are not a diagnosis and have not been checked.</p>",
           f"<div class='card noprint'><form method='get' action='/sheet' class='row'><select name='date' onchange='this.form.submit()'>{opts}</select>"
           "<button type='button' onclick='window.print()'>Print</button></form></div>"]
    if not rows:
        out.append("<div class='card'>No appointments for this date.</div>")
    doctor = None
    for r in rows:
        if (r["doctor_name"] or "").lower() != (doctor or "").lower():
            if doctor is not None:
                out.append("</table></div>")
            doctor = r["doctor_name"]
            out.append(f"<div class='card'><h2>{e(doctor)} · {e(spoken_date(date))}</h2>"
                       "<table><tr><th>Time</th><th>Patient</th><th>Status</th><th>Visit notes</th></tr>")
        notes = e(r["visit_notes"]) if r["visit_notes"] else "<span class='muted'>None given</span>"
        if r["urgent"]:
            notes = "<b class='err'>Flagged urgent on the call.</b><br>" + notes
        out.append(f"<tr><td>{e(r['appointment_time'])}</td><td>{e(r['patient_name'])}</td>"
                   f"<td><span class='pill {e(r['status'])}'>{LABELS.get(r['status'], r['status'])}</span></td>"
                   f"<td style='white-space:pre-wrap'>{notes}</td></tr>")
    if doctor is not None:
        out.append("</table></div>")
    return page("".join(out), "/sheet")


# ---------- free slots page ----------

def doctor_datalist(c):
    names = {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM appointments")} | \
            {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM slots")}
    return "<datalist id='doctors'>" + "".join(f"<option value='{e(n)}'>" for n in sorted(names) if n) + "</datalist>"


@app.post("/slots/note", response_class=HTMLResponse)
def slots_note(note: str = Form("")):
    """Read a dictated note and show the slots it describes as a draft to check."""
    if not note.strip():
        return go("/slots", "The note was empty.")
    with db() as c:
        doctors = ({r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM appointments")}
                   | {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM slots")}
                   | {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM waitlist")})
    return slots_page(draft=parse_slot_note(note, doctors))


def add_one_slot(c, doctor_name, slot_date, slot_time, how="added free slot"):
    """Add one free slot. Returns a reason if it could not be added, otherwise None."""
    if not doctor_name or not valid_date_time(slot_date, slot_time):
        return "not a valid date and time"
    if slot_dt(slot_date, slot_time) < now():
        return "already in the past"
    taken = c.execute(
        "SELECT patient_name FROM appointments WHERE doctor_name=? COLLATE NOCASE AND appointment_date=? "
        "AND appointment_time=? AND status!='cancelled'", (doctor_name, slot_date, slot_time)).fetchone()
    if taken:
        return f"{taken['patient_name']} has an appointment then"
    cur = c.execute("INSERT OR IGNORE INTO slots (doctor_name, slot_date, slot_time, created_at) VALUES (?,?,?,?)",
                    (doctor_name, slot_date, slot_time, stamp()))
    if not cur.rowcount:
        return "already in the list"
    audit(c, "receptionist", how, f"{doctor_name}, {slot_date} {slot_time}")
    return None


@app.post("/slots/add-many")
def slots_add_many(doctor_name: str = Form(...), slot: list[str] = Form(default=[]), from_note: str = Form("")):
    doctor_name = doctor_name.strip()
    added, skipped = 0, []
    with db() as c:
        for item in slot:
            d, _, t = item.partition("|")
            why = add_one_slot(c, doctor_name, d, t, "added free slot from a voice note")
            if why:
                skipped.append(f"{d} {t} ({why})")
            else:
                added += 1
    msg = f"Added {added} free slot(s) for {doctor_name}."
    if skipped:
        msg += " Skipped: " + "; ".join(skipped)
    return go("/slots", msg)


@app.get("/slots", response_class=HTMLResponse)
def slots_page(msg: str = "", draft: dict | None = None):
    d = draft if isinstance(draft, dict) else {}
    with db() as c:
        rows = c.execute("SELECT * FROM slots WHERE slot_date >= ? ORDER BY slot_date, slot_time, doctor_name",
                         (now().date().isoformat(),)).fetchall()
        dl = doctor_datalist(c)
    out = ["<h1>Free slots</h1><p class='sub'>Times a doctor is free. When a patient asks to move, the agent offers "
           "up to 3 of these with the same doctor. Slots from cancelled appointments appear here on their own.</p>"]
    if msg:
        out.append(f"<div class='warn'>{e(msg)}</div>")
    out.append("<div class='card'><h2>Add by voice note</h2>"
               "<p class='muted'>Click in the box, hold your Wispr Flow key and speak. For example: "
               "&ldquo;Doctor Rao is free on Tuesday at 11 AM and 4 PM.&rdquo;</p>"
               "<form method='post' action='/slots/note'>"
               f"<textarea name='note' rows='2' style='width:100%;box-sizing:border-box;padding:8px;font-size:14px;border:1px solid #cbd0d9;border-radius:6px' "
               f"placeholder='Speak or type when a doctor is free' required>{e(d.get('note', ''))}</textarea>"
               "<div style='margin-top:8px'><button>Read note</button></div></form></div>")
    if d:
        gaps = (" I could not find: " + ", ".join(d["missing"]) + ".") if d["missing"] else ""
        remarks = " ".join(d["remarks"])
        out.append(f"<div class='warn'>Draft from your note. Untick anything wrong, then click Add these slots.{e(gaps)} {e(remarks)}</div>")
        ticks = "".join(
            f"<label style='display:block;margin:4px 0'><input type='checkbox' name='slot' value='{e(sd)}|{e(st)}' checked> "
            f"{e(spoken_date(sd))} at {e(spoken_time(st))} <span class='muted'>({e(sd)} {e(st)})</span></label>"
            for sd, st in d["slots"]) or "<p class='muted'>No slots could be read from the note. Use the form below instead.</p>"
        out.append(f"<div class='card'><h2>Check the draft</h2><form method='post' action='/slots/add-many'>{dl}"
                   f"<input name='doctor_name' list='doctors' placeholder='Doctor name' value='{e(d['doctor_name'])}' required>"
                   f"<input type='hidden' name='from_note' value='{e(d['note'])}'>{ticks}"
                   f"<div style='margin-top:8px'><button {'' if d['slots'] else 'disabled'}>Add these slots</button></div></form></div>")
    out.append(f"<div class='card'><h2>Add by typing</h2><form method='post' action='/slots/add' class='row'>{dl}"
               "<input name='doctor_name' list='doctors' placeholder='Doctor name' required>"
               "<input type='date' name='slot_date' required><input type='time' name='slot_time' required>"
               "<button>Add free slot</button></form></div>")
    if rows:
        out.append("<div class='card'><table><tr><th>Date</th><th>Time</th><th>Doctor</th><th>Status</th><th>From</th><th></th></tr>")
        for s in rows:
            text = {"free": "Free", "held": "Being offered on a call", "booked": f"Booked: {s['filled_by'] or ''}"}[s["status"]]
            origin = "Cancelled or moved appointment" if s["origin"] == "freed" else "Added by staff"
            rm = (f"<form method='post' action='/slots/delete/{s['id']}'><button class='small grey'>Remove</button></form>"
                  if s["status"] == "free" else "")
            out.append(f"<tr><td>{e(s['slot_date'])}</td><td>{e(s['slot_time'])}</td><td>{e(s['doctor_name'])}</td>"
                       f"<td>{e(text)}</td><td>{origin}</td><td>{rm}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card'>No free slots listed. Without any, a patient who asks to move is told the clinic will call back.</div>")
    return page("".join(out), "/slots")


@app.post("/slots/add")
def slots_add(doctor_name: str = Form(...), slot_date: str = Form(...), slot_time: str = Form(...)):
    doctor_name, slot_time = doctor_name.strip(), slot_time[:5]
    with db() as c:
        why = add_one_slot(c, doctor_name, slot_date, slot_time)
    return go("/slots", f"Not added: {why}." if why else "")


@app.post("/slots/delete/{slot_id}")
def slots_delete(slot_id: int):
    with db() as c:
        s = c.execute("SELECT * FROM slots WHERE id=? AND status='free'", (slot_id,)).fetchone()
        if s:
            c.execute("DELETE FROM slots WHERE id=?", (slot_id,))
            audit(c, "receptionist", "removed free slot", f"{s['doctor_name']}, {s['slot_date']} {s['slot_time']}")
    return go("/slots")


# ---------- voice note to waitlist entry ----------

DAY_WORDS = {"mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue", "wed": "wed",
             "wednesday": "wed", "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu", "fri": "fri",
             "friday": "fri", "sat": "sat", "saturday": "sat", "sun": "sun", "sunday": "sun"}


def _doc_key(name):
    return re.sub(r"\s+", " ", re.sub(r"\bdr\b\.?", "doctor", name.lower())).strip()


def parse_note(text, known_doctors):
    """Turn a dictated sentence into a draft waitlist entry. Simple word rules, so staff must check it."""
    raw = " ".join(text.split())
    work = raw

    phone = ""
    m = re.search(r"(?:\+?91[\s-]?)?(?:\d[\s-]?){10}", work)
    if m:
        phone = re.sub(r"\D", "", m.group())[-10:]
        work = work[:m.start()] + " " + work[m.end():]

    doctor = ""
    key = _doc_key(work)
    for name in sorted(known_doctors, key=len, reverse=True):
        if name and _doc_key(name) in key:
            doctor = name
            break
    doc_match = re.search(r"\b(?:doctor|dr)\b\.?\s+[A-Za-z]+", work, flags=re.I)
    if not doctor and doc_match:
        doctor = "Doctor " + doc_match.group().split()[-1].title()

    low = work.lower()
    days = [DAY_WORDS[w] for w in re.findall(r"[a-z]+", low) if w.rstrip("s") in DAY_WORDS or w in DAY_WORDS
            for w in [w if w in DAY_WORDS else w.rstrip("s")]]
    if "weekday" in low:
        days += ["mon", "tue", "wed", "thu", "fri"]
    if "weekend" in low:
        days += ["sat", "sun"]
    days = [d for d in DAYS if d in days]

    language = "hi" if re.search(r"\bhindi\b", low) else "en"

    name = re.sub(r"^(please\s+)?(add|put|note that|note|waitlist)\s+", "", work, flags=re.I)
    name = re.split(r"\b(can|could|wants?|would|is|needs?|prefers?|to the|on the|for|any|available|number|phone|speaks?|with)\b",
                    name, maxsplit=1, flags=re.I)[0]
    if doc_match and doc_match.group().lower() in name.lower():
        name = ""
    name = name.strip(" ,.-")

    missing = [label for label, value in [("patient name", name), ("phone number", phone), ("doctor", doctor)] if not value]
    return {"patient_name": name, "phone": phone, "doctor_name": doctor, "days": days,
            "preferred_language": language, "note": raw, "missing": missing}


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def find_doctor(work, known_doctors):
    key = _doc_key(work)
    for name in sorted(known_doctors, key=len, reverse=True):
        if name and _doc_key(name) in key:
            return name
    m = re.search(r"\b(?:doctor|dr)\b\.?\s+[A-Za-z]+", work, flags=re.I)
    return "Doctor " + m.group().split()[-1].title() if m else ""


def parse_slot_note(text, known_doctors):
    """Turn a dictated sentence into draft free slots (every date with every time). Staff must check it."""
    raw = " ".join(text.split())
    doctor = find_doctor(raw, known_doctors)
    work = re.sub(r"\b(?:doctor|dr)\b\.?\s+[A-Za-z]+", " ", raw, flags=re.I).lower()
    today = now().date()
    dates, remarks = [], []

    def add_date(d):
        if d not in dates:
            dates.append(d)

    def from_day_month(day, month, year=None):
        try:
            d = datetime(year or today.year, month, day).date()
            if not year and d < today:
                d = datetime(today.year + 1, month, day).date()
            add_date(d)
        except ValueError:
            remarks.append(f"'{day}/{month}' is not a real date, so I left it out.")

    mon = "(" + "|".join(MONTHS) + ")[a-z]*"
    patterns = [
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda m: from_day_month(int(m[3]), int(m[2]), int(m[1]))),
        (r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b",
         lambda m: from_day_month(int(m[1]), int(m[2]), (int(m[3]) + (2000 if int(m[3]) < 100 else 0)) if m[3] else None)),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + mon + r"\b", lambda m: from_day_month(int(m[1]), MONTHS[m[2]])),
        (r"\b" + mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", lambda m: from_day_month(int(m[2]), MONTHS[m[1]])),
        (r"\bday after tomorrow\b", lambda m: add_date(today + timedelta(days=2))),
        (r"\btomorrow\b", lambda m: add_date(today + timedelta(days=1))),
        (r"\btoday\b", lambda m: add_date(today)),
    ]
    for pattern, action in patterns:
        for m in re.finditer(pattern, work):
            action(m)
        work = re.sub(pattern, " ", work)
    for m in re.finditer(r"\b(mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b", work):
        target = DAYS.index(m[1][:3])
        ahead = (target - today.weekday()) % 7 or 7
        add_date(today + timedelta(days=ahead))
    work = re.sub(r"\b(mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b", " ", work)

    times = []
    if re.search(r"\bnoon\b", work):
        times.append("12:00")
    for m in re.finditer(r"\b(\d{1,2})(?:[:.](\d{2}))?(?:\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?))?(?:\s+in the (morning|afternoon|evening))?", work):
        hour, minute = int(m[1]), int(m[2] or 0)
        half = (m[3] or "").replace(".", "").replace(" ", "") or {"morning": "am", "afternoon": "pm", "evening": "pm"}.get(m[4] or "", "")
        if hour > 23 or minute > 59:
            continue
        if half == "pm" and hour < 12:
            hour += 12
        elif half == "am" and hour == 12:
            hour = 0
        elif not half and not m[2] and 1 <= hour <= 7:
            hour += 12
            remarks.append(f"No AM or PM heard for {m[1]}, so I assumed {m[1]} PM.")
        elif not half and not m[2] and 8 <= hour <= 11:
            remarks.append(f"No AM or PM heard for {m[1]}, so I assumed {m[1]} AM.")
        t = f"{hour:02d}:{minute:02d}"
        if t not in times:
            times.append(t)

    missing = [label for label, value in [("doctor", doctor), ("date", dates), ("time", times)] if not value]
    slots = [(d.isoformat(), t) for d in sorted(dates) for t in sorted(times)]
    return {"doctor_name": doctor, "slots": slots, "remarks": remarks, "missing": missing, "note": raw}


# ---------- waitlist page ----------

@app.post("/waitlist/note", response_class=HTMLResponse)
def waitlist_note(note: str = Form("")):
    """Read a dictated note and show it as a draft entry for the receptionist to check."""
    if not note.strip():
        return go("/waitlist", "The note was empty.")
    with db() as c:
        doctors = ({r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM appointments")}
                   | {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM slots")}
                   | {r[0] for r in c.execute("SELECT DISTINCT doctor_name FROM waitlist")})
    return waitlist_page(draft=parse_note(note, doctors))


@app.get("/waitlist", response_class=HTMLResponse)
def waitlist_page(msg: str = "", draft: dict | None = None):
    d = draft if isinstance(draft, dict) else {}
    with db() as c:
        rows = c.execute("SELECT * FROM waitlist WHERE status!='removed' ORDER BY status, created_at").fetchall()
        dl = doctor_datalist(c)
    out = ["<h1>Waitlist</h1><p class='sub'>Patients who want an earlier appointment. When a slot is freed, they are "
           "called one at a time, longest wait first, until someone takes it.</p>"]
    if msg:
        out.append(f"<div class='warn'>{e(msg)}</div>")
    out.append("<div class='card'><h2>Add by voice note</h2>"
               "<p class='muted'>Click in the box, hold your Wispr Flow key and speak. For example: "
               "&ldquo;Mr Gowda can come any morning this week for Doctor Rao, number 98765 43210.&rdquo;</p>"
               "<form method='post' action='/waitlist/note'>"
               f"<textarea name='note' rows='2' style='width:100%;box-sizing:border-box;padding:8px;font-size:14px;border:1px solid #cbd0d9;border-radius:6px' "
               f"placeholder='Speak or type a note about a waiting patient' required>{e(d.get('note', ''))}</textarea>"
               "<div style='margin-top:8px'><button>Read note</button></div></form></div>")
    if d:
        gaps = (" I could not find: " + ", ".join(d["missing"]) + ". Please fill those in.") if d.get("missing") else ""
        out.append(f"<div class='warn'>Draft from your note. Check every box below, then click Add to waitlist.{e(gaps)}</div>")
    v = lambda k: e(str(d.get(k, "")))
    days = "".join(f"<label><input type='checkbox' name='days' value='{x}' {'checked' if x in d.get('days', []) else ''}> {x.title()}</label>" for x in DAYS)
    hi = "selected" if d.get("preferred_language") == "hi" else ""
    out.append(f"<div class='card'><h2>{'Check the draft' if d else 'Add by typing'}</h2><form method='post' action='/waitlist/add'>{dl}<div class='row'>"
               f"<input name='patient_name' placeholder='Patient name' value='{v('patient_name')}' required>"
               f"<input name='phone' placeholder='Phone number' value='{v('phone')}' required>"
               f"<input name='doctor_name' list='doctors' placeholder='Doctor name' value='{v('doctor_name')}' required>"
               f"<select name='preferred_language'><option value='en'>English</option><option value='hi' {hi}>Hindi</option></select>"
               f"<input type='hidden' name='from_note' value='{v('note')}'></div>"
               f"<div class='row' style='margin-top:10px'><span class='muted'>Days they can come (leave empty for any day):</span>{days}"
               "<button>Add to waitlist</button></div></form></div>")
    if rows:
        out.append("<div class='card'><table><tr><th>Patient</th><th>Doctor</th><th>Days</th><th>Status</th><th>Added</th><th></th></tr>")
        for w in rows:
            st = "Waiting" if w["status"] == "active" else "Got a slot"
            rm = f"<form method='post' action='/waitlist/remove/{w['id']}'><button class='small grey'>Remove</button></form>"
            out.append(f"<tr><td>{e(w['patient_name'])}<br><small>{e(w['phone'])}</small></td><td>{e(w['doctor_name'])}</td>"
                       f"<td>{e('Any day' if w['days'] in ('', 'any') else w['days'].replace(',', ', ').title())}</td>"
                       f"<td>{st}</td><td>{e(w['created_at'][:10])}</td><td>{rm}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card'>Nobody is on the waitlist.</div>")
    return page("".join(out), "/waitlist")


@app.post("/waitlist/add")
def waitlist_add(patient_name: str = Form(...), phone: str = Form(...), doctor_name: str = Form(...),
                 preferred_language: str = Form("en"), days: list[str] = Form(default=[]),
                 from_note: str = Form("")):
    number = to_e164(phone)
    if not number:
        return go("/waitlist", f"Phone number '{phone}' is not valid.")
    chosen = ",".join(d for d in DAYS if d in days) or "any"
    with db() as c:
        if c.execute("SELECT 1 FROM do_not_call WHERE phone=?", (number,)).fetchone():
            return go("/waitlist", "This person asked not to be called, so they cannot be added.")
        c.execute("INSERT INTO waitlist (patient_name, phone, doctor_name, days, preferred_language, created_at) "
                  "VALUES (?,?,?,?,?,?)", (patient_name.strip(), number, doctor_name.strip(), chosen, preferred_language, stamp()))
        audit(c, "receptionist", "added to waitlist" + (" from a voice note" if from_note else ""),
              f"{patient_name.strip()} ({doctor_name.strip()})", "", from_note[:200])
    return go("/waitlist")


@app.post("/waitlist/remove/{wid}")
def waitlist_remove(wid: int):
    with db() as c:
        w = c.execute("SELECT * FROM waitlist WHERE id=?", (wid,)).fetchone()
        if w:
            c.execute("UPDATE waitlist SET status='removed' WHERE id=?", (wid,))
            audit(c, "receptionist", "removed from waitlist", f"{w['patient_name']} ({w['doctor_name']})")
    return go("/waitlist")


# ---------- activity log page ----------

@app.get("/log", response_class=HTMLResponse)
def log_page():
    with db() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall()
    out = ["<h1>Activity log</h1><p class='sub'>Every outcome and every change made by hand, newest first.</p>"]
    if rows:
        out.append("<div class='card'><table><tr><th>When</th><th>Who</th><th>What</th><th>Record</th><th>Before</th><th>After</th></tr>")
        for r in rows:
            out.append(f"<tr><td>{e(r['at'][:16].replace('T', ' '))}</td><td>{e(r['actor'])}</td><td>{e(r['action'])}</td>"
                       f"<td>{e(r['record'])}</td><td>{e(LABELS.get(r['before'], r['before']))}</td>"
                       f"<td>{e(LABELS.get(r['after'], r['after']))}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card'>Nothing logged yet.</div>")
    return page("".join(out), "/log")


if __name__ == "__main__":
    print("\nOpen this in your browser:  http://localhost:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
