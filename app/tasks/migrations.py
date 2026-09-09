"""Small forward-only schema migrations for installations without Alembic."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def migrate_v73_classified_admission(conn: AsyncConnection) -> None:
    """Add and backfill v7.3 admission columns on an existing PostgreSQL table.

    ``metadata.create_all`` creates the columns for new installations but does not alter
    existing tables.  These statements are idempotent so every API start can safely verify
    the schema before workers begin claiming tasks.
    """
    if conn.dialect.name != "postgresql":
        return
    statements = (
        "ALTER TABLE consult_task ADD COLUMN IF NOT EXISTS task_kind VARCHAR(8)",
        "ALTER TABLE consult_task ADD COLUMN IF NOT EXISTS image_count INTEGER",
        """
        UPDATE consult_task
           SET image_count = CASE
               WHEN jsonb_typeof(COALESCE(payload::jsonb -> 'images', '[]'::jsonb)) = 'array'
               THEN jsonb_array_length(COALESCE(payload::jsonb -> 'images', '[]'::jsonb))
               ELSE 0
           END
         WHERE image_count IS NULL
        """,
        """
        UPDATE consult_task
           SET task_kind = CASE WHEN COALESCE(image_count, 0) > 0 THEN 'image' ELSE 'text' END
         WHERE task_kind IS NULL OR task_kind NOT IN ('text', 'image')
        """,
        "ALTER TABLE consult_task ALTER COLUMN task_kind SET DEFAULT 'text'",
        "ALTER TABLE consult_task ALTER COLUMN task_kind SET NOT NULL",
        "ALTER TABLE consult_task ALTER COLUMN image_count SET DEFAULT 0",
        "ALTER TABLE consult_task ALTER COLUMN image_count SET NOT NULL",
        """
        CREATE INDEX IF NOT EXISTS ix_consult_task_kind_status_updated
            ON consult_task (task_kind, status, updated_at)
        """,
    )
    for statement in statements:
        await conn.execute(text(statement))
