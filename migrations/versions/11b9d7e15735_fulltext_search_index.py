"""fulltext search index

Revision ID: 11b9d7e15735
Revises: aeebd45ecf70
Create Date: 2026-07-29 11:02:28.354346

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '11b9d7e15735'
down_revision: Union[str, Sequence[str], None] = 'aeebd45ecf70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Index expressions must be immutable and cannot contain a subquery, which
# rules out unnesting the tags array. Casting the JSONB to text is immutable
# and good enough: to_tsvector treats the brackets, quotes and commas as
# separators, so ["Widow","Pension"] tokenises to the words we want.
FTS = """
    to_tsvector('english',
        coalesce(title, '') || ' ' ||
        coalesce(description, '') || ' ' ||
        coalesce(tags::text, ''))
"""


def upgrade() -> None:
    """Retrieval index for the assistant.

    Lexical rather than vector, deliberately. Citizens search for scheme names
    and benefit words that appear verbatim in the corpus, so lexical recall is
    strong here; and embedding 8,957 documents would need an embedding model
    resident in RAM on a 7.2 GB box that is already running the extraction
    pass. `service_records.embedding` stays in the schema for when that trade
    changes — this is not a decision against vectors, it is a decision about
    which one to build first.

    The tag array is folded into the document because myScheme's tags are the
    colloquial vocabulary ('Widow', 'Scholarship') that titles often omit.
    """
    op.execute(f"CREATE INDEX ix_cat_fts ON scheme_catalogue USING GIN ({FTS})")
    # Trigram index backs substring matching for partial words a citizen types
    # ('schol'), which to_tsquery cannot do on its own.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_cat_title_trgm ON scheme_catalogue "
               "USING GIN (title gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cat_title_trgm")
    op.execute("DROP INDEX IF EXISTS ix_cat_fts")
