"""Configuration and secret handling.

Everything secret comes from the environment. Nothing secret is ever logged:
data.gov.in requires its key as a URL query parameter, so any URL that reaches a
log line or an error message must go through `redact()` first.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SOURCES_DIR = ROOT / "sources"


def _load_dotenv() -> None:
    """Minimal .env loader — avoids a dependency for six variables."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

# Anything that looks like a credential, wherever it appears in a URL or message.
_SECRET_PATTERNS = [
    re.compile(r"(api[-_]?key=)[^&\s]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
    re.compile(r"(sk-[A-Za-z0-9\-]{8,})"),
]


def redact(text: str) -> str:
    """Strip credentials from a string before it is logged or surfaced."""
    if not text:
        return text
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(
            lambda m: (m.group(1) + "***") if m.lastindex else "***", out
        )
    return out


@dataclass(frozen=True)
class Settings:
    data_gov_api_key: str = os.environ.get("DATA_GOV_API_KEY", "")
    openrouter_api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
    crawler_contact: str = os.environ.get(
        "CRAWLER_CONTACT", "https://example.invalid/askbharat"
    )
    database_url: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://askbharat:askbharat@localhost:5433/askbharat",
    )

    @property
    def user_agent(self) -> str:
        """Identify the crawler honestly, with a contact route."""
        return f"askbharat-research/0.1 (+{self.crawler_contact})"

    def require(self, name: str) -> str:
        value = getattr(self, name, "")
        if not value:
            raise RuntimeError(
                f"{name.upper()} is not set. Copy .env.example to .env and fill it in."
            )
        return value


settings = Settings()
