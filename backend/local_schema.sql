-- Backend-owned tables in the local `lifelink` Postgres. Applied by
-- backend/app.py at every start (every statement is idempotent), after
-- fingerprint-device/db/schema.sql and iris-module/db/schema.sql.

-- Command modes: fingerprint enroll/verify/identify (1:N, no patient id)
-- plus the Pi Zero camera's modes. Same list as iris-module/db/schema.sql.
alter table device_commands drop constraint if exists device_commands_mode_check;
alter table device_commands add constraint device_commands_mode_check
  check (mode in ('enroll', 'verify', 'identify', 'iris_enroll', 'iris_verify', 'photo_capture'));

-- Last known copy of each patient's record (the GET /api/patients/<id>
-- JSON), so identification and the emergency view keep working while
-- Supabase is unreachable. Refreshed on every successful read/write.
create table if not exists patient_cache (
  lifelink_id  text primary key,
  full_name    text not null,
  summary      jsonb not null,   -- list-row shape (id, fullName, gender, ...)
  record       jsonb not null,   -- full patient JSON
  created_at   timestamptz,      -- patient's creation time (list order)
  cached_at    timestamptz not null default now()
);

-- Operator audit trail (was browser localStorage). Hash-chained: each row's
-- entry_hash covers the previous row's hash, so an edited or deleted row
-- breaks the chain (checked by GET /api/audit/verify).
create table if not exists audit_events (
  id           bigserial primary key,
  occurred_at  timestamptz not null default now(),
  event        text not null,
  detail       text not null default '',
  prev_hash    text not null,
  entry_hash   text not null
);
create index if not exists idx_audit_events_occurred_at on audit_events(occurred_at);
