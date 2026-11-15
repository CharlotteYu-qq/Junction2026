"""Settings from .env (copy .env.example; never commit real keys)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TZ = ZoneInfo("Europe/Helsinki")


@dataclass(frozen=True)
class Settings:
    aimo_api_key: str = os.getenv("AIMO_API_KEY", "")
    aimo_api_base_url: str = os.getenv("AIMO_API_BASE_URL", "") or "https://junction26.valioaimo.fi"
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "") or "claude-opus-5-5"
    parser: str = os.getenv("PARSER", "auto")  # auto | rules | claude
    offer_ttl_minutes: int = int(os.getenv("OFFER_TTL_MINUTES", "") or 30)
    # Sign-in password for the buyer portal (falls back to DEMO_PASSWORD from older deployments).
    # Without one (local development), signing in needs no password.
    buyer_password: str = os.getenv("BUYER_PASSWORD", "") or os.getenv("DEMO_PASSWORD", "")
    # Public demo: show the password on the sign-in page (greyed in the field) so visitors can try the portal.
    show_buyer_password: bool = os.getenv("SHOW_BUYER_PASSWORD", "").strip().lower() in ("1", "true", "yes")
    valio_dir: Path = field(default=ROOT / "data" / "valio")
    runtime_dir: Path = field(default=ROOT / "data" / "runtime")
    ui_dir: Path = field(default=ROOT / "ui")
    history_dir: Path = field(default=ROOT / "data" / "Userdata" / "order_history")  # synthetic order history


settings = Settings()
