"""Service configuration, read from the environment and an optional `.env` file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = Field(
        None, description="Enables the Claude interpreter. Without it, the rule-based one is used."
    )
    interpreter_model: str = "claude-opus-5-5"
    interpreter_effort: Literal["low", "medium", "high"] = "low"
    interpreter_max_tokens: int = Field(8000, ge=1000, le=32000)
    llm_spend_cap_usd: float = Field(20.0, ge=0, description="Hard cap on total Claude spending.")
    llm_daily_cap_usd: float | None = Field(
        None, ge=0, description="Optional cap on Claude spending per UTC day. Empty means none."
    )
    llm_ledger_path: Path = Path("var/llm_spend.json")
    database_url: SecretStr | None = Field(
        None,
        description="Postgres for the spend ledger. Set on a deployed service, whose disk is "
        "wiped on restart; without it, spending is kept in llm_ledger_path.",
    )

    ctgov_api_root: str = "https://clinicaltrials.gov/api/v2"
    ctgov_requests_per_minute: float = Field(
        50.0, gt=0, description="Request spacing. The API throttles bursts of about ten."
    )
    ctgov_timeout_seconds: float = Field(30.0, gt=0)
    ctgov_cache_dir: Path | None = Field(
        Path("var/cache/ctgov"), description="Response cache. Set empty to disable."
    )
    retrieval_max_records: int = Field(
        10_000, ge=1, description="Most records fetched per cohort before truncating."
    )
    answer_deadline_seconds: float = Field(
        180.0, gt=0, description="Longest an answer may spend on registry requests."
    )

    questions_per_minute: int = Field(
        10, ge=1, description="Questions one client may ask per minute (/v1/query, /v1/interpret)."
    )
    questions_per_hour: int = Field(60, ge=1, description="Questions one client may ask per hour.")
    questions_per_hour_total: int = Field(
        600, ge=1, description="Questions all clients together may ask per hour."
    )
    requests_per_minute: int = Field(
        120, ge=1, description="Other requests one client may make per minute."
    )
    max_concurrent_answers: int = Field(
        4, ge=1, description="Questions answered at once; more get a 503 to retry shortly."
    )
    max_body_bytes: int = Field(16_384, ge=1024, description="Largest request body accepted.")
    trusted_proxy_hops: int = Field(
        0,
        ge=0,
        description="Proxies in front of the service. With 1, the client is the last address in "
        "X-Forwarded-For, the one the proxy added, which a client cannot forge.",
    )

    @field_validator("llm_daily_cap_usd", mode="before")
    @classmethod
    def _empty_means_no_daily_cap(cls, value: object) -> object:
        # A host may not let a variable be deleted, only emptied.
        return None if isinstance(value, str) and not value.strip() else value
