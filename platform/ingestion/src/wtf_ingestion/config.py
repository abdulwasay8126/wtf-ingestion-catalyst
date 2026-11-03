from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WTF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://wtf@127.0.0.1:55433/wtf_ingestion"
    output_dir: Path = Path("outputs/publications")
    raw_dir: Path = Path("raw")
    log_level: str = "INFO"
    sec_user_agent: str | None = None
    enable_live_sec: bool = False
    enable_live_ir: bool = False
    enable_sec_estimates: bool = False
    sec_force_recheck: bool = False
    enable_sec_ocr: bool = False
    sec_metadata_workers: int = Field(default=1, ge=1, le=4)
    # SEC's ceiling is 10/sec across clients; retain headroom and one serial worker.
    sec_requests_per_second: float = Field(default=8.0, ge=0.5, le=8.0)
    dashboard_mode: Literal["fixture", "live"] = "fixture"
    history_retention_years: int = Field(default=2, ge=2, le=2)
    universe_file: Path | None = None
    request_timeout_seconds: float = Field(default=20.0, ge=1, le=120)
    max_response_bytes: int = Field(default=12_000_000, ge=1_000, le=50_000_000)
    publication_horizon_days: int = Field(default=90, ge=1, le=366)
    freshness_hours: int = Field(default=36, ge=1, le=168)
    sec_metadata_poll_minutes: int = Field(default=0, ge=0, le=1440)
    earnings_update_target_minutes: int = Field(default=15, ge=1, le=1440)
    enable_live_alpaca: bool = False
    alpaca_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    alpaca_api_secret: SecretStr | None = Field(default=None, exclude=True, repr=False)
    alpaca_rights_reference: str | None = None
    alpaca_rights_datasets: list[Literal["prices", "actions", "news", "calendar"]] = Field(
        default_factory=list
    )
    alpaca_history_days: int = Field(default=124, ge=7, le=366)
    alpaca_freshness_hours: int = Field(default=36, ge=1, le=168)
    alpaca_max_pages: int = Field(default=60, ge=1, le=200)

    def assert_live_alpaca_ready(self) -> None:
        if not self.enable_live_alpaca:
            raise RuntimeError("Alpaca collection is disabled.")
        if self.dashboard_mode != "live":
            raise RuntimeError("Live Alpaca collection requires the live publication namespace.")
        if not self.alpaca_rights_reference or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{3,120}", self.alpaca_rights_reference
        ):
            raise RuntimeError("A documented Alpaca company-use rights reference is required.")
        if set(self.alpaca_rights_datasets) != {"prices", "actions", "news", "calendar"}:
            raise RuntimeError("Alpaca retention/display rights must cover every enabled dataset.")
        if not self.alpaca_api_key or not self.alpaca_api_secret:
            raise RuntimeError("Alpaca credentials are required in runtime secrets.")

    @field_validator("sec_user_agent")
    @classmethod
    def reject_placeholder_user_agent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        forbidden = {"change-me", "example@example.com", "company contact@example.com"}
        if not cleaned or cleaned.lower() in forbidden:
            raise ValueError("SEC User-Agent must be an approved real organization/contact value")
        if not re.fullmatch(
            r"[A-Za-z0-9 ._-]{3,100} [A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", cleaned
        ):
            raise ValueError("SEC User-Agent requires an organization and monitored contact email")
        return cleaned

    def assert_live_sec_ready(self) -> None:
        if not self.enable_live_sec:
            raise RuntimeError("Live SEC access is disabled; fixtures remain available")
        if not self.sec_user_agent:
            raise RuntimeError("WTF_SEC_USER_AGENT is required before live SEC access")

    def assert_live_ir_ready(self) -> None:
        if not self.enable_live_ir:
            raise RuntimeError(
                "Live issuer-IR access is disabled pending per-host permission records"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
