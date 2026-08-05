"""Source registry: load the YAML plan and sync it into the database."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from askbharat.config import SOURCES_DIR
from askbharat.db.models import Source
from askbharat.db.session import session_scope


@dataclass
class SourceSpec:
    slug: str
    name: str
    base_url: str
    adapter: str
    tier: int = 2
    jurisdiction_level: str = "central"
    state: str | None = None
    refresh_days: int = 30
    notes: str = ""
    extra: dict = field(default_factory=dict)


def load_specs(path=None) -> list[SourceSpec]:
    path = path or SOURCES_DIR / "registry.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = []
    for entry in raw.get("sources", []):
        known = {
            k: entry[k]
            for k in (
                "slug", "name", "base_url", "adapter", "tier",
                "jurisdiction_level", "state", "refresh_days", "notes",
            )
            if k in entry
        }
        extra = {k: v for k, v in entry.items() if k not in known}
        specs.append(SourceSpec(**known, extra=extra))
    return specs


def sync_to_db(specs: list[SourceSpec] | None = None) -> tuple[int, int]:
    """Upsert registry entries. Never clears audit fields — those are earned."""
    specs = specs or load_specs()
    created = updated = 0
    with session_scope() as s:
        for spec in specs:
            row = s.query(Source).filter_by(slug=spec.slug).one_or_none()
            if row is None:
                row = Source(slug=spec.slug)
                s.add(row)
                created += 1
            else:
                updated += 1
            row.name = spec.name
            row.base_url = spec.base_url
            row.adapter = spec.adapter
            row.tier = spec.tier
            row.jurisdiction_level = spec.jurisdiction_level
            row.state = spec.state
            row.refresh_days = spec.refresh_days
            if spec.notes and not row.audit_notes:
                row.audit_notes = spec.notes.strip()
    return created, updated
