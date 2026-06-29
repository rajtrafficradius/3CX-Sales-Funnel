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
    # Auto-enabled when both are set; only in-scope agents' calls (matched by name against
    # ROSTER_INSCOPE_NAMES) are ingested, so non-BDE Aircall users are ignored.
    aircall_app_id: str = ""
    aircall_api_key: str = ""
    aircall_base: str = "https://api.aircall.io/v1"
    aircall_page_size: int = 50  # Aircall's max per_page

    # --- Marketing enrichment (per-domain, cached in the `enrichment` table) ---
    semrush_api_key: str = ""        # SEMRUSH_API_KEY
    apollo_api_key: str = ""         # APOLLO_API_KEY
    # Apollo guardrail: the client only ever calls organizations/enrich (free company
    # data). It NEVER calls people/match/search or sends reveal_* params (those burn
    # credits). apollo_enabled is a hard kill-switch; apollo_max_per_day caps lookups.
    apollo_enabled: bool = True
    apollo_max_per_day: int = 500
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
    tz: str = "Australia/Melbourne"
    # Pipeline 2 (already-with-agency): default WEEKLY BDE rotation cadence. A
    # contract-end signal from the AI still overrides this per-prospect (don't pester
    # someone locked into a long contract). Env: PIPELINE2_DEFAULT_CADENCE_DAYS.
    pipeline2_default_cadence_days: int = 7
    # FREE website tracking-pixel scan: how many domains to scan each refresh cycle so
    # the whole DB gets paid-ads detection progressively (0 disables the in-loop scan).
    website_scan_per_cycle: int = 80
    website_scan_workers: int = 8

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
    def inscope_names(self) -> list[str]:
        return _csv(self.roster_inscope_names)

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
