"""Application settings loaded from environment / .env"""
from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # "sqlite" keeps the prototype self-contained; PostgreSQL is the production target.
    app_name: str = "FDE Platform"
    database_url: str = "sqlite:///./fde.db"

    # ---- Business policy (MUST be configured before production use) ----
    # Approval thresholds. Empty/None => policy is NOT configured; the
    # corresponding automated decisions stay disabled instead of guessing.
    approval_threshold_1: float | None = None   # below: project manager may approve
    approval_threshold_2: float | None = None   # between: PM + GM ; above: GM + finance

    # Match tolerances (percent). Defaults are safe/strict.
    price_tolerance_pct: float = 0.0
    tax_tolerance_pct: float = 0.5
    rounding_tolerance: float = 0.05

    # Duplicate-request window (days) for material requests
    duplicate_window_days: int = 7

    # Phase-3 costing policy (unset => labor cost recorded as 0 and flagged)
    default_labor_rate: float | None = None      # SAR per hour, fallback rate
    overhead_pct: float | None = None             # % applied on materials+labor

    # ---- Integrations ----
    whatsapp_verify_token: str = ""
    whatsapp_webhook_secret: str = ""   # if empty in dev mode, verification is skipped
    erp_webhook_secret: str = ""       # required for ERP posting callbacks
    erp_connector: str = "export"        # "export" (safe bundle) | "odoo" | custom

    # Minimal number of quotations required before GM approval (single-supplier
    # requires a documented exception approval instead)
    min_quotations: int = 2

    dev_mode: bool = True   # enables seeded dev users (X-User auth)


@lru_cache
def get_settings() -> Settings:
    return Settings()
