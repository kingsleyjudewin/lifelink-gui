-- LifeLink fingerprint local database schema.
-- Run as: PGPASSWORD=lifelink psql -U lifelink -h localhost -d lifelink -f fingerprint-device/db/schema.sql
--
-- Deliberately separate from Supabase: fingerprint template bytes never
-- leave this machine. Patients themselves stay in Supabase (see
-- backend/schema.sql) -- these tables reference a patient by lifelink_id
-- (text, e.g. "LL-1234-M"), not a local patients table. The original
-- life-link-finger-print- module's own `patients`/`audit_log` tables are
-- not used by this integration.
--
-- Safe to run multiple times (every statement is idempotent).

create extension if not exists pgcrypto;

-- template_data is encrypted at rest with pgcrypto's pgp_sym_encrypt_bytea
-- / pgp_sym_decrypt_bytea. The passphrase lives in backend/.env
-- (TEMPLATE_ENCRYPTION_KEY), never in this database.
create table if not exists fingerprint_templates (
  id              serial primary key,
  patient_id      text not null,
  finger_position text not null,
  device_id       text not null,
  device_slot     integer,  -- legacy; templates are no longer stored on the sensor
  template_data   bytea not null,
  created_at      timestamptz not null default now(),
  unique (patient_id, finger_position)
);

-- Orchestration handoff between the frontend and the ESP32: the frontend
-- enqueues {mode, patient_id}, the device polls (pending -> in_progress),
-- then reports back (-> done/error) with `result` for the frontend's poll
-- to pick up. patient_id is required for 'verify' (claimed-identity check)
-- and left null for 'enroll' (the patient doesn't exist yet -- the
-- captured template rides back in `result` and is only linked to a
-- patient afterward via POST /api/enroll).
create table if not exists device_commands (
  id          serial primary key,
  device_id   text not null,
  mode        text not null check (mode in ('enroll', 'verify')),
  patient_id  text,
  status      text not null default 'pending' check (status in ('pending', 'in_progress', 'done', 'error')),
  params      jsonb,  -- enroll: {exclude_templates: [...]} for the duplicate-finger check
  result      jsonb,  -- while in_progress: {progress: {stage, scan, of}}
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- Upgrade older databases: the sensor no longer holds templates, so there
-- is no slot to record.
alter table fingerprint_templates alter column device_slot drop not null;
alter table device_commands add column if not exists params jsonb;

create index if not exists idx_fingerprint_templates_patient_id on fingerprint_templates(patient_id);
create index if not exists idx_device_commands_device_status on device_commands(device_id, status, created_at);
