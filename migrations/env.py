"""Alembic environment.

The URL comes from `askbharat.config.settings`, never from alembic.ini — the
password lives in .env and must not be committed. `pgvector.sqlalchemy` is
imported for its side effect of registering the `vector` type so autogenerate
can render `service_records.embedding`.
"""
from logging.config import fileConfig

import pgvector.sqlalchemy  # noqa: F401  (registers the vector type)
from alembic import context
from sqlalchemy import engine_from_config, pool

from askbharat.config import settings
from askbharat.db.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Expression and operator-class indexes that Alembic cannot compare reliably.
# It cannot match a `text()` index expression in the metadata against the one
# Postgres reports, so every autogenerate proposes dropping and recreating them
# — and an earlier run did exactly that, silently removing the full-text
# indexes while adding an unrelated column. They are declared in models.py for
# humans and skipped here for the comparator.
UNCOMPARABLE_INDEXES = {"ix_cat_fts", "ix_cat_title_trgm", "ix_cat_embedding",
                        "ix_svc_embedding"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "index" and name in UNCOMPARABLE_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
