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

    # --- Behaviour ---
    backfill_start: str = ""  # 'YYYY-MM-DD' or blank => auto-detect
    daily_lookback_days: int = 3
    rpc_min_talk_seconds: int = 25
    tz: str = "Australia/Melbourne"

    # --- Roster in-scope rule ---
    roster_inscope_groups: str = ""
    roster_inscope_extensions: str = ""

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

    @property
    def inscope_groups(self) -> list[str]:
        return _csv(self.roster_inscope_groups)

    @property
    def inscope_extensions(self) -> list[str]:
        return _csv(self.roster_inscope_extensions)

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
