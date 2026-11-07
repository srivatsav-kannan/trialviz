"""Service configuration, read from the environment and an optional `.env` file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = Field(
        None, description="Enables the Claude interpreter. Without it, the rule-based one is used."
    )
    interpreter_model: str = "claude-opus-5-5"
    interpreter_effort: Literal["low", "medium", "high"] = "low"
    interpreter_max_tokens: int = Field(8000, ge=1000, le=32000)
    llm_spend_cap_usd: float = Field(20.0, ge=0)
    llm_ledger_path: Path = Path("var/llm_spend.json")
