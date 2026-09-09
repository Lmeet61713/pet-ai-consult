BEGIN;

ALTER TABLE consult_task ADD COLUMN IF NOT EXISTS task_kind VARCHAR(8);
ALTER TABLE consult_task ADD COLUMN IF NOT EXISTS image_count INTEGER;

UPDATE consult_task
SET image_count = CASE
    WHEN jsonb_typeof(COALESCE(payload::jsonb -> 'images', '[]'::jsonb)) = 'array'
    THEN jsonb_array_length(COALESCE(payload::jsonb -> 'images', '[]'::jsonb))
    ELSE 0
END
WHERE image_count IS NULL;

UPDATE consult_task
SET task_kind = CASE WHEN COALESCE(image_count, 0) > 0 THEN 'image' ELSE 'text' END
WHERE task_kind IS NULL OR task_kind NOT IN ('text', 'image');

ALTER TABLE consult_task ALTER COLUMN task_kind SET DEFAULT 'text';
ALTER TABLE consult_task ALTER COLUMN task_kind SET NOT NULL;
ALTER TABLE consult_task ALTER COLUMN image_count SET DEFAULT 0;
ALTER TABLE consult_task ALTER COLUMN image_count SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_consult_task_kind_status_updated
ON consult_task (task_kind, status, updated_at);

COMMIT;
