-- Analytics database DDL for the 3CX Sales-Funnel AI Reporting Agent.
-- Applied idempotently by `init-db`. Lives in the team's OWN Postgres,
-- never in the 3CX database.

CREATE TABLE IF NOT EXISTS bde_agents (
  extension   text PRIMARY KEY,
  bde_name    text,
  email       text,
  group_name  text,
  role_name   text,
  in_scope    boolean NOT NULL DEFAULT false,
  active      boolean NOT NULL DEFAULT true,
  synced_at   timestamptz
);

CREATE TABLE IF NOT EXISTS calls (
  call_id           text PRIMARY KEY,
  bde_extension     text REFERENCES bde_agents(extension),
  bde_name          text,
  direction         text,
  dest_number       text,
  started_at        timestamptz,
  ring_seconds      int,
  talk_seconds      int,
  answered          boolean,
  is_voicemail      boolean,
  call_type         text,
  recording_present boolean,
  has_transcript    boolean NOT NULL DEFAULT false,
  fresh_or_followup text,            -- 'fresh' | 'followup'
  in_scope          boolean NOT NULL DEFAULT false,
  lead_id           text
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls (started_at);
CREATE INDEX IF NOT EXISTS idx_calls_dest ON calls (dest_number, started_at);
CREATE INDEX IF NOT EXISTS idx_calls_ext_day ON calls (bde_extension, started_at);

CREATE TABLE IF NOT EXISTS transcripts (
  call_id   text PRIMARY KEY REFERENCES calls(call_id),
  source    text DEFAULT '3cx',
  diarized  boolean,
  text      text,
  sentiment text,
  summary   text
);

CREATE TABLE IF NOT EXISTS classifications (
  call_id            text PRIMARY KEY REFERENCES calls(call_id),
  rpc_connect        boolean, rpc_confidence   numeric,
  full_pitch         boolean, pitch_confidence numeric,
  is_lead            boolean, lead_confidence  numeric,
  qualified          boolean, qual_confidence  numeric,
  meeting_booked     boolean,
  call_outcome       text,
  evidence           jsonb,
  model              text,
  classified_at      timestamptz,
  needs_human_review boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_class_review ON classifications (needs_human_review);

CREATE TABLE IF NOT EXISTS meetings (
  lead_id      text,
  call_id      text REFERENCES calls(call_id),
  booked_at    timestamptz,
  meeting_done boolean,             -- from calendar/CRM (pluggable adapter)
  source       text
);

CREATE TABLE IF NOT EXISTS daily_funnel (
  report_date   date,
  bde_name      text,               -- each in-scope BDE, or 'ALL'
  track         text,               -- 'fresh' | 'followup' | 'combined'
  calls_made    int,
  connected     int,                -- CDR proxy
  transcribed   int,                -- coverage
  rpc_connect   int,
  full_pitch    int,
  leads         int,
  qualified       int,              -- combined track only
  meetings_booked int,              -- combined track only; from the call transcript
  meetings_done   int,              -- combined track only; from calendar/CRM (optional, future)
  PRIMARY KEY (report_date, bde_name, track)
);
-- Forward-compatible: add the transcript-derived "booked" column on existing DBs.
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS meetings_booked int;

CREATE TABLE IF NOT EXISTS processing_state (
  job                 text PRIMARY KEY,   -- 'funnel_agent'
  last_processed_date date,
  backfill_complete   boolean NOT NULL DEFAULT false,
  updated_at          timestamptz
);
