#!/usr/bin/env python3
"""
LifeLink Backend — Flask + Supabase (Postgres)

Backs the non-biometric parts of the app: patient registration (photo,
demographics, medical info), the review/commit step, the patient dashboard,
existing-patient lookup ("identify"), and the full medical records
repository. Also relays real fingerprint enroll/verify commands to the
GT-521Fx2 sensor via the ESP32 in ../fingerprint-device/firmware (see the
"fingerprint device" section below) — the backend never runs the biometric
comparison itself, only the device does. Iris enroll/verify go through the
Pi Zero camera (../iris-module/camera-client, via /health + /capture) and
the local iris engine service in ../iris-module/engine (see the "iris"
section below).

Two databases: patient/medical data lives in Supabase (backend/schema.sql);
fingerprint and iris template bytes live in a local Postgres on this
machine (fingerprint-device/db/schema.sql + iris-module/db/schema.sql,
LOCAL_DB_* in backend/.env) and never touch Supabase.

Run once first:
  - apply backend/schema.sql in the Supabase SQL Editor
  - apply fingerprint-device/db/schema.sql, then iris-module/db/schema.sql,
    to your local `lifelink` database
  - start the iris engine: iris-module/engine/.venv/bin/python iris-module/engine/iris_service.py
Then:  backend/.venv/bin/python backend/app.py
Open:  http://localhost:3000
"""

import base64
import binascii
import hashlib
import json
import os
import random
import re
import string
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from urllib import error as urlerror, request as urlrequest

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory, abort
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
IRIS_ENGINE_URL = os.environ.get("IRIS_ENGINE_URL", "http://127.0.0.1:8010").rstrip("/")

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
DEVICE_COMMAND_MODES = {"enroll", "verify", "iris_enroll", "iris_verify", "photo_capture"}
# Requests served by the Pi Zero camera: iris scans and the patient photo.
CAMERA_MODES = ("iris_enroll", "iris_verify", "photo_capture")
IRIS_EYE_SIDES = {"left", "right"}


# ---------------------------------------------------------------- helpers --

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    app.logger.warning("%s %s -> %s: %s", request.method, request.path, err.status, err.message)
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


@app.route("/i18n/<path:filename>")
def i18n_file(filename):
    # Vendored i18next + UI translations (kept local so the kiosk works offline).
    return send_from_directory(PROJECT_ROOT / "i18n", filename)


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
    if mode in ("verify", "iris_verify"):
        if not patient_id:
            raise ApiError(f"patient_id is required for {mode} commands", 400)
        get_patient_row_by_lifelink_id(patient_id)
        if mode == "iris_verify" and not patient_has_iris_templates(patient_id):
            raise ApiError("This patient has no enrolled iris templates", 409)
    elif mode == "iris_enroll":
        eye_side = body.get("eye_side")
        if eye_side not in IRIS_EYE_SIDES:
            raise ApiError("eye_side must be 'left' or 'right'", 400)
        params = {"eye_side": eye_side}
    elif mode == "photo_capture":
        pass
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
        if mode in CAMERA_MODES:
            close_active_iris_commands(cur, device_id, "superseded", only_expired=False)
        cur.execute(
            """
            INSERT INTO device_commands (device_id, mode, patient_id, status, params)
            VALUES (%s, %s, %s, 'pending', %s)
            RETURNING id, device_id, mode, patient_id, status, result, created_at, updated_at
            """,
            (device_id, mode, patient_id if mode in ("verify", "iris_verify") else None,
             psycopg2.extras.Json(params)),
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


@app.route("/api/devices/<device_id>/commands/<int:command_id>/cancel", methods=["POST"])
def cancel_device_command(device_id, command_id):
    """The GUI gave up on a command (e.g. "skip iris for now"). Ends it if
    still open, so the camera stops scanning for it."""
    with local_db_cursor() as cur:
        cur.execute(
            """
            UPDATE device_commands
            SET status = 'error', result = %s, updated_at = now()
            WHERE id = %s AND device_id = %s AND status IN ('pending', 'in_progress')
            """,
            (psycopg2.extras.Json({"error": "cancelled"}), command_id, device_id),
        )
    return "", 204


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


# ------------------------------------------------------------------- iris --
# Pi 5 <-> Pi Zero camera protocol (see iris-module/README.md). The Pi Zero
# boots, finds this server (main-pi.local), and asks GET /health until it
# says ready — which it does only while the GUI has an active iris request
# (an iris_enroll / iris_verify device command) for that camera. It then
# scans, stops the camera, and POSTs its best frame to /capture:
#
#   SUCCESS -> processed (or nothing left to do); the Pi Zero shuts down
#   RESCAN  -> the engine rejected the frame; the Pi Zero scans again
#   RETRY   -> checksum mismatch / engine unreachable; resend the same frame
#
# Each answer is recorded against the frame's capture_id, so a resent
# capture gets the same answer instead of being processed twice. The frame
# goes to the local iris engine (../iris-module/engine/iris_service.py,
# IRIS_ENGINE_URL) and is never written to disk.
#
# Templates are stored encrypted in the local Postgres like fingerprints.
# An enroll template is stored as soon as it is extracted, unlinked
# (patient_id NULL); the commit step links it via
# POST /api/patients/<id>/iris-templates. The browser never receives
# template bytes.

MAX_IRIS_IMAGE_B64 = 7 * 1024 * 1024  # ~5 MB decoded
UNLINKED_IRIS_TTL = "1 day"
# Covers powering on the Pi Zero (it boots per scan) plus a few rescans;
# the frontend polls for the same length of time.
IRIS_COMMAND_TTL = "5 minutes"

class IrisEngineUnavailable(Exception):
    pass


def call_iris_engine(path, payload, timeout=30):
    """POSTs JSON to the iris engine. Returns (http_status, body). Raises
    IrisEngineUnavailable if the engine can't be reached."""
    req = urlrequest.Request(
        f"{IRIS_ENGINE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urlerror.HTTPError as err:
        try:
            body = json.loads(err.read())
        except ValueError:
            body = {}
        # FastAPI wraps HTTPException details in {"detail": ...}.
        return err.code, body.get("detail", body)
    except (urlerror.URLError, TimeoutError, ConnectionError) as err:
        raise IrisEngineUnavailable(str(err))


def patient_has_iris_templates(lifelink_id):
    with local_db_cursor() as cur:
        cur.execute("SELECT 1 FROM iris_templates WHERE patient_id = %s LIMIT 1", (lifelink_id,))
        return cur.fetchone() is not None


def close_active_iris_commands(cur, device_id, error, only_expired):
    """Ends open iris requests for a camera: the expired ones, or (when the
    GUI starts a new request) all of them."""
    cur.execute(
        f"""
        UPDATE device_commands
        SET status = 'error', result = %s, updated_at = now()
        WHERE device_id = %s AND mode IN %s AND status IN ('pending', 'in_progress')
          {"AND created_at < now() - interval '" + IRIS_COMMAND_TTL + "'" if only_expired else ""}
        """,
        (psycopg2.extras.Json({"error": error}), device_id, CAMERA_MODES),
    )


def active_iris_command(cur, device_id):
    close_active_iris_commands(cur, device_id, "expired", only_expired=True)
    cur.execute(
        """
        SELECT id, mode, patient_id, params FROM device_commands
        WHERE device_id = %s AND mode IN %s AND status IN ('pending', 'in_progress')
        ORDER BY created_at ASC LIMIT 1
        """,
        (device_id, CAMERA_MODES),
    )
    return cur.fetchone()


def set_iris_progress(cur, command_id, progress):
    cur.execute(
        """
        UPDATE device_commands SET status = 'in_progress', result = %s, updated_at = now()
        WHERE id = %s AND status IN ('pending', 'in_progress')
        """,
        (psycopg2.extras.Json({"progress": progress}), command_id),
    )


def finish_command(cur, command_id, status, result):
    cur.execute(
        """
        UPDATE device_commands SET status = %s, result = %s, updated_at = now()
        WHERE id = %s AND status IN ('pending', 'in_progress')
        """,
        (status, psycopg2.extras.Json(result), command_id),
    )


def capture_answer(cur, capture_id, device_id, command_id, status, reason=None):
    """Records and returns the final answer for a capture_id. RETRY answers
    are not recorded: the Pi Zero is meant to resend that same capture."""
    answer = {"capture_id": capture_id, "status": status}
    if reason:
        answer["reason"] = reason
    cur.execute(f"DELETE FROM iris_captures WHERE created_at < now() - interval '{UNLINKED_IRIS_TTL}'")
    cur.execute(
        """
        INSERT INTO iris_captures (device_id, capture_id, command_id, answer)
        VALUES (%s, %s, %s, %s) ON CONFLICT (device_id, capture_id) DO NOTHING
        """,
        (device_id, capture_id, command_id, psycopg2.extras.Json(answer)),
    )
    return jsonify(answer)


# Latest low-res preview frame per camera, so the operator can see where the
# eye is while the camera scans. Memory only, never written to disk, and
# served only while fresh.
MAX_PREVIEW_B64 = 300 * 1024
PREVIEW_MAX_AGE_SECONDS = 5
iris_previews = {}  # device_id -> (jpeg bytes, time.monotonic())


@app.route("/preview", methods=["POST"])
def post_preview():
    body = request.get_json(force=True, silent=True) or {}
    device_id = body.get("device_id")
    image_data = body.get("image_data")
    if not device_id or not isinstance(image_data, str) or len(image_data) > MAX_PREVIEW_B64:
        raise ApiError("device_id and image_data (base64 JPEG, max 300 KB) are required", 400)
    try:
        iris_previews[device_id] = (base64.b64decode(image_data, validate=True), time.monotonic())
    except (binascii.Error, ValueError):
        raise ApiError("image_data must be valid base64", 400)
    return "", 204


@app.route("/api/devices/<device_id>/preview", methods=["GET"])
def get_preview(device_id):
    frame = iris_previews.get(device_id)
    if not frame or time.monotonic() - frame[1] > PREVIEW_MAX_AGE_SECONDS:
        return "", 204
    return Response(frame[0], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


# Patient photos taken with the Pi Zero camera (photo_capture requests):
# held in memory until the GUI collects them once, then dropped. The GUI
# stages the photo in the registration draft like a webcam capture, and it
# is only written to uploads/ when the patient is committed.
PHOTO_MAX_AGE_SECONDS = 10 * 60
camera_photos = {}  # command_id -> (jpeg bytes, time.monotonic())


def store_camera_photo(command_id, jpeg):
    now = time.monotonic()
    for cid, (_, taken) in list(camera_photos.items()):
        if now - taken > PHOTO_MAX_AGE_SECONDS:
            camera_photos.pop(cid, None)
    camera_photos[command_id] = (jpeg, now)


@app.route("/api/devices/<device_id>/commands/<int:command_id>/photo", methods=["GET"])
def get_camera_photo(device_id, command_id):
    with local_db_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM device_commands WHERE id = %s AND device_id = %s AND mode = 'photo_capture'",
            (command_id, device_id),
        )
        if not cur.fetchone():
            raise ApiError("Command not found", 404)
    photo = camera_photos.pop(command_id, None)
    if not photo or time.monotonic() - photo[1] > PHOTO_MAX_AGE_SECONDS:
        raise ApiError("Photo not available; take it again", 404)
    return Response(photo[0], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/health", methods=["GET"])
def health():
    """Pi Zero discovery / readiness check. ready is true only while the GUI
    has an active iris request for this camera."""
    device_id = request.args.get("device_id")
    command = None
    if device_id:
        with local_db_cursor() as cur:
            command = active_iris_command(cur, device_id)
    answer = {"status": "ok", "system": "LifeLink Pi 5", "ready": command is not None}
    if command:
        # What to capture: an eye crop for the engine, or a face photo.
        answer["request"] = "photo" if command["mode"] == "photo_capture" else "iris"
    return jsonify(answer)


@app.route("/capture", methods=["POST"])
def post_capture():
    """The Pi Zero's best frame, processed against the oldest active request
    for that camera: an eye crop for iris requests, a face photo for
    photo_capture."""
    body = request.get_json(force=True, silent=True) or {}
    capture_id = body.get("capture_id")
    device_id = body.get("device_id")
    image_data = body.get("image_data")
    if not capture_id or not device_id:
        raise ApiError("capture_id and device_id are required", 400)
    if not image_data or not isinstance(image_data, str) or len(image_data) > MAX_IRIS_IMAGE_B64:
        raise ApiError("image_data (base64 image, max ~5 MB) is required", 400)

    try:
        raw = base64.b64decode(image_data, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("image_data must be valid base64", 400)
    image_hash = body.get("image_hash")
    if image_hash and hashlib.sha256(raw).hexdigest() != str(image_hash).lower():
        return jsonify({"capture_id": capture_id, "status": "RETRY", "reason": "checksum_mismatch"})

    with local_db_cursor() as cur:
        cur.execute(
            "SELECT answer FROM iris_captures WHERE device_id = %s AND capture_id = %s",
            (device_id, capture_id),
        )
        seen = cur.fetchone()
        if seen:
            return jsonify(seen["answer"])

        command = active_iris_command(cur, device_id)
        if not command:
            return capture_answer(cur, capture_id, device_id, None, "SUCCESS", "no_active_request")

        if command["mode"] == "photo_capture":
            if not raw.startswith(b"\xff\xd8\xff"):
                return capture_answer(cur, capture_id, device_id, command["id"], "RESCAN", "not_a_jpeg")
            store_camera_photo(command["id"], raw)
            finish_command(cur, command["id"], "done", {"photo_bytes": len(raw)})
            return capture_answer(cur, capture_id, device_id, command["id"], "SUCCESS")

        cur.execute("SELECT count(*) AS n FROM iris_captures WHERE command_id = %s", (command["id"],))
        attempt = cur.fetchone()["n"] + 1
        set_iris_progress(cur, command["id"], {"stage": "processing", "scan": attempt})

        stored = []
        if command["mode"] == "iris_verify":
            cur.execute(
                """
                SELECT eye_side, pgp_sym_decrypt_bytea(template_data, %s) AS template_data
                FROM iris_templates WHERE patient_id = %s
                """,
                (TEMPLATE_ENCRYPTION_KEY, command["patient_id"]),
            )
            stored = [{"eye_side": r["eye_side"],
                       "template_data": base64.b64encode(bytes(r["template_data"])).decode()}
                      for r in cur.fetchall()]
            if not stored:
                finish_command(cur, command["id"], "error", {"error": "no_iris_templates"})
                return capture_answer(cur, capture_id, device_id, command["id"], "SUCCESS", "no_iris_templates")

    # The engine call takes seconds; no transaction is held open across it.
    try:
        if command["mode"] == "iris_enroll":
            code, engine = call_iris_engine("/extract", {"image_data": image_data})
        else:
            code, engine = call_iris_engine("/verify", {"image_data": image_data, "templates": stored})
    except IrisEngineUnavailable as err:
        app.logger.warning("Iris engine unreachable at %s: %s", IRIS_ENGINE_URL, err)
        with local_db_cursor() as cur:
            set_iris_progress(cur, command["id"], {"stage": "engine_unavailable", "scan": attempt})
        return jsonify({"capture_id": capture_id, "status": "RETRY", "reason": "engine_unavailable"})

    with local_db_cursor() as cur:
        if code == 422:
            reason = engine.get("reason") if isinstance(engine, dict) else None
            set_iris_progress(cur, command["id"], {"stage": "rescan", "scan": attempt, "reason": reason})
            return capture_answer(cur, capture_id, device_id, command["id"], "RESCAN", reason)
        if code != 200:
            app.logger.warning("Iris engine %s returned %s: %s", command["mode"], code, engine)
            return jsonify({"capture_id": capture_id, "status": "RETRY", "reason": "engine_error"})

        if command["mode"] == "iris_enroll":
            eye_side = (command["params"] or {}).get("eye_side")
            cur.execute(
                f"DELETE FROM iris_templates WHERE patient_id IS NULL "
                f"AND created_at < now() - interval '{UNLINKED_IRIS_TTL}'"
            )
            cur.execute(
                """
                INSERT INTO iris_templates
                    (patient_id, eye_side, device_id, command_id, model_name, quality_score, template_data)
                VALUES (NULL, %s, %s, %s, %s, %s, pgp_sym_encrypt_bytea(%s, %s))
                """,
                (eye_side, device_id, command["id"], engine["model_name"], engine.get("quality_score"),
                 psycopg2.Binary(base64.b64decode(engine["template_data"])), TEMPLATE_ENCRYPTION_KEY),
            )
            result = {"eye_side": eye_side, "quality_score": engine.get("quality_score")}
        else:
            result = {k: engine.get(k) for k in ("match", "similarity", "eye_side", "threshold")}
        finish_command(cur, command["id"], "done", result)
        return capture_answer(cur, capture_id, device_id, command["id"], "SUCCESS")


@app.route("/api/patients/<lifelink_id>/iris-templates", methods=["POST"])
def link_patient_iris_templates(lifelink_id):
    """Links a registration's two captured eyes ({command_ids: [...]}, from
    their iris_enroll commands) to the newly created patient, in one
    transaction. Replaces any iris templates the patient already had."""
    body = request.get_json(force=True, silent=True) or {}
    command_ids = body.get("command_ids")
    if (not isinstance(command_ids, list) or len(command_ids) != 2
            or not all(isinstance(c, int) for c in command_ids)):
        raise ApiError("command_ids must be a list of the two iris_enroll command ids", 400)

    get_patient_row_by_lifelink_id(lifelink_id)
    with local_db_cursor() as cur:
        cur.execute(
            """
            SELECT id, eye_side FROM iris_templates
            WHERE command_id = ANY(%s) AND patient_id IS NULL
            FOR UPDATE
            """,
            (command_ids,),
        )
        rows = cur.fetchall()
        if sorted(r["eye_side"] for r in rows) != ["left", "right"]:
            raise ApiError("Need one captured, not yet linked template for each eye; re-capture the iris", 409)
        cur.execute("DELETE FROM iris_templates WHERE patient_id = %s", (lifelink_id,))
        cur.execute(
            "UPDATE iris_templates SET patient_id = %s WHERE id = ANY(%s)",
            (lifelink_id, [r["id"] for r in rows]),
        )
    return jsonify({"patient_id": lifelink_id, "stored": 2, "eye_sides": ["left", "right"]}), 201


if __name__ == "__main__":
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 50)
    print(" LifeLink Backend (Flask + Supabase)")
    print(f" Serving app + API at: http://localhost:{PORT}")
    print("=" * 50)
    # Werkzeug debugger allows remote code execution; only enable explicitly.
    app.run(host="0.0.0.0", port=PORT, debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
