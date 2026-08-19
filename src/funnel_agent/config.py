"""Central configuration, loaded from environment / .env via pydantic-settings.

Every tunable lives here so nothing is hardcoded in the modules. The 3CX DB
schema mapping (CDR + transcript table/column names) is configuration too —
it is filled from the Phase B discovery (`SCHEMA_NOTES.md`) so ingestion never
hardcodes guesses about the 3CX schema.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | None) -> list[str]:
    """Parse a comma-separated env string into a clean list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class CdrSchema(BaseSettings):
    """Column mapping for the 3CX `cdr_output` table (overridden after Phase B)."""

    model_config = SettingsConfigDict(env_prefix="CDR_", env_file=".env", extra="ignore")

    table: str = "cdr_output"
    col_call_id: str = "call_id"
    col_extension: str = "src_extension"
    col_direction: str = "direction"
    col_dest_number: str = "dst_number"
    col_started_at: str = "start_time"
    col_ring_seconds: str = "ringing_dur"
    col_talk_seconds: str = "talking_dur"
    col_disposition: str = "termination_reason"
    outbound_value: str = "Outbound"


class TranscriptSchema(BaseSettings):
    """Mapping for the 3CX transcript source (discovered + confirmed in Phase B)."""

    model_config = SettingsConfigDict(env_prefix="TRANSCRIPT_", env_file=".env", extra="ignore")

    table: str = ""
    col_call_id: str = ""
    col_text: str = ""
    col_sentiment: str = ""
    col_summary: str = ""
    col_diarized: str = ""

    @property
    def configured(self) -> bool:
        """True once Phase B has filled in the minimum needed to read transcripts."""
        return bool(self.table and self.col_call_id and self.col_text)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- 3CX Configuration API ---
    threecx_api_base: str = "https://dotmappers.3cx.in:5001"
    threecx_client_id: str = ""
    threecx_client_secret: str = ""
    threecx_verify_tls: bool = True

    # --- Source of CDR + transcripts ---
    # 'api' = read from the 3CX Configuration API (Recordings + call data) — no DB access needed.
    # 'db'  = read from the 3CX PostgreSQL directly (original design; needs SOURCE_DB_DSN).
    source_mode: str = "api"

    # --- Databases ---
    source_db_dsn: str = ""
    analytics_db_dsn: str = ""

    # --- LLM ---
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model_cheap: str = ""
    llm_model_strong: str = ""
    anthropic_api_key: str = ""                   # Claude API — powers the Lisa-4 AI website designer
    google_places_api_key: str = ""               # GOOGLE_PLACES_API_KEY — Lisa-4's Maps prospect sweep
    lisa4_designer_model: str = "claude-sonnet-4-5"  # Claude model the AI designer uses to build the site
    lisa_sms_model: str = "claude-opus-4-8"       # Claude model the inbound-SMS auto-responder composes replies with
    confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    llm_max_transcript_chars: int = 24000
    classify_workers: int = 8  # parallel LLM calls during classification

    # Audio-transcription fallback: when 3CX has a recording but no transcript,
    # download the WAV and transcribe it ourselves (closes 3CX's transcription gaps).
    transcribe_missing: bool = True
    transcribe_model: str = "gpt-4o-mini-transcribe"
    transcribe_workers: int = 6
    # Only STT recordings older than this — gives 3CX time to transcribe recent
    # calls for free; we only pay to fill genuine gaps (and on intraday refreshes).
    transcribe_min_age_minutes: int = 15

    # --- Aircall (some BDEs / the BDM dial via Aircall instead of 3CX) ---
    # Basic-auth REST API: API ID (AIRCALL_APP_ID) + API token (AIRCALL_API_KEY).
    # Auto-enabled when both are set; only in-scope agents' calls are ingested, matched by name
    # against AIRCALL_AGENT_NAMES (preferred) or, if unset, ROSTER_INSCOPE_NAMES. A dedicated
    # list keeps Aircall scoping independent of the 3CX roster rule: a deployment that scopes
    # 3CX by GROUPS (so ROSTER_INSCOPE_NAMES is empty) must still name its Aircall agents here,
    # otherwise no Aircall calls are ever ingested.
    aircall_app_id: str = ""
    aircall_api_key: str = ""
    aircall_base: str = "https://api.aircall.io/v1"
    aircall_page_size: int = 50  # Aircall's max per_page
    # Fireflies — capture Alfred's recorded meetings/calls + auto-detect callbacks. Lowercase env var name.
    fireflies_api: str = Field(default="", validation_alias="fireflies_api")
    aircall_agent_names: str = ""  # CSV of BDE/BDM names who dial via Aircall (Alfred, Ben, …)
    message_classify_days: int = 21  # only classify inbound SMS/chat within this recency window
    messages_enabled: bool = False   # gate the refresh loop's SMS capture+auto-booking (off until validated)

    # --- Lisa-1: the AI cold-caller subsystem (Retell talk-only). Isolated from the 3CX/Aircall funnel. ---
    retellai_api_key: str = ""
    lisa_enabled: bool = False                    # master switch for outbound Lisa calls
    lisa_agent_id: str = "agent_a2a482e7f3bc1fad2fde7360d3"
    lisa4_agent_id: str = "agent_b24cb23e82cdbea2caa25a4d41"  # Lisa 4 — website-selling agent (pre-built reveal)
    lisa4_pool_size: int = 500                    # reserved Lisa-4 prospect pool (no-website + critical-issue)
    lisa4_use_dnb_backfill: bool = False          # LISA4_USE_DNB_BACKFILL — when FALSE (default) reserve_lisa4_pool fills
    #                                               from gmaps stock ONLY; the D&B/raghav critical-issue + no-website
    #                                               backfill steps are skipped (D&B reserved for Lisa-1/human calling)
    lisa4_from_numbers: str = "+61468030256"      # Lisa 4's caller ID (Twilio) — must be imported into Retell to dial
    lisa4_autodial_enabled: bool = False          # master gate for Lisa-4 auto-dialing (env fallback; DB toggle wins)
    lisa4_daily_target: int = 200                 # Lisa-4 calls per working day
    lisa4_per_number_daily_cap: int = 150         # max outbound calls per caller-ID per day (rotation pool hygiene)
    # --- Lisa-5: a second isolated cold-caller (cloned from Lisa-4's dialer; reads lisa5_pool). OFF by default. ---
    lisa5_agent_id: str = ""                       # Lisa 5 — Retell agent id (empty until configured)
    lisa5_from_numbers: str = ""                   # Lisa 5's caller ID(s) (Twilio, CSV E.164) — must be imported into Retell to dial
    lisa5_autodial_enabled: bool = False          # master gate for Lisa-5 auto-dialing (env fallback; DB toggle wins)
    lisa5_daily_target: int = 200                 # Lisa-5 calls per working day
    lisa5_per_number_daily_cap: int = 150         # max outbound calls per caller-ID per day (rotation pool hygiene)
    lisa_orb_agents: str = ""   # orb A/B picker whitelist "agent_id|Label,agent_id|Label" (empty = list all agents)
    lisa_from_numbers: str = ""                   # CSV of Lisa's Retell-registered caller numbers (E.164)
    lisa_session_minutes: int = 45               # length of the booked strategy session
    lisa_webhook_token: str = ""                 # shared secret guarding /api/lisa/postcall
    lisa_sms_enabled: bool = False               # send the minimal curiosity SMS on a missed call
    lisa_sms_from: str = ""                       # primary SMS-capable number to text from (e.g. 827)
    lisa_sms_numbers: str = ""                    # CSV of ALL SMS-capable numbers (backend picks one; e.g. 827,<new>)
    lisa_sms_agent_id: str = ""                   # (legacy) Retell chat agent — only used if Twilio isn't configured
    lisa_transfer_number: str = ""               # human closer's number for a live warm-transfer of a hot lead
    public_base_url: str = "https://www.trmatrix.com.au"  # base for public share links (audit view links texted to prospects)
    # SMS is a BACKEND process via Twilio direct (decoupled from the Retell voice agent → zero call-latency
    # impact; Lisa only talks). Set these to send SMS straight from Twilio instead of through Retell.
    twilio_account_sid: str = ""                 # Twilio Account SID (AC…) — used in the API URL
    twilio_auth_token: str = ""                   # Twilio Auth Token (used only if no API key is set)
    twilio_api_key_sid: str = ""                 # Twilio API Key SID (SK…) — preferred, revocable auth
    twilio_api_key_secret: str = ""              # Twilio API Key Secret (pairs with the API Key SID)
    twilio_messaging_service_sid: str = ""       # optional: Messaging Service SID (MG…); Twilio picks the number
    # Lisa is an isolated AI BDE with her OWN reserved pool + calendar + auto-dialer.
    lisa_pool_size: int = 500                    # how many GAds-confirmed prospects to reserve for Lisa
    lisa_daily_target: int = 50                  # calls Lisa places per working day
    lisa_retry_cadence_days: int = 3             # days between retry attempts on a no-answer
    lisa_retry_max_attempts: int = 4             # stop retrying after this many attempts
    reveal_closer_names: str = "Manoj"  # human closer(s) who run Lisa-4 website reveals via Aircall — auto reveal-tracking (REVEAL_CLOSER_NAMES)
    booking_qualifier_names: str = "Ben, Manoj"  # BDMs whose answered calls to BOOKED prospects auto-note the CRM (qualification tracking; BOOKING_QUALIFIER_NAMES)
    lisa_double_tap_hours: int = 2               # a no-answer gets one quick same-day retry after this many hours
    lisa_call_window_start: int = 9              # only auto-dial between these local hours (business hours)
    lisa_call_window_start_min: int = 0          # minute past the start HOUR to actually open (e.g. 30 => 8:30)
    lisa_call_window_end: int = 17
    lisa_max_concurrent: int = 1                 # natural dialing: ONE call at a time (no concurrency)
    lisa_min_call_gap_seconds: int = 0           # min gap between calls; 0 = auto (window / daily_target, ~8h/50=~9.6min)
    lisa_hos_heavy_interval_s: int = 180         # throttle heavy orchestration (reserve/coach/QA/enrich/schedule) to ~every N s so each 60s cycle is free to DIAL
    lisa_dm_resolve_per_cycle: int = 12          # DM resolves per heavy cycle (kept small so the heavy pass stays fast; dial-order stays ahead of dialing)
    lisa_autodial_enabled: bool = False          # MASTER GATE — no call fires until this is true
    lisa_coaching_enabled: bool = True           # AI Sales Coach: learn playbook from won/lost + QA each call
    lisa_audit_before_call: bool = True          # run the Digital-Marketing-Insight audit before each call (cached)

    # --- DataForSEO (SEO metrics + Google Ads Transparency Center; PAID, pay-per-request) ---
    # Auto-enriched for Raghav $1-10M paid-ads-gated prospects; on-demand for everyone else.
    dataforseo_login: str = ""        # account email
    dataforseo_password: str = ""     # API password OR the base64 "login:password" token (auto-detected)
    dataforseo_base: str = "https://api.dataforseo.com"
    dataforseo_location_code: int = 2036   # Australia
    dataforseo_language_code: str = "en"
    dataforseo_ads_recent_days: int = 45   # last_shown within N days => "currently running ads"

    # --- Marketing enrichment (per-domain, cached in the `enrichment` table) ---
    semrush_api_key: str = ""        # SEMRUSH_API_KEY
    apollo_api_key: str = ""         # APOLLO_API_KEY
    # Apollo guardrail: the client only ever calls organizations/enrich (free company
    # data). It NEVER calls people/match/search or sends reveal_* params (those burn
    # credits). apollo_enabled is a hard kill-switch; apollo_max_per_day caps lookups.
    apollo_enabled: bool = True
    apollo_max_per_day: int = 500
    apollo_paid_reveal: bool = False   # PAID: reveal top-DM email(sync)+phone(async) per prospect via domain (APOLLO_PAID_REVEAL)
    apollo_reveal_max_per_day: int = 300   # hard cap on paid reveals/day (credit safety)
    enrich_missing: bool = True      # run the enrich step in the pipeline
    enrich_workers: int = 4          # parallel domain lookups
    enrich_refresh_days: int = 30    # re-fetch a cached domain only if older than this

    # --- Dashboard auth / kiosk ---
    # CSV of manager logins (see everyone); seeded by `users-sync`.
    manager_emails: str = "raj@trafficradius.com.au"
    # If set, an always-on office TV can load /?tv=1&token=<KIOSK_TOKEN> for a
    # read-only ALL view without a personal login (sets a kiosk cookie).
    kiosk_token: str = ""

    # --- Behaviour ---
    backfill_start: str = ""  # 'YYYY-MM-DD' or blank => auto-detect
    daily_lookback_days: int = 3
    rpc_min_talk_seconds: int = 25
    # --- RPC Connect "Next Move" feedback (rpc.py) ---
    # Two missed MOBILE dials to the same number by the same BDE within this many
    # minutes counts as a completed "double tap" (the right-party-connect drill).
    rpc_double_tap_window_min: int = 120
    # A voicemail miss with at least this much talk time is treated as "voicemail left".
    rpc_voicemail_left_seconds: int = 10
    # How long an open, un-actioned RPC retry can sit before we flag the BDM.
    rpc_retry_notify_hours: int = 6
    # --- Smart next-call priority (next_call.py): score = intent x attention x revenue (Intent-led) ---
    next_call_w_intent: float = 0.5
    next_call_w_attention: float = 0.3
    next_call_w_revenue: float = 0.2
    next_call_revenue_cap_musd: float = 100.0   # revenue score saturates at this ($M USD)
    next_call_tier_hot: int = 70                 # score >= => 'hot'
    next_call_tier_warm: int = 40                # score >= => 'warm' (else 'cool')
    next_call_sync_interval_min: int = 10        # skip the recompute if a sync is fresher than this
    tz: str = "Australia/Melbourne"
    # Pipeline 2 (already-with-agency): default WEEKLY BDE rotation cadence. A
    # contract-end signal from the AI still overrides this per-prospect (don't pester
    # someone locked into a long contract). Env: PIPELINE2_DEFAULT_CADENCE_DAYS.
    pipeline2_default_cadence_days: int = 7
    # Weekly recall — 'call every week until we actually connect'. Covers un-connected prospects
    # in the active callback pipelines (P1 RPC-callback / P3 gatekeeper-callback) plus the P2
    # agency rotation, landing a dated event on the assigned BDE's calendar each week.
    weekly_recall_enabled: bool = True
    weekly_recall_cadence_days: int = 7          # days between recall attempts
    weekly_recall_max_weeks: int = 12            # stop auto-recalling an un-connected prospect after this
    weekly_recall_hour: int = 10                 # local hour to schedule the recall
    weekly_recall_horizon_days: int = 45         # only put a recall on the calendar if due within this
                                                 # many days (skip contract-parked far-future agency)
    # FREE website tracking-pixel scan: how many domains to scan each refresh cycle so
    # the whole DB gets paid-ads detection progressively (0 disables the in-loop scan).
    website_scan_per_cycle: int = 80
    website_scan_workers: int = 8

    # FREE WHOIS trickle: how many running-ads prospects to (re)lookup WHOIS for each refresh
    # cycle. auDA rate-limits .au WHOIS per IP, so this MUST be paced (sequential, small batch,
    # oldest-attempt first) — never bulk. Over a few hours it fills the whole running-ads set
    # without tripping the registry limit. 0 disables the in-loop trickle.
    whois_trickle_per_cycle: int = 12

    # Paced Apollo decision-maker (people) fill for running-ads prospects with missing/stale DMs.
    # Apollo rate-limits bursts, so a few per cycle (sequential, oldest-attempt first) fills the
    # gap over time from the always-on loop (fresh budget vs a one-off bulk run). 0 disables it.
    apollo_people_trickle_per_cycle: int = 12   # paid DM reveals per heavy cycle (throttled ~3min); daily cap still applies

    # GUARANTEE the fresh running-ads pool is fully enriched: each cycle, top up a few confirmed-ads
    # prospects that are still missing a free tab (website / Apollo org / business intel). Complements
    # the WHOIS + Apollo-people trickles above so every confirmed-ads prospect page & value ranking is
    # complete over time. Business-intel is one LLM call/domain, so keep this modest. 0 disables it.
    fresh_ads_enrich_per_cycle: int = 20
    fresh_ads_enrich_workers: int = 6

    # Names that are NOT cold-calling BDEs and must never be auto-assigned a calling worklist — e.g.
    # the BDM (Ben), who only does verification calls / oversight. CSV of names; matched to the roster
    # by canonical name. They still appear in analysis/reports (they do make some calls).
    non_calling_names: str = "Ben"

    # Private/isolated BDEs: their funnel data is hidden from every non-admin viewer (other BDEs, the
    # BDMs, managers, and the TV/kiosk) and excluded from the OVERALL/team totals, leaderboard, pickers
    # and reports. Only admins (Raj, Vysakh) and the private BDE themselves ever see it. CSV of the
    # canonical bde_name(s). Isolation is opt-in; empty = no private BDEs (unchanged behaviour).
    private_bde_names: str = ""

    # Names/emails granted FULL see+edit access to the booked-CRM (beyond admins) — e.g. Alfred the closer.
    # CSV of canonical bde_name(s) and/or login emails. CRM_ACCESS_NAMES.
    crm_access_names: str = "Alfred"

    # The whole CALLING system (calendar + call scheduling: fresh calls, RPC-connect callbacks,
    # gatekeeper/weekly recalls) operates ONLY on prospects CONFIRMED running Google Ads. The old
    # BDE-sourced (non-GAds) data is reached from the Agency & RPC page / pipeline pages instead.
    # Daily-call analysis, reports and the dashboard are UNAFFECTED (they read all calls). Flip off to
    # restore multi-source scheduling.
    calls_gads_only: bool = True

    # --- Retry calls (confirmed-Google-Ads prospects we dialed but never converted) ---
    # Prospects we called but didn't reach the DM / book / get a gatekeeper callback (no answer,
    # voicemail, or "not interested") aren't dropped — they go on a "retry" list and get re-called at a
    # DIFFERENT time of day (same BDE), until they pick up or hit retry_max_attempts (then marked dead).
    retry_enabled: bool = True
    retry_max_attempts: int = 5
    retry_cadence_days: int = 3          # min gap before the next retry
    retry_per_cycle: int = 4000          # cap events touched per refresh cycle

    # Reached-the-DM-but-no-next-step prospects (warm): keep re-calling with a DIFFERENT BDE (fresh
    # voice/line) + time until they show interest, up to reached_max_attempts. Each call carries the
    # full history so the new BDE isn't calling blind.
    reached_enabled: bool = True
    reached_max_attempts: int = 8

    # --- BDE daily calling worklist (AI allocation over the confirmed-Google-Ads pool) ---
    # Each BDE should have this many calls to make per day = their due follow-ups (callbacks/recalls)
    # PLUS fresh confirmed-ads prospects to top up. The allocator fills the remainder with fresh,
    # fully-enriched, value×90-day-performance-matched prospects. 0 disables the allocator.
    fresh_alloc_enabled: bool = True
    # Whole daily worklist per BDE = follow-ups (callbacks/recalls/retries/reached) FIRST, then fresh
    # tops up to this number. Pilot go-live target is 75/day (was 200); override with BDE_DAILY_CALL_TARGET.
    bde_daily_call_target: int = 75
    # Restrict every calling-worklist allocator (fresh / retry / reached + the agency rotation) to ONLY
    # these BDEs when set — used to pilot the GAds calendar & pool with one BDE before rolling out to the
    # whole team. Empty = allocate to every calling BDE (normal behaviour). CSV of canonical names.
    calendar_alloc_names: str = ""
    # Spread the fresh worklist across this many upcoming WORKING days at target/day/BDE (so the
    # calendar shows an even ~target×BDEs per day), instead of dumping it all on the next day. The
    # finite fresh pool naturally tapers off once exhausted.
    fresh_alloc_horizon_days: int = 14
    # empirical-Bayes shrink for 90-day BDE rates (higher = trust low-volume BDEs less; blend to mean).
    fresh_alloc_perf_shrink_k: int = 40

    # --- Roster in-scope rule ---
    # PRIMARY rule (preferred): BDE names. BDEs keep one stable name but rotate across
    # many extensions (mobile, landline, new numbers). Any 3CX line whose name matches
    # one of these is in-scope and rolls up under that BDE — so new numbers are captured
    # automatically and a person's lines are merged. e.g. "Bharat,Sunil,Syed,Dilip".
    roster_inscope_names: str = ""
    roster_inscope_groups: str = ""
    roster_inscope_extensions: str = ""
    roster_exclude_extensions: str = ""  # always out-of-scope, overrides everything (e.g. admins)
    # Merge a secondary extension into a primary BDE (e.g. a BDE's landline / 2nd
    # line -> their main line) so each person appears once with combined totals.
    # Format: "secondaryExt:primaryExt,secondaryExt:primaryExt"  e.g. "303:302,182:184,190:252".
    roster_merge_map: str = ""

    # --- WhatsApp nurturing (Meta Cloud API) — #13 ---
    # Business-initiated messages require PRE-APPROVED templates in Meta Business Manager.
    # Leave blank to run in DRY-RUN (the engine schedules + logs messages but doesn't send).
    whatsapp_enabled: bool = False
    whatsapp_phone_number_id: str = ""      # Meta WABA phone number ID
    whatsapp_access_token: str = ""         # permanent system-user access token
    whatsapp_api_version: str = "v21.0"
    # Approved template names for each step of the meeting-confirmation sequence.
    whatsapp_tpl_confirm: str = "meeting_confirmed"     # immediately on booking
    whatsapp_tpl_value: str = "meeting_value"           # what you'll get
    whatsapp_tpl_reminder: str = "meeting_reminder"     # +2h: authority/FOMO + confirm
    whatsapp_reschedule_url: str = ""                   # link sent if they don't confirm

    # --- Emma Collins (AI staff #3 — Meeting Scheduler) ---
    # Who may open Emma's console + APIs BESIDES admins (comma-separated login emails).
    # Admins always have access; this allow-list adds named non-admin users (Kiran is the
    # approver — NOTHING sends without a human clicking Approve & schedule).
    scheduler_users: str = ("kiran@trafficradius.com.au,vysakh@trafficradius.com.au,"
                            "raj@trafficradius.com.au")
    # Microsoft 365 / Graph app-only credentials for SENDING invites from the scheduler
    # mailbox (Emma Collins). The full Graph client is already built (emma.py) — these 4 env
    # values are ALL that's needed to go live. Until then approvals queue in emma_meetings as
    # 'approved-awaiting-creds' and are sent automatically by emma_tick when creds land.
    ms_tenant_id: str = ""        # MS_TENANT_ID
    ms_client_id: str = ""        # MS_CLIENT_ID
    ms_client_secret: str = ""    # MS_CLIENT_SECRET
    scheduler_mailbox: str = ""   # SCHEDULER_MAILBOX — e.g. emma.collins@trafficradius.com.au
    emma_default_duration_min: int = 45   # default meeting length for Emma's invites

    # --- Report email (optional) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    report_email_from: str = ""
    report_email_to: str = ""

    @field_validator("llm_provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"openai", "anthropic"}:
            raise ValueError("LLM_PROVIDER must be 'openai' or 'anthropic'")
        return v

    @field_validator("threecx_api_base")
    @classmethod
    def _normalize_threecx_base(cls, v: str) -> str:
        """Be forgiving about a mistyped base URL — a common deploy footgun.

        Repairs a missing or garbled scheme (e.g. a dropped leading character
        like 'ttps://host:5001', or no scheme at all like 'host:5001') to
        'https://...'. The 3CX Configuration API is HTTPS-only, so coercing to
        https is always correct here, and it prevents an obscure
        `UnsupportedProtocol` failure deep in the HTTP client at runtime.
        """
        import re

        v = (v or "").strip().rstrip("/")
        if not v or v.startswith(("http://", "https://")):
            return v
        v = re.sub(r"^[a-zA-Z]+://", "", v)  # strip any malformed scheme prefix
        return "https://" + v

    @property
    def aircall_enabled(self) -> bool:
        """Pull Aircall calls only when both credentials are present."""
        return bool(self.aircall_app_id and self.aircall_api_key)

    @property
    def dataforseo_enabled(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)

    @property
    def inscope_names(self) -> list[str]:
        return _csv(self.roster_inscope_names)

    @property
    def private_bdes(self) -> list[str]:
        """Canonical names of isolated BDEs (hidden from non-admins). See private_bde_names."""
        return _csv(self.private_bde_names)

    @property
    def calendar_alloc_bdes(self) -> list[str]:
        """If non-empty, the ONLY BDEs the calling allocators deal a worklist to. See calendar_alloc_names."""
        return _csv(self.calendar_alloc_names)

    @property
    def crm_access(self) -> list[str]:
        """Lower-cased names/emails with full booked-CRM see+edit access (beyond admins). See crm_access_names."""
        return [s.lower() for s in _csv(self.crm_access_names)]

    @property
    def lisa_numbers(self) -> list[str]:
        """Lisa's Retell-registered caller numbers (E.164), rotated across calls. From LISA_FROM_NUMBERS."""
        return _csv(self.lisa_from_numbers)

    @property
    def lisa4_numbers(self) -> list[str]:
        """Lisa 4's caller number(s) (E.164) — isolated from Lisa-1. From LISA4_FROM_NUMBERS."""
        return _csv(self.lisa4_from_numbers)

    @property
    def lisa5_numbers(self) -> list[str]:
        """Lisa 5's caller number(s) (E.164) — isolated from Lisa-1/4. From LISA5_FROM_NUMBERS."""
        return _csv(self.lisa5_from_numbers)

    @property
    def scheduler_user_emails(self) -> list[str]:
        """Lower-cased login emails allowed into Emma Collins' console (besides admins).
        From SCHEDULER_USERS (CSV; default kiran@ + vysakh@ + raj@trafficradius.com.au)."""
        return [e.lower() for e in _csv(self.scheduler_users)]

    @property
    def graph_configured(self) -> bool:
        """True once all Microsoft Graph credentials for invite-sending are present."""
        return bool(self.ms_tenant_id and self.ms_client_id
                    and self.ms_client_secret and self.scheduler_mailbox)

    @property
    def reveal_closers(self) -> list[str]:
        """Human closer(s) who run Lisa-4 website reveals (Aircall). Used to auto-mark booked reveals
        'revealed' in the CRM when they call the prospect. From REVEAL_CLOSER_NAMES."""
        return _csv(self.reveal_closer_names)

    @property
    def lisa_sms_number_list(self) -> list[str]:
        """All SMS-capable numbers (E.164). From LISA_SMS_NUMBERS, else falls back to LISA_SMS_FROM."""
        return _csv(self.lisa_sms_numbers) or ([self.lisa_sms_from] if self.lisa_sms_from else [])

    @property
    def aircall_agents(self) -> list[str]:
        """Names matched against Aircall users to decide in-scope agents. Dedicated list so
        Aircall works even when 3CX is scoped by GROUPS (inscope_names empty); falls back to
        inscope_names when AIRCALL_AGENT_NAMES isn't set (preserves single-config deployments)."""
        names = _csv(self.aircall_agent_names)
        return names if names else self.inscope_names

    @property
    def inscope_groups(self) -> list[str]:
        return _csv(self.roster_inscope_groups)

    @property
    def inscope_extensions(self) -> list[str]:
        return _csv(self.roster_inscope_extensions)

    @property
    def exclude_extensions(self) -> list[str]:
        return _csv(self.roster_exclude_extensions)

    @property
    def manager_email_list(self) -> list[str]:
        return _csv(self.manager_emails)

    @property
    def merge_map(self) -> dict[str, str]:
        """{secondary_extension: primary_extension} parsed from ROSTER_MERGE_MAP."""
        out: dict[str, str] = {}
        for pair in _csv(self.roster_merge_map):
            if ":" in pair:
                sec, prim = (p.strip() for p in pair.split(":", 1))
                if sec and prim:
                    out[sec] = prim
        return out

    @property
    def cdr(self) -> CdrSchema:
        return CdrSchema()

    @property
    def transcript(self) -> TranscriptSchema:
        return TranscriptSchema()


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
