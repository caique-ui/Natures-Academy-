"""
ragbot/migrations/0002_pgvector_embedding.py

OPTIONAL migration for pgvector ANN support.
Safely skips itself if the pgvector extension is not installed in PostgreSQL.

NOTE: The embedding_vec column is populated at ingest time going forward.
There is no backfill from the old binary column because bytea cannot be
directly cast to vector in SQL — and if you're applying this migration
before any ingestion has run, the table is empty anyway.

To enable pgvector:
  1. In psql:  CREATE EXTENSION IF NOT EXISTS vector;
  2. pip install pgvector django-pgvector
  3. Add 'pgvector.django' to INSTALLED_APPS
  4. Set VECTORSTORE_USE_PGVECTOR = True in settings.py
  5. python manage.py migrate
"""

from django.db import migrations, connection


def _pgvector_available():
    """Return True only if the 'vector' type exists in PostgreSQL."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_type WHERE typname = 'vector' LIMIT 1;"
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


def add_vector_column(apps, schema_editor):
    if not _pgvector_available():
        print(
            "\n  ⚠️  pgvector extension not found — skipping 0002 migration. "
            "Run `CREATE EXTENSION vector;` in psql first if you want it."
        )
        return

    # Add the vector column (nullable — populated at ingest time)
    schema_editor.execute("""
        ALTER TABLE ragbot_documentchunk
        ADD COLUMN IF NOT EXISTS embedding_vec vector(1536);
    """)

    # Create IVFFlat index for cosine similarity search
    schema_editor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunk_embedding_ivfflat
        ON ragbot_documentchunk
        USING ivfflat (embedding_vec vector_cosine_ops)
        WITH (lists = 100);
    """)

    print("\n  ✅ pgvector column and IVFFlat index created.")


def remove_vector_column(apps, schema_editor):
    if not _pgvector_available():
        return
    schema_editor.execute(
        "DROP INDEX IF EXISTS idx_chunk_embedding_ivfflat;"
    )
    schema_editor.execute(
        "ALTER TABLE ragbot_documentchunk DROP COLUMN IF EXISTS embedding_vec;"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ragbot", "0001_vectorstore_db"),
    ]

    operations = [
        migrations.RunPython(add_vector_column, remove_vector_column),
    ]