"""The canonical record.

This schema is the product. Everything upstream feeds it; everything downstream
reads it. Two invariants matter more than the field list:

1. **Every content field is nullable, and null is a first-class answer.** The
   answer engine's abstention behaviour depends on being able to distinguish
   "the source doesn't say" from "nobody extracted it yet". Extraction is
   instructed to prefer null over inference; a plausible-looking guess is worse
   than a gap, because a gap can be surfaced honestly to the citizen.

2. **Provenance is per-field, not per-record.** A record assembled from myScheme
   plus a state portal plus a PDF must be able to cite the specific source for
   the fee it quotes. Without that, citations are decorative.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, model_validator


class Kind(StrEnum):
    SERVICE = "service"
    SCHEME = "scheme"
    DOCUMENT = "document"
    CERTIFICATE = "certificate"
    GRIEVANCE = "grievance"


class Level(StrEnum):
    CENTRAL = "central"
    STATE = "state"
    DISTRICT = "district"


class ApplicationMode(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    CSC = "csc"          # Common Service Centre
    POST = "post"


class LinkStatus(StrEnum):
    OK = "ok"
    TLS_BROKEN = "tls_broken"
    DEAD_DOMAIN = "dead_domain"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    UNCHECKED = "unchecked"


class Jurisdiction(BaseModel):
    level: Level
    # None for central; the state/UT name for anything state-delivered.
    state: str | None = None

    @model_validator(mode="after")
    def _state_required_below_central(self):
        if self.level in (Level.STATE, Level.DISTRICT) and not self.state:
            raise ValueError(f"jurisdiction.level={self.level} requires a state")
        return self


class Owner(BaseModel):
    ministry: str | None = None
    department: str | None = None
    agency: str | None = None


class RequiredDocument(BaseModel):
    name: str
    mandatory: bool | None = None      # None = source didn't say
    notes: str | None = None
    accepted_alternatives: list[str] = Field(default_factory=list)


class Fees(BaseModel):
    amount: float | None = None
    currency: str = "INR"
    varies: bool = False               # true when it depends on category/state/urgency
    waivers: list[str] = Field(default_factory=list)
    notes: str | None = None


class Step(BaseModel):
    order: int
    instruction: str
    url: HttpUrl | None = None


class HowToApply(BaseModel):
    modes: list[ApplicationMode] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    online_url: HttpUrl | None = None
    offline_where: str | None = None


class Eligibility(BaseModel):
    """Facet-shaped where the source states it; prose otherwise.

    The structured half mirrors the 22-facet index the portals already hold but
    never surface (see SCRAPE_REPORT.md §5) — that alignment is what lets us
    filter by the dimensions citizens actually differ on.
    """
    prose: str | None = None
    min_age: int | None = None
    max_age: int | None = None
    gender: list[str] = Field(default_factory=list)
    caste: list[str] = Field(default_factory=list)
    income_ceiling: float | None = None
    bpl_only: bool | None = None
    minority_only: bool | None = None
    disability_required: bool | None = None
    residence: str | None = None        # rural | urban | both
    occupation: list[str] = Field(default_factory=list)
    student_only: bool | None = None
    marital_status: list[str] = Field(default_factory=list)


class Source(BaseModel):
    url: HttpUrl
    fetched_at: datetime
    http_status: int | None = None
    content_hash: str | None = None
    snapshot_path: str | None = None    # local copy — the citation substrate
    licence: str | None = None


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class ServiceRecord(BaseModel):
    """One government service, scheme, or document — unified across silos."""

    id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    # What citizens actually call it: "RC book", "ration card", "khatauni".
    # This is what makes colloquial queries resolve.
    colloquial_names: list[str] = Field(default_factory=list)

    kind: Kind
    jurisdiction: Jurisdiction
    owner: Owner = Field(default_factory=Owner)

    # The unification key. "someone died" pulls together death registration,
    # pension transfer, succession certificate, and bank formalities — records
    # that live in four different silos today.
    life_events: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    what_it_is: str | None = None
    who_is_eligible: Eligibility | None = None
    documents_required: list[RequiredDocument] = Field(default_factory=list)
    fees: Fees | None = None
    how_to_apply: HowToApply | None = None
    processing_time: str | None = None
    validity: str | None = None
    renewal: str | None = None

    # The thing no portal writes down and every citizen needs: common rejection
    # reasons, the step people get stuck on, what to do when it stalls.
    what_can_go_wrong: list[str] = Field(default_factory=list)

    helpline: str | None = None
    grievance_route: str | None = None

    sources: list[Source] = Field(default_factory=list)
    # field name -> index into `sources`. Written by extraction, read by the
    # answer engine when it builds a citation.
    field_provenance: dict[str, int] = Field(default_factory=dict)

    confidence: Confidence = 0.0
    last_verified_at: datetime | None = None
    link_status: LinkStatus = LinkStatus.UNCHECKED

    @model_validator(mode="after")
    def _provenance_points_at_real_sources(self):
        n = len(self.sources)
        bad = {f: i for f, i in self.field_provenance.items() if not 0 <= i < n}
        if bad:
            raise ValueError(
                f"field_provenance references source indices outside 0..{n - 1}: {bad}"
            )
        return self

    def cited_source(self, field: str) -> Source | None:
        """The specific source backing one field, for citation rendering."""
        idx = self.field_provenance.get(field)
        return self.sources[idx] if idx is not None else None

    def is_answerable(self, field: str) -> bool:
        """Whether the answer engine may speak to this field.

        A field is answerable only if it is populated *and* attributable. An
        unattributed value cannot be cited, so it is treated as absent.
        """
        value = getattr(self, field, None)
        if value in (None, [], {}, ""):
            return False
        return field in self.field_provenance


class DirectoryRecord(BaseModel):
    """A row from a data.gov.in directory dataset.

    Deliberately separate from ServiceRecord: these answer "where / which"
    questions (nearest passport office, hospitals in a district, pincode
    lookup) via structured filtering, not prose RAG. Keeping the native schema
    means we don't flatten useful columns into text.
    """

    id: str
    dataset_id: str                     # data.gov.in index_name
    dataset_title: str
    row: dict[str, str | float | int | None]

    # Lifted out of `row` when present, so they can be indexed and filtered.
    state: str | None = None
    district: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    source: Source
