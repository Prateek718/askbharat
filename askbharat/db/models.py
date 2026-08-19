"""Database schema.

Design notes:

- `raw_documents` is append-only and content-addressed. It is the citation
  substrate: when the answer engine cites a page, it cites the bytes we actually
  read, and can still show them after the source changes or dies. Re-extraction
  never needs a re-crawl.
- `extraction_queue` is a durable work queue rather than an in-memory job,
  because OpenRouter's free tier caps us at 1000 requests/day — extraction runs
  for days and must survive restarts without duplicating work.
- `service_records` keeps the canonical record as JSONB plus lifted columns for
  the dimensions we filter on. Jurisdiction is a first-class column, not a JSON
  path, because state filtering is the core feature.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, list: JSONB}


class Source(Base):
    """A registry entry — one crawlable source, with its licence and rules."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    base_url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(32))          # api | rendered | static
    tier: Mapped[int] = mapped_column(Integer, default=2)     # 1 = highest demand
    jurisdiction_level: Mapped[str] = mapped_column(String(16), default="central")
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Filled by the Phase 0.2 audit. Crawling is gated on these being present.
    licence: Mapped[str | None] = mapped_column(String(128), nullable=True)
    licence_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    robots_crawl_delay: Mapped[float | None] = mapped_column(Float, nullable=True)
    robots_allows: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    audit_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_days: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list[RawDocument]] = relationship(back_populates="source")

    @property
    def crawlable(self) -> bool:
        """Gate: never crawl a source we haven't cleared."""
        return bool(self.active and self.audited_at and self.robots_allows)


class RawDocument(Base):
    """An immutable snapshot of one fetched URL."""

    __tablename__ = "raw_documents"
    __table_args__ = (
        # Same URL + same bytes = same row. Re-fetching unchanged content is a
        # no-op, which is what makes the crawl cheap to repeat.
        UniqueConstraint("url", "content_hash", name="uq_raw_url_hash"),
        Index("ix_raw_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_path: Mapped[str] = mapped_column(Text)      # on disk; DB stays small
    body_bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(default=dict)  # adapter-specific extras
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[Source | None] = relationship(back_populates="documents")


class ExtractionTask(Base):
    """Durable work queue for LLM extraction.

    States: pending -> in_flight -> done | failed | parked
    `parked` means schema validation failed twice and a human should look.
    """

    __tablename__ = "extraction_queue"
    __table_args__ = (
        UniqueConstraint("raw_document_id", name="uq_extract_doc"),
        Index("ix_extract_claim", "status", "tier", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_document_id: Mapped[int] = mapped_column(ForeignKey("raw_documents.id"))
    tier: Mapped[int] = mapped_column(Integer, default=2, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServiceRecordRow(Base):
    """The canonical record, persisted.

    JSONB payload for the full ServiceRecord; lifted columns for what we filter
    and rank on. `embedding` is nullable so records exist before indexing.
    """

    __tablename__ = "service_records"
    __table_args__ = (
        Index("ix_svc_jurisdiction", "jurisdiction_level", "state"),
        Index("ix_svc_kind_tier", "kind", "confidence"),
        Index(
            "ix_svc_embedding", "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)

    # First-class, not a JSON path — state filtering is the core feature.
    jurisdiction_level: Mapped[str] = mapped_column(String(16), default="central")
    state: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    ministry: Mapped[str | None] = mapped_column(Text, nullable=True)
    life_events: Mapped[list] = mapped_column(default=list)
    payload: Mapped[dict] = mapped_column()          # full ServiceRecord
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    link_status: Mapped[str] = mapped_column(String(24), default="unchecked", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 384 dims, matching `scheme_catalogue.embedding` and the encoder actually
    # in use. It was 1024 — sized for a large model that was never chosen —
    # which made it silently unusable: a vector written by the 384-dim encoder
    # cannot be stored here, so the column could only ever stay empty.
    #
    # This is not a duplicate of the catalogue vector. They index different
    # text and answer different questions:
    #
    #   scheme_catalogue.embedding  title + description + tags
    #                               -> "what is this scheme called, roughly"
    #                               -> complete today, for all 4,810
    #   service_records.embedding   eligibility + documents + how to apply
    #                               -> "which schemes have rules like mine"
    #                               -> arrives as extraction lands
    #
    # The second is what lets "I have a BPL card and two acres" match on the
    # eligibility *rules* rather than on the scheme's name.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)


class SchemeCatalogue(Base):
    """myScheme's own catalogue metadata, kept separate from what we extract.

    This is the source's structured data — category, tags, ministry, title —
    delivered by its search API rather than inferred by a model. It is separate
    from `service_records` on purpose:

    - It is complete *now*: title, description, category and tags are populated
      on 100% of rows, so the site's browse, search and filter layer works
      before a single LLM call has run.
    - Provenance stays legible. An LLM pass can never silently overwrite what
      the government actually published, and a bad extraction can be rebuilt
      without re-fetching the catalogue.

    Joined to `service_records` by id = 'myscheme:' || slug.
    """

    __tablename__ = "scheme_catalogue"
    __table_args__ = (
        Index("ix_cat_ministry", "ministry"),
        Index("ix_cat_state", "state"),
        # The retrieval indexes are declared here, not only in the migration
        # that created them. Autogenerate compares the live database against
        # *this* metadata, so an index it cannot see is an index it proposes to
        # drop — which is exactly what happened: adding the embedding column
        # generated a migration that silently removed both of these, turning
        # every full-text query back into a sequential scan.
        Index(
            "ix_cat_fts",
            text("to_tsvector('english', coalesce(title,'') || ' ' || "
                 "coalesce(description,'') || ' ' || coalesce(tags::text,''))"),
            postgresql_using="gin",
        ),
        Index(
            "ix_cat_title_trgm", "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_cat_embedding", "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    slug: Mapped[str] = mapped_column(String(160), primary_key=True)
    title: Mapped[str] = mapped_column(Text, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    categories: Mapped[list] = mapped_column(default=list)
    tags: Mapped[list] = mapped_column(default=list)
    ministry: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Present in the API payload but empty on every row we pulled; kept so a
    # later backfill has somewhere to land.
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 384 dims, matching multilingual-e5-small. Sized to fit comfortably in
    # memory during runtime and extraction.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)


class DirectoryRow(Base):
    """A row from an approved data.gov.in directory dataset."""

    __tablename__ = "directory_records"
    __table_args__ = (
        Index("ix_dir_geo", "state", "district"),
        Index("ix_dir_pincode", "pincode"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(64), index=True)
    dataset_title: Mapped[str] = mapped_column(Text)
    row: Mapped[dict] = mapped_column()
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    district: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(12), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkCheck(Base):
    """Standing link-verification results.

    Separate from service_records because rot is measured continuously and we
    want the history — a link that flaps is a different problem from one that
    died, and the audit showed 39.6% of destinations are broken.
    """

    __tablename__ = "link_checks"
    __table_args__ = (Index("ix_link_url_time", "url", "checked_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True only after retry-verification — the naive single pass reported 47.2%
    # rot where the verified figure is 39.6%.
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
