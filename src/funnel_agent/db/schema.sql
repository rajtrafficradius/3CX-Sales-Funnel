-- Analytics database DDL for the 3CX Sales-Funnel AI Reporting Agent.
-- Applied idempotently by `init-db`. Lives in the team's OWN Postgres,
-- never in the 3CX database.

-- Fail fast if any DDL (ALTER TABLE …) is blocked by a lock held by the running
-- refresh loop, instead of hanging — the entrypoint then simply retries init-db.
SET lock_timeout = '4s';

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
  recording_id      text,            -- 3CX RecordingId, for downloading the audio
  has_transcript    boolean NOT NULL DEFAULT false,
  fresh_or_followup text,            -- 'fresh' | 'followup'
  in_scope          boolean NOT NULL DEFAULT false,
  lead_id           text
);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_id text;
-- Which telephony system the call came from. Some BDEs / the BDM dial via Aircall
-- instead of 3CX; this routes recording download + transcription to the right client.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT '3cx';
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
  meeting_confirmation boolean,  -- TRUE = only re-confirmed a meeting booked earlier (no new date)
  meeting_rescheduled  boolean,  -- TRUE = re-booked an earlier meeting to a new date (still counts)
  -- Lead qualification (BAPU) + routing
  budget             boolean,
  authority          boolean,
  problem            boolean,
  urgency            boolean,
  lead_temperature   text,       -- 'warm' | 'hot' | 'super_hot' | 'none' (Pipeline 1 only)
  pipeline           text,       -- 'pipeline1_interested' | 'pipeline2_existing_agency' | 'none'
  callback_requested boolean,
  callback_when      text,
  contract_end       text,       -- Pipeline 2: prospect's agency contract-end signal
  recommended_cadence_days int,  -- Pipeline 2: AI-recommended days until next call
  prospect_company   text,
  prospect_website   text,       -- bare domain; enrichment cache key
  prospect_industry  text,
  call_outcome       text,
  evidence           jsonb,
  model              text,
  classified_at      timestamptz,
  needs_human_review boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_class_review ON classifications (needs_human_review);
-- Forward-compatible: add new columns on existing DBs.
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS meeting_confirmation boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS meeting_rescheduled boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS budget boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS authority boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS problem boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS urgency boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS lead_temperature text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS pipeline text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS callback_requested boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS callback_when text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS contract_end text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS recommended_cadence_days int;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_company text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_website text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_industry text;
-- BANT + AO, auto-validation, booking firmness, richer extraction (2026-06-23)
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS aspiration boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS open_to_listening boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS runs_paid_ads boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS has_marketing_agency boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS problem_summary text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS booking_status text;  -- firm|tentative|cancelled_or_declined|none
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS meeting_datetime text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_contact_name text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_mobile text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS prospect_email text;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS do_not_contact boolean;  -- rude / asked to be removed (#5)
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS gatekeeper_handled_well boolean;  -- RPC gatekeeper handling (#5)
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS gatekeeper_notes text;
-- Company-level booking memory (2026-06-29): a referral/handoff to a 2nd contact at a
-- company that ALREADY had the meeting arranged is NOT a new booking. booking_already_exists
-- flags that case (like reschedule/confirmation but across DIFFERENT contacts). company_key
-- is a stable per-company id (domain -> company-name-slug -> phone) used to dedup bookings
-- at the COMPANY level, not just per phone number.
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS booking_already_exists boolean;
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS company_key text;
CREATE INDEX IF NOT EXISTS idx_class_website ON classifications (prospect_website);
CREATE INDEX IF NOT EXISTS idx_class_company_key ON classifications (company_key);

-- WhatsApp nurturing queue (#13). One row per scheduled message in the meeting-
-- confirmation sequence. The engine schedules these when a meeting is booked and a
-- worker sends due ones via the Meta Cloud API (dry-run when not configured).
CREATE TABLE IF NOT EXISTS whatsapp_messages (
  id           bigserial PRIMARY KEY,
  call_id      text,                  -- the booking call that triggered the sequence
  dest_number  text NOT NULL,         -- recipient (prospect) phone
  step         text NOT NULL,         -- 'confirm' | 'value' | 'reminder'
  template     text,                  -- Meta template name used
  scheduled_at timestamptz NOT NULL,
  sent_at      timestamptz,
  status       text NOT NULL DEFAULT 'scheduled',  -- scheduled|sent|failed|skipped|dry_run
  wa_message_id text,
  error        text,
  created_at   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_due ON whatsapp_messages (status, scheduled_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_call_step ON whatsapp_messages (call_id, step) WHERE call_id IS NOT NULL;

-- BDM/admin override of the AI's qualification on a booked meeting (#4b). The AI
-- qualifies every booking (BANT); a BDM (Raven/Kiran) or admin can re-qualify it with
-- a MANDATORY reason, and the funnel's Qualified Booked count honours the override.
CREATE TABLE IF NOT EXISTS qualification_overrides (
  call_id     text PRIMARY KEY REFERENCES calls(call_id),
  qualified   boolean NOT NULL,
  reason      text NOT NULL,
  override_by text,
  created_at  timestamptz DEFAULT now()
);

-- Raw uploaded business records (e.g. D&B export). UNLIKE `prospects` (one row per
-- domain), this keeps EVERY given business as its own row — many businesses can share
-- one domain, and many have no domain at all. The Database browser groups these by
-- domain (summing revenue) for display; the prospect page shows all businesses + the
-- dynamic enrichment (Apollo / website / tracking) for the domain.
CREATE TABLE IF NOT EXISTS companies (
  id            bigserial PRIMARY KEY,
  source        text NOT NULL DEFAULT 'given_data',  -- which upload this came from
  company_name  text NOT NULL,
  company_profile text,
  year_started  int,
  incorporated  text,
  fiscal_year_end text,
  employees     int,
  revenue_musd  numeric,        -- revenue in millions USD (static, from the file)
  industry      text,
  sub_industry  text,
  address       text,
  suburb        text,
  state         text,
  postal_code   text,
  country       text,
  domain        text,           -- cleaned bare domain, NULL when none on file
  phone         text,           -- as given
  phone_norm    text,           -- trailing-9 AU form for matching
  source_url    text,
  parent_company text,
  subsidiaries  text,
  branches      text,
  contacts      jsonb,          -- [{"name":..,"role":..}, ...] up to 5
  created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_companies_phone ON companies(phone_norm);
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(lower(company_name));

-- Per-domain marketing enrichment cache (SEMrush + Apollo free company data).
-- Keyed by bare domain so many calls to the same business reuse one lookup.
CREATE TABLE IF NOT EXISTS enrichment (
  domain     text PRIMARY KEY,
  semrush    jsonb,
  apollo     jsonb,
  website    jsonb,             -- free site scrape: tracking pixels (GTM/GA4/Meta/etc.),
                                -- detected paid-ads activity, emails/socials, summary
  status     text,               -- 'ok' | 'partial' | 'error' | 'not_found'
  fetched_at timestamptz
);
ALTER TABLE enrichment ADD COLUMN IF NOT EXISTS website jsonb;
ALTER TABLE enrichment ADD COLUMN IF NOT EXISTS business_intel jsonb;
ALTER TABLE enrichment ADD COLUMN IF NOT EXISTS whois jsonb;

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
  qualified       int,              -- split by track (fresh/followup/combined)
  meetings_booked int,              -- ANY new booking the BDE made (loose)
  qualified_booked int,             -- strict subset: booking that is also qualified (BAPU)
  meetings_done   int,              -- split by track; from calendar/CRM (optional, future)
  -- Lead quality (interested pipeline) + pipeline routing
  warm            int,
  hot             int,
  super_hot       int,
  pipeline1       int,              -- interested
  pipeline2       int,              -- not interested, already with an agency
  PRIMARY KEY (report_date, bde_name, track)
);
-- Forward-compatible: add the transcript-derived "booked" column on existing DBs.
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS meetings_booked int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS qualified_booked int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS warm int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS hot int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS super_hot int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS pipeline1 int;
ALTER TABLE daily_funnel ADD COLUMN IF NOT EXISTS pipeline2 int;

CREATE TABLE IF NOT EXISTS processing_state (
  job                 text PRIMARY KEY,   -- 'funnel_agent'
  last_processed_date date,
  backfill_complete   boolean NOT NULL DEFAULT false,
  updated_at          timestamptz
);

-- ============================ Phase 2: auth + calendar ============================
-- Per-person login. One account per in-scope BDE (sees only own data) + manager(s)
-- (see everyone). Passwords are PBKDF2-hashed (stdlib); sessions are DB-backed.
CREATE TABLE IF NOT EXISTS users (
  id            serial PRIMARY KEY,
  email         text UNIQUE NOT NULL,
  name          text,
  role          text NOT NULL DEFAULT 'bde',     -- 'admin' | 'bdm' | 'bde' | 'manager'(legacy)
  bde_name      text,                            -- canonical daily_funnel bde_name (NULL for admin/bdm/manager)
  password_hash text NOT NULL,
  active        boolean NOT NULL DEFAULT true,
  must_change   boolean NOT NULL DEFAULT true,    -- prompt password change on first login
  created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
  token      text PRIMARY KEY,
  user_id    int REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now(),
  expires_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

-- In-dashboard calendar. Interested-prospect callbacks are auto-assigned to the
-- right BDE (idempotent per source call_id via the partial unique index below).
CREATE TABLE IF NOT EXISTS calendar_events (
  id         serial PRIMARY KEY,
  bde_name   text,                                -- owner (matches daily_funnel.bde_name)
  type       text NOT NULL DEFAULT 'callback',    -- 'callback' | 'meeting' | 'note'
  title      text,
  start_at   timestamptz,
  end_at     timestamptz,
  status     text NOT NULL DEFAULT 'pending',     -- 'pending' | 'done' | 'cancelled'
  call_id    text,                                -- source call (callbacks); idempotency key
  dest_number text,
  notes      text,
  created_by text,
  created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calendar_bde_start ON calendar_events (bde_name, start_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_callback_call
  ON calendar_events (call_id) WHERE type = 'callback' AND call_id IS NOT NULL;

-- ============================ Master prospect database ============================
-- The team's known-prospect universe (seeded from DATA_MASTER_FILE), keyed by the
-- prospect's WEBSITE (domain). A 3CX call links to a prospect by phone (normalized
-- AU form, see phones_norm) or by business name resolved from the transcript.
-- Holds the decision-maker contacts + SEMrush metrics so every call to a known
-- business inherits real firmographics + lets us enrich one domain once.
CREATE TABLE IF NOT EXISTS prospects (
  id              serial PRIMARY KEY,
  domain          text UNIQUE,          -- bare, lowercased website; identity + enrichment key
  business_name   text,                 -- display name (from master file or transcript)
  location        text,                 -- state / region
  -- decision-maker contacts (master file carries up to 2; overflow + user-added go in extra_contacts)
  contact1_name   text, contact1_title text, contact1_mobile text, contact1_phone text,
  contact2_name   text, contact2_title text, contact2_mobile text, contact2_phone text,
  extra_contacts  jsonb,                -- [{name,title,mobile,phone,added_by,added_at}] overflow / user-added
  -- normalized AU phone forms (trailing 9 digits) across ALL contacts, for matching 3CX dest_number
  phones_norm     text[] NOT NULL DEFAULT '{}',
  -- SEMrush-style metrics seeded from the master file (live enrichment cached separately in `enrichment`)
  competitor_relevance text,
  common_keywords      int,
  organic_keywords     int,
  organic_traffic      int,
  organic_cost         numeric,
  adwords_keywords     int,
  -- pipeline / assignment state (set by classification + BDM overrides)
  pipeline        text,                 -- 'pipeline1_interested' | 'pipeline2_existing_agency' | null
  assigned_bde    text,                 -- current owner (Pipeline-2 rotation / BDM override)
  contract_end    text,                 -- free-text contract-end signal from calls (Pipeline 2 cadence)
  next_action_at  timestamptz,          -- AI/BDM-scheduled next touch
  notes           text,
  extra           jsonb,                -- flexible extra fields (future master-file columns)
  source          text DEFAULT 'master_file',
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prospects_phones ON prospects USING gin (phones_norm);
CREATE INDEX IF NOT EXISTS idx_prospects_pipeline ON prospects (pipeline);
CREATE INDEX IF NOT EXISTS idx_prospects_assigned ON prospects (assigned_bde);

-- Pipeline-2 assignment + cadence state, keyed by the normalized dialled number
-- (dest9 = trailing 9 significant digits) — the same distinct-prospect key the
-- pipeline tables count by. Works whether or not the prospect is in the master DB.
-- Pipeline 2 = prospect already with an agency: we keep calling with a DIFFERENT
-- BDE each attempt (rotation) on an AI/BDM-set cadence until they book.
CREATE TABLE IF NOT EXISTS prospect_pipeline (
  dest9          text PRIMARY KEY,
  dest_number    text,                  -- a representative dialled form (for display/links)
  prospect_id    int REFERENCES prospects(id) ON DELETE SET NULL,
  pipeline       text,                  -- engine manages 'pipeline2_existing_agency'
  business_name  text,
  assigned_bde   text,                  -- current owner (NEXT BDE to call) — rotation / BDM override
  last_bde       text,                  -- who called most recently (never auto-assign back to them)
  next_action_at timestamptz,           -- scheduled next touch
  cadence_days   int,                   -- effective cadence (default ~monthly; AI/contract-aware later)
  contract_end   text,                  -- contract-end signal from calls (future: AI-derived)
  attempts       int NOT NULL DEFAULT 0,
  last_call_at   timestamptz,
  override_by    text,                  -- BDM email if manually overridden (auto-rotate then yields)
  notes          text,
  dnd            boolean NOT NULL DEFAULT false,  -- #5 do-not-call (rude / asked to be removed)
  dnd_reason     text,
  dnd_at         timestamptz,
  dnd_by         text,                  -- 'ai' (auto) or a BDM email (manual)
  updated_at     timestamptz DEFAULT now()
);
ALTER TABLE prospect_pipeline ADD COLUMN IF NOT EXISTS dnd boolean NOT NULL DEFAULT false;
ALTER TABLE prospect_pipeline ADD COLUMN IF NOT EXISTS dnd_reason text;
ALTER TABLE prospect_pipeline ADD COLUMN IF NOT EXISTS dnd_at timestamptz;
ALTER TABLE prospect_pipeline ADD COLUMN IF NOT EXISTS dnd_by text;
CREATE INDEX IF NOT EXISTS idx_pp_assigned ON prospect_pipeline (assigned_bde, next_action_at);
CREATE INDEX IF NOT EXISTS idx_pp_next ON prospect_pipeline (next_action_at);

-- Rotation/assignment history (every (re)assignment, auto or BDM), keyed by dest9.
CREATE TABLE IF NOT EXISTS prospect_assignments (
  id           serial PRIMARY KEY,
  dest9        text,
  bde_name     text,
  assigned_by  text,                    -- 'auto_rotate' | BDM email
  reason       text,
  call_id      text,                    -- the call that triggered the (re)assignment, if any
  created_at   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prospect_assign ON prospect_assignments (dest9, created_at);
