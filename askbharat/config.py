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

    # "development" or "production". Only ever widens what is checked and
    # narrows what is exposed — never changes what the site says about a
    # scheme. A citizen must not get a different answer per environment.
    app_env: str = os.environ.get("APP_ENV", "development")

    # Bind address. The default is loopback so a dev run is not silently
    # reachable from the network; a container overrides it to 0.0.0.0, which
    # is safe there because the publish rule decides real exposure.
    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = int(os.environ.get("PORT", "8077"))

    # One worker by default and that is a deliberate ceiling, not laziness:
    # each worker loads its own copy of the bi-encoder and cross-encoder,
    # ~1.1 GB resident apiece. Two workers do not fit on a 7.2 GB box beside
    # Postgres. Raise it only where the RAM is actually there.
    workers: int = int(os.environ.get("WEB_CONCURRENCY", "1"))

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

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

    def check_production_ready(self) -> list[str]:
        """Problems that should stop a production boot. Empty list means go.

        Returned rather than raised so the caller can report every problem at
        once. Finding out about the second missing variable only after fixing
        the first is a bad way to spend a deploy window.

        The assistant's API key is intentionally *not* required. Browsing,
        search and every scheme page work without it — that is the whole point
        of the catalogue/extraction split — so a missing key degrades the site
        to read-only rather than refusing to start.
        """
        problems: list[str] = []
        if not self.is_production:
            return problems

        url = self.database_url
        if "localhost" in url or "127.0.0.1" in url:
            problems.append(
                "DATABASE_URL points at localhost, which in a container is the "
                "container itself, not the database host."
            )
        if "askbharat:askbharat@" in url:
            problems.append(
                "DATABASE_URL still carries the development password. Set "
                "POSTGRES_PASSWORD to something generated."
            )
        if self.crawler_contact.startswith("https://example.invalid"):
            problems.append(
                "CRAWLER_CONTACT is still the placeholder. It is the address "
                "sites we crawl use to reach a human; it must be real."
            )
        return problems


settings = Settings()
