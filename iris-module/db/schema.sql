-- LifeLink iris local database schema.
-- Apply AFTER fingerprint-device/db/schema.sql (it extends device_commands):
--   PGPASSWORD=… psql -U lifelink -h localhost -d lifelink -f iris-module/db/schema.sql
--
-- Same rules as fingerprints: iris templates live only in this local
-- Postgres, encrypted with pgp_sym_encrypt_bytea (TEMPLATE_ENCRYPTION_KEY in
-- backend/.env), and reference a Supabase patient by lifelink_id.
--
-- Safe to run multiple times (every statement is idempotent).

create extension if not exists pgcrypto;

-- A template is extracted while the patient does not exist yet (mid
-- registration). It is stored right away with patient_id NULL and the
-- enroll command's id; the commit step links it to the new patient. Rows
-- never linked are purged by the backend after a day.
create table if not exists iris_templates (
  id            serial primary key,
  patient_id    text,
  eye_side      text not null check (eye_side in ('left', 'right')),
  device_id     text not null,
  command_id    integer references device_commands(id) on delete set null,
  model_name    text not null,
  quality_score real,
  template_data bytea not null,
  created_at    timestamptz not null default now()
);

create unique index if not exists uq_iris_templates_patient_eye
  on iris_templates(patient_id, eye_side) where patient_id is not null;
create index if not exists idx_iris_templates_command_id on iris_templates(command_id);

-- device_commands is shared with the fingerprint device; allow the Pi Zero
-- camera's command modes too (iris scans and the patient photo).
alter table device_commands drop constraint if exists device_commands_mode_check;
alter table device_commands add constraint device_commands_mode_check
  check (mode in ('enroll', 'verify', 'identify', 'iris_enroll', 'iris_verify', 'photo_capture'));

-- One row per Pi Zero capture that got a final answer (SUCCESS/RESCAN), so
-- a capture resent after a lost response gets the same answer instead of
-- being processed twice. Purged by the backend after a day.
create table if not exists iris_captures (
  device_id   text not null,
  capture_id  text not null,
  command_id  integer references device_commands(id) on delete set null,
  answer      jsonb not null,
  created_at  timestamptz not null default now(),
  primary key (device_id, capture_id)
);
create index if not exists idx_iris_captures_command_id on iris_captures(command_id);
