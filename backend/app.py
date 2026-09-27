#!/usr/bin/env python3
"""
LifeLink Backend — Flask + Supabase (Postgres)

Backs the non-biometric parts of the app: patient registration (photo,
demographics, medical info), the review/commit step, the patient dashboard,
existing-patient lookup ("identify"), and the full medical records
repository. Also relays real fingerprint enroll/verify commands to the
GT-521Fx2 sensor via the ESP32 in ../fingerprint-device/firmware (see the
"fingerprint device" section below) — the backend never runs the biometric
comparison itself, only the device does. Iris capture stays a pure
frontend simulation (no iris hardware exists) — see LifeLinkBiometrics in
index.html.

Two databases: patient/medical data lives in Supabase (backend/schema.sql);
fingerprint template bytes live in a local Postgres on this machine
(fingerprint-device/db/schema.sql, LOCAL_DB_* in backend/.env) and never
touch Supabase.

Run once first:
  - apply backend/schema.sql in the Supabase SQL Editor
  - apply fingerprint-device/db/schema.sql to your local `lifelink` database
Then:  backend/.venv/bin/python backend/app.py
Open:  http://localhost:3000
"""

import base64
import binascii
import os
import random
import re
import string
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
UPLOADS_DIR = BASE_DIR / "uploads"

load_dotenv(BASE_DIR / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
PORT = int(os.environ.get("BACKEND_PORT", 3000))
TEMPLATE_ENCRYPTION_KEY = os.environ["TEMPLATE_ENCRYPTION_KEY"]

LOCAL_DB_CONFIG = {
    "host": os.environ.get("LOCAL_DB_HOST", "localhost"),
    "port": int(os.environ.get("LOCAL_DB_PORT", 5432)),
    "dbname": os.environ.get("LOCAL_DB_NAME", "lifelink"),
    "user": os.environ.get("LOCAL_DB_USER", "lifelink"),
    "password": os.environ.get("LOCAL_DB_PASSWORD", "lifelink"),
}

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


@contextmanager
def local_db_cursor():
    """Cursor against the local Postgres holding fingerprint data. Template
    bytes never leave this machine or go through Supabase. Commits on a
    clean exit, rolls back on exception, always closes the connection."""
    conn = psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **LOCAL_DB_CONFIG)
    try:
        with conn, conn.cursor() as cur:
            yield cur
    finally:
        conn.close()

app = Flask(__name__, static_folder=None)
CORS(app)

RECORD_CATEGORIES = {
    "Visits", "Diagnoses", "Prescriptions", "Lab Reports",
    "Surgeries", "Vaccinations", "Scans", "Discharge Summaries",
}
ALLERGY_CATEGORIES = {"drug", "food", "environmental"}
DEVICE_COMMAND_MODES = {"enroll", "verify"}


# ---------------------------------------------------------------- helpers --

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"error": err.message}), err.status


def parse_date_or_none(value, field_label):
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"{field_label} must be a valid date (YYYY-MM-DD)", 400)
    return value


def validate_patient_dates(body):
    """Validate every date field up front so a bad value fails before any
    write happens, instead of leaving a half-written patient behind."""
    if "dob" in body:
        parse_date_or_none(body.get("dob"), "dob")
    for s in body.get("surgeries") or []:
        parse_date_or_none(s.get("date"), "surgery date")


def calculate_age(dob_str):
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = date.today()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return max(years, 0)


def gen_lifelink_id(gender):
    suffix = "F" if (gender or "").lower().startswith("f") else (
        "M" if (gender or "").lower().startswith("m") else "X"
    )
    for _ in range(20):
        candidate = f"LL-{random.randint(1000, 9999)}-{suffix}"
        existing = (
            supabase.table("patients")
            .select("patient_id")
            .eq("lifelink_id", candidate)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return candidate
    raise ApiError("Could not allocate a unique patient id, try again", 500)


def gen_record_id():
    for _ in range(20):
        candidate = f"REC-{random.randint(1000, 9999)}"
        existing = (
            supabase.table("clinical_records")
            .select("record_id")
            .eq("record_id", candidate)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return candidate
    raise ApiError("Could not allocate a unique record id, try again", 500)


def get_patient_row_by_lifelink_id(lifelink_id):
    res = (
        supabase.table("patients")
        .select("*")
        .eq("lifelink_id", lifelink_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise ApiError(f"No patient found for id {lifelink_id}", 404)
    return res.data[0]


def save_photo_data_url(lifelink_id, photo_data_url):
    """Decode a base64 data: URL from the browser canvas capture and write
    it to backend/uploads/<lifelink_id>.jpg. Returns the public path."""
    match = re.match(r"^data:image/(\w+);base64,(.+)$", photo_data_url or "")
    if not match:
        raise ApiError("photoDataUrl must be a base64 image data URL", 400)
    ext = "jpg" if match.group(1) in ("jpeg", "jpg") else match.group(1)
    try:
        raw = base64.b64decode(match.group(2))
    except (binascii.Error, ValueError):
        raise ApiError("photoDataUrl is not valid base64", 400)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{lifelink_id}.{ext}"
    (UPLOADS_DIR / filename).write_bytes(raw)
    return f"/uploads/{filename}"


def assemble_patient(row):
    """Join a patients row with every related table into the nested shape
    the frontend's LifeLinkStore patient object already expects."""
    pid = row["patient_id"]

    allergies_rows = (
        supabase.table("allergies").select("*").eq("patient_id", pid).execute().data
    )
    allergies = {"drug": [], "food": [], "environmental": []}
    for a in allergies_rows:
        cat = a.get("category") or "drug"
        if cat not in allergies:
            cat = "drug"
        allergies[cat].append({
            "name": a.get("allergen"),
            "severity": a.get("severity"),
            "note": a.get("reaction") or "",
        })

    contacts = (
        supabase.table("emergency_contacts")
        .select("*")
        .eq("patient_id", pid)
        .order("priority")
        .execute()
        .data
    )
    primary_contact = contacts[0] if contacts else {}

    history_rows = (
        supabase.table("medical_history").select("*").eq("patient_id", pid).execute().data
    )
    chronic_conditions = [h["condition"] for h in history_rows]

    med_rows = (
        supabase.table("medications").select("*").eq("patient_id", pid).execute().data
    )
    medications = [{
        "name": m.get("medicine_name"),
        "dosage": m.get("dosage") or "",
        "frequency": m.get("frequency") or "",
        "purpose": m.get("purpose") or "",
    } for m in med_rows]

    surgery_rows = (
        supabase.table("surgeries").select("*").eq("patient_id", pid).execute().data
    )
    surgeries = [{
        "name": s.get("surgery_name"),
        "date": s.get("surgery_date"),
        "hospital": s.get("hospital") or "",
    } for s in surgery_rows]

    device_rows = (
        supabase.table("medical_devices").select("*").eq("patient_id", pid).execute().data
    )
    medical_devices = [d["device_name"] for d in device_rows]

    record_rows = (
        supabase.table("clinical_records")
        .select("*")
        .eq("patient_id", pid)
        .order("record_date", desc=True)
        .execute()
        .data
    )
    records = [{
        "id": r["record_id"],
        "category": r["category"],
        "date": r.get("record_date"),
        "title": r.get("title"),
        "doctor": r.get("doctor") or "",
        "facility": r.get("facility") or "",
        "summary": r.get("summary") or "",
        "code": r.get("code") or "",
        "badge": r.get("badge") or "",
    } for r in record_rows]

    return {
        "id": row["lifelink_id"],
        "fullName": row["full_name"],
        "dob": row.get("date_of_birth"),
        "age": calculate_age(row.get("date_of_birth")),
        "gender": row.get("gender") or "",
        "bloodGroup": row.get("blood_group") or "",
        "phone": row.get("phone") or "",
        "email": row.get("email") or "",
        "emergencyContact": primary_contact.get("name", ""),
        "emergencyPhone": primary_contact.get("phone", ""),
        "address": row.get("address") or "",
        "nationalId": row.get("national_id") or "",
        "photoUrl": row.get("photo_path") or "",
        "allergies": allergies,
        "chronicConditions": chronic_conditions,
        "medications": medications,
        "surgeries": surgeries,
        "medicalDevices": medical_devices,
        "emergencyNotes": row.get("emergency_notes") or "",
        "records": records,
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def replace_child_rows(table, patient_id, rows):
    """Delete-and-reinsert pattern for a patient's child rows on update —
    simple and safe at this app's scale (one patient's rows at a time)."""
    supabase.table(table).delete().eq("patient_id", patient_id).execute()
    if rows:
        supabase.table(table).insert(rows).execute()


def write_related_rows(patient_id, body):
    for s in body.get("surgeries") or []:
        parse_date_or_none(s.get("date"), "surgery date")

    allergies = body.get("allergies") or {}
    allergy_rows = []
    for cat in ALLERGY_CATEGORIES:
        for item in allergies.get(cat, []) or []:
            if not item.get("name"):
                continue
            allergy_rows.append({
                "patient_id": patient_id,
                "allergen": item["name"],
                "reaction": item.get("note") or "",
                "severity": item.get("severity") or "Mild",
                "category": cat,
            })
    replace_child_rows("allergies", patient_id, allergy_rows)

    contact_name = body.get("emergencyContact")
    contact_rows = []
    if contact_name:
        contact_rows.append({
            "patient_id": patient_id,
            "name": contact_name,
            "phone": body.get("emergencyPhone") or "",
            "priority": 1,
        })
    replace_child_rows("emergency_contacts", patient_id, contact_rows)

    history_rows = [
        {"patient_id": patient_id, "condition": c, "status": "chronic"}
        for c in (body.get("chronicConditions") or []) if c
    ]
    replace_child_rows("medical_history", patient_id, history_rows)

    med_rows = [
        {
            "patient_id": patient_id,
            "medicine_name": m.get("name"),
            "dosage": m.get("dosage") or "",
            "frequency": m.get("frequency") or "",
            "purpose": m.get("purpose") or "",
        }
        for m in (body.get("medications") or []) if m.get("name")
    ]
    replace_child_rows("medications", patient_id, med_rows)

    surgery_rows = [
        {
            "patient_id": patient_id,
            "surgery_name": s.get("name"),
            "surgery_date": parse_date_or_none(s.get("date"), "surgery date"),
            "hospital": s.get("hospital") or "",
        }
        for s in (body.get("surgeries") or []) if s.get("name")
    ]
    replace_child_rows("surgeries", patient_id, surgery_rows)

    device_rows = [
        {"patient_id": patient_id, "device_name": d}
        for d in (body.get("medicalDevices") or []) if d
    ]
    replace_child_rows("medical_devices", patient_id, device_rows)


# ------------------------------------------------------------------ pages --

@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOADS_DIR, filename)


# -------------------------------------------------------------- patients --

@app.route("/api/patients", methods=["GET"])
def list_patients():
    """Lightweight list — used by the Identify page's quick-select list."""
    rows = (
        supabase.table("patients")
        .select("lifelink_id, full_name, gender, blood_group, date_of_birth, photo_path")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return jsonify([{
        "id": r["lifelink_id"],
        "fullName": r["full_name"],
        "gender": r.get("gender") or "",
        "bloodGroup": r.get("blood_group") or "",
        "age": calculate_age(r.get("date_of_birth")),
        "photoUrl": r.get("photo_path") or "",
    } for r in rows])


@app.route("/api/patients/search", methods=["GET"])
def search_patients():
    """MRN / name search for the Identify page's manual fallback."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    rows = (
        supabase.table("patients")
        .select("lifelink_id, full_name, gender, blood_group, date_of_birth, photo_path")
        .or_(f"lifelink_id.ilike.%{q}%,full_name.ilike.%{q}%")
        .limit(20)
        .execute()
        .data
    )
    return jsonify([{
        "id": r["lifelink_id"],
        "fullName": r["full_name"],
        "gender": r.get("gender") or "",
        "bloodGroup": r.get("blood_group") or "",
        "age": calculate_age(r.get("date_of_birth")),
        "photoUrl": r.get("photo_path") or "",
    } for r in rows])


@app.route("/api/patients/<lifelink_id>", methods=["GET"])
def get_patient(lifelink_id):
    row = get_patient_row_by_lifelink_id(lifelink_id)
    return jsonify(assemble_patient(row))


@app.route("/api/patients", methods=["POST"])
def create_patient():
    """Commit step: registration draft (patient-info + medical-info +
    photo) becomes a real persisted patient."""
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("fullName"):
        raise ApiError("fullName is required", 400)
    validate_patient_dates(body)

    if body.get("id"):
        lifelink_id = body["id"]
        existing = (
            supabase.table("patients")
            .select("patient_id")
            .eq("lifelink_id", lifelink_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            raise ApiError(f"Patient id {lifelink_id} already exists", 409)
    else:
        lifelink_id = gen_lifelink_id(body.get("gender"))

    photo_path = None
    if body.get("photoDataUrl"):
        photo_path = save_photo_data_url(lifelink_id, body["photoDataUrl"])

    insert_res = (
        supabase.table("patients")
        .insert({
            "lifelink_id": lifelink_id,
            "full_name": body["fullName"],
            "date_of_birth": parse_date_or_none(body.get("dob"), "dob"),
            "gender": body.get("gender") or "",
            "blood_group": body.get("bloodGroup") or "",
            "phone": body.get("phone") or "",
            "email": body.get("email") or "",
            "address": body.get("address") or "",
            "national_id": body.get("nationalId") or "",
            "emergency_notes": body.get("emergencyNotes") or "",
            "photo_path": photo_path,
        })
        .execute()
    )
    patient_row = insert_res.data[0]
    write_related_rows(patient_row["patient_id"], body)

    return jsonify(assemble_patient(get_patient_row_by_lifelink_id(lifelink_id))), 201


@app.route("/api/patients/<lifelink_id>", methods=["PUT"])
def update_patient(lifelink_id):
    """Update Profile modal on the dashboard."""
    row = get_patient_row_by_lifelink_id(lifelink_id)
    body = request.get_json(force=True, silent=True) or {}

    photo_path = row.get("photo_path")
    if body.get("photoDataUrl"):
        photo_path = save_photo_data_url(lifelink_id, body["photoDataUrl"])

    supabase.table("patients").update({
        "full_name": body.get("fullName", row["full_name"]),
        "date_of_birth": parse_date_or_none(body.get("dob", row.get("date_of_birth")), "dob"),
        "gender": body.get("gender", row.get("gender")),
        "blood_group": body.get("bloodGroup", row.get("blood_group")),
        "phone": body.get("phone", row.get("phone")),
        "email": body.get("email", row.get("email")),
        "address": body.get("address", row.get("address")),
        "national_id": body.get("nationalId", row.get("national_id")),
        "emergency_notes": body.get("emergencyNotes", row.get("emergency_notes")),
        "photo_path": photo_path,
    }).eq("patient_id", row["patient_id"]).execute()

    # Only replace child-table rows if the caller actually sent that section,
    # so a partial update (e.g. just the photo) doesn't wipe everything else.
    partial = {
        k: body[k] for k in (
            "allergies", "emergencyContact", "emergencyPhone",
            "chronicConditions", "medications", "surgeries", "medicalDevices",
        ) if k in body
    }
    if partial:
        current = assemble_patient(row)
        merged = {**current, **partial}
        write_related_rows(row["patient_id"], merged)

    return jsonify(assemble_patient(get_patient_row_by_lifelink_id(lifelink_id)))


# --------------------------------------------------------------- records --

@app.route("/api/patients/<lifelink_id>/records", methods=["GET"])
def list_records(lifelink_id):
    row = get_patient_row_by_lifelink_id(lifelink_id)
    category = request.args.get("category")
    q = (request.args.get("q") or "").strip()

    query = supabase.table("clinical_records").select("*").eq("patient_id", row["patient_id"])
    if category and category != "All":
        query = query.eq("category", category)
    if q:
        query = query.or_(
            f"title.ilike.%{q}%,summary.ilike.%{q}%,doctor.ilike.%{q}%,facility.ilike.%{q}%"
        )
    rows = query.order("record_date", desc=True).execute().data

    return jsonify([{
        "id": r["record_id"],
        "category": r["category"],
        "date": r.get("record_date"),
        "title": r.get("title"),
        "doctor": r.get("doctor") or "",
        "facility": r.get("facility") or "",
        "summary": r.get("summary") or "",
        "code": r.get("code") or "",
        "badge": r.get("badge") or "",
    } for r in rows])


@app.route("/api/patients/<lifelink_id>/records", methods=["POST"])
def add_record(lifelink_id):
    row = get_patient_row_by_lifelink_id(lifelink_id)
    body = request.get_json(force=True, silent=True) or {}

    category = body.get("category")
    title = body.get("title")
    if category not in RECORD_CATEGORIES:
        raise ApiError(f"category must be one of {sorted(RECORD_CATEGORIES)}", 400)
    if not title:
        raise ApiError("title is required", 400)

    record_id = gen_record_id()
    insert_res = (
        supabase.table("clinical_records")
        .insert({
            "record_id": record_id,
            "patient_id": row["patient_id"],
            "category": category,
            "record_date": parse_date_or_none(body.get("date"), "date") or date.today().isoformat(),
            "title": title,
            "doctor": body.get("doctor") or "",
            "facility": body.get("facility") or "",
            "summary": body.get("summary") or "",
            "code": body.get("code") or "",
            "badge": body.get("badge") or "",
        })
        .execute()
    )
    r = insert_res.data[0]
    return jsonify({
        "id": r["record_id"],
        "category": r["category"],
        "date": r.get("record_date"),
        "title": r.get("title"),
        "doctor": r.get("doctor") or "",
        "facility": r.get("facility") or "",
        "summary": r.get("summary") or "",
        "code": r.get("code") or "",
        "badge": r.get("badge") or "",
    }), 201


# ------------------------------------------------------------ fingerprint --
# Real GT-521Fx2 hardware, relayed through the ESP32 firmware in
# ../fingerprint-device/firmware (see that folder's README for wiring and
# flashing). Set the device's config.py BACKEND_BASE_URL to this machine's
# LAN address (not localhost) and DEVICE_ID to match what the frontend uses.
#
# Template bytes live in a local Postgres (LOCAL_DB_* in backend/.env), not
# Supabase — encrypted at rest with pgcrypto's pgp_sym_encrypt_bytea /
# pgp_sym_decrypt_bytea (TEMPLATE_ENCRYPTION_KEY), so the plaintext template
# never touches a plain INSERT/UPDATE or leaves this machine. Patients
# themselves stay in Supabase; these tables reference a patient by
# lifelink_id (text), not the local db's own (otherwise unused) patients
# table from the original module.

# GT-521Fx2 templates are fixed-size; anything else is a truncated capture.
GT521_TEMPLATE_SIZE = 498


@app.route("/api/devices/<device_id>/commands", methods=["POST"])
def create_device_command(device_id):
    """Enqueued by the frontend's 'Start Enrollment' / 'Scan to Identify'
    actions; the device polls for and claims it below."""
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    if mode not in DEVICE_COMMAND_MODES:
        raise ApiError(f"mode must be one of {sorted(DEVICE_COMMAND_MODES)}", 400)

    patient_id = body.get("patient_id")
    params = None
    if mode == "verify":
        if not patient_id:
            raise ApiError("patient_id is required for verify commands", 400)
        get_patient_row_by_lifelink_id(patient_id)
    else:
        # Fingers already captured earlier in this registration, so the
        # device can refuse the same finger being enrolled twice.
        exclude = body.get("exclude_templates") or []
        if not isinstance(exclude, list) or len(exclude) > 9:
            raise ApiError("exclude_templates must be a list of at most 9 templates", 400)
        for t in exclude:
            decode_template(t.get("template_data") if isinstance(t, dict) else None)
            if not t.get("finger_position"):
                raise ApiError("each exclude_templates entry needs a finger_position", 400)
        params = {"exclude_templates": [
            {"finger_position": t["finger_position"], "template_data": t["template_data"]} for t in exclude
        ]}

    with local_db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_commands (device_id, mode, patient_id, status, params)
            VALUES (%s, %s, %s, 'pending', %s)
            RETURNING id, device_id, mode, patient_id, status, result, created_at, updated_at
            """,
            (device_id, mode, patient_id if mode == "verify" else None, psycopg2.extras.Json(params)),
        )
        row = cur.fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/devices/<device_id>/commands/pending", methods=["GET"])
def get_pending_device_command(device_id):
    """Polled by the device. Atomically claims (pending -> in_progress) and
    returns the oldest queued command for this device, or 204 if none.
    FOR UPDATE SKIP LOCKED means two concurrent pollers can never claim the
    same row."""
    with local_db_cursor() as cur:
        cur.execute(
            """
            UPDATE device_commands
            SET status = 'in_progress', updated_at = now()
            WHERE id = (
                SELECT id FROM device_commands
                WHERE device_id = %s AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, device_id, mode, patient_id, status, params, result, created_at, updated_at
            """,
            (device_id,),
        )
        row = cur.fetchone()
    if not row:
        return "", 204
    return jsonify(dict(row))


@app.route("/api/devices/<device_id>/commands/<int:command_id>", methods=["GET"])
def get_device_command(device_id, command_id):
    with local_db_cursor() as cur:
        cur.execute(
            """
            SELECT id, device_id, mode, patient_id, status, result, created_at, updated_at
            FROM device_commands WHERE device_id = %s AND id = %s
            """,
            (device_id, command_id),
        )
        row = cur.fetchone()
    if not row:
        raise ApiError("Command not found", 404)
    return jsonify(dict(row))


@app.route("/api/devices/<device_id>/commands/<int:command_id>/result", methods=["POST"])
def post_device_command_result(device_id, command_id):
    """The device reports its outcome here; the frontend's poll of
    GET .../commands/<id> picks it up from the same row."""
    body = request.get_json(force=True, silent=True) or {}
    status = body.get("status")
    if status not in ("done", "error"):
        raise ApiError("status must be 'done' or 'error'", 400)

    with local_db_cursor() as cur:
        cur.execute(
            """
            UPDATE device_commands
            SET status = %s, result = %s, updated_at = now()
            WHERE id = %s AND device_id = %s
            RETURNING id, device_id, mode, patient_id, status, result, created_at, updated_at
            """,
            (status, psycopg2.extras.Json(body.get("result")), command_id, device_id),
        )
        row = cur.fetchone()
    if not row:
        raise ApiError("Command not found", 404)
    return jsonify(dict(row))


@app.route("/api/devices/<device_id>/commands/<int:command_id>/progress", methods=["POST"])
def post_device_command_progress(device_id, command_id):
    """Live step updates while a command runs ({stage: place|lift, scan, of})
    so the frontend can tell the operator what to do at the sensor."""
    body = request.get_json(force=True, silent=True) or {}
    with local_db_cursor() as cur:
        cur.execute(
            """
            UPDATE device_commands SET result = %s, updated_at = now()
            WHERE id = %s AND device_id = %s AND status = 'in_progress'
            RETURNING id
            """,
            (psycopg2.extras.Json({"progress": body}), command_id, device_id),
        )
        row = cur.fetchone()
    if not row:
        raise ApiError("Command not found or not in progress", 404)
    return "", 204


def decode_template(template_data):
    if not template_data:
        raise ApiError("template_data is required", 400)
    try:
        raw = base64.b64decode(template_data, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("template_data must be valid base64", 400)
    if len(raw) != GT521_TEMPLATE_SIZE:
        raise ApiError(f"template_data must decode to {GT521_TEMPLATE_SIZE} bytes, got {len(raw)}", 400)
    return raw


def upsert_templates(cur, lifelink_id, device_id, templates):
    """templates: [(finger_position, raw_bytes)]. Encryption happens inside
    the query via pgp_sym_encrypt_bytea."""
    ids = []
    for finger_position, raw in templates:
        cur.execute(
            """
            INSERT INTO fingerprint_templates
                (patient_id, finger_position, device_id, device_slot, template_data)
            VALUES (%s, %s, %s, NULL, pgp_sym_encrypt_bytea(%s, %s))
            ON CONFLICT (patient_id, finger_position)
            DO UPDATE SET device_id = EXCLUDED.device_id,
                          device_slot = NULL,
                          template_data = EXCLUDED.template_data,
                          created_at = now()
            RETURNING id
            """,
            (lifelink_id, finger_position, device_id, psycopg2.Binary(raw), TEMPLATE_ENCRYPTION_KEY),
        )
        ids.append(cur.fetchone()["id"])
    return ids


@app.route("/api/patients/<lifelink_id>/templates", methods=["POST"])
def store_patient_templates(lifelink_id):
    """All of a registration's captured fingers in one local-Postgres
    transaction: either every template is stored or none is."""
    body = request.get_json(force=True, silent=True) or {}
    device_id = body.get("device_id")
    templates = body.get("templates")
    if not device_id or not isinstance(templates, list) or not templates:
        raise ApiError("device_id and a non-empty templates list are required", 400)
    if len(templates) > 10:
        raise ApiError("at most 10 templates per patient", 400)

    get_patient_row_by_lifelink_id(lifelink_id)
    decoded, seen = [], set()
    for t in templates:
        position = t.get("finger_position") if isinstance(t, dict) else None
        if not position:
            raise ApiError("each template needs a finger_position", 400)
        if position in seen:
            raise ApiError(f"finger_position {position} listed twice", 400)
        seen.add(position)
        decoded.append((position, decode_template(t.get("template_data"))))

    with local_db_cursor() as cur:
        ids = upsert_templates(cur, lifelink_id, device_id, decoded)
    return jsonify({"patient_id": lifelink_id, "stored": len(ids),
                    "finger_positions": [p for p, _ in decoded]}), 201


@app.route("/api/enroll", methods=["POST"])
def enroll_fingerprint():
    """Links a captured template (from a 'done' enroll command's result) to
    a real patient — called by the frontend right after patient creation,
    since the device has no patient_id yet at capture time. Encryption
    happens inside the query via pgp_sym_encrypt_bytea — the plaintext
    template never gets assigned to a Python variable that touches a plain
    INSERT/UPDATE."""
    body = request.get_json(force=True, silent=True) or {}
    lifelink_id = body.get("patient_id")
    finger_position = body.get("finger_position")
    device_id = body.get("device_id")
    template_data = body.get("template_data")
    if not lifelink_id or not finger_position or not device_id or not template_data:
        raise ApiError("patient_id, finger_position, device_id and template_data are required", 400)

    get_patient_row_by_lifelink_id(lifelink_id)
    raw = decode_template(template_data)
    with local_db_cursor() as cur:
        template_id = upsert_templates(cur, lifelink_id, device_id, [(finger_position, raw)])[0]

    return jsonify({
        "template_id": template_id,
        "patient_id": lifelink_id,
        "finger_position": finger_position,
    }), 201


@app.route("/api/patients/<lifelink_id>/templates", methods=["GET"])
def get_patient_templates(lifelink_id):
    """The patient's decrypted templates. The sensor stores nothing
    permanently: the device loads each one into a scratch slot and runs the
    1:1 match on-chip against a live capture."""
    get_patient_row_by_lifelink_id(lifelink_id)
    with local_db_cursor() as cur:
        cur.execute(
            """
            SELECT finger_position, device_id, device_slot,
                   pgp_sym_decrypt_bytea(template_data, %s) AS template_data
            FROM fingerprint_templates
            WHERE patient_id = %s
            """,
            (TEMPLATE_ENCRYPTION_KEY, lifelink_id),
        )
        rows = cur.fetchall()
    return jsonify([{
        "finger_position": r["finger_position"],
        "device_id": r["device_id"],
        "device_slot": r["device_slot"],
        "template_data": base64.b64encode(bytes(r["template_data"])).decode(),
    } for r in rows])


@app.route("/api/verify", methods=["POST"])
def verify_fingerprint():
    """Records the result of an on-sensor 1:1 comparison the
    device already performed — the backend never sees raw template bytes
    here, only what the device reports."""
    body = request.get_json(force=True, silent=True) or {}
    lifelink_id = body.get("patient_id")
    if not lifelink_id:
        raise ApiError("patient_id is required", 400)
    get_patient_row_by_lifelink_id(lifelink_id)

    return jsonify({
        "match": bool(body.get("match")),
        "confidence": body.get("confidence", 1.0),
        "finger_position": body.get("finger_position"),
    })


if __name__ == "__main__":
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 50)
    print(" LifeLink Backend (Flask + Supabase)")
    print(f" Serving app + API at: http://localhost:{PORT}")
    print("=" * 50)
    # Werkzeug debugger allows remote code execution; only enable explicitly.
    app.run(host="0.0.0.0", port=PORT, debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
