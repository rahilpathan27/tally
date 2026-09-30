BEGIN;

ALTER TABLE core_recovery_incidents
    ALTER COLUMN correction_entry_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS correction_reference text;

INSERT INTO core_schema_migrations(version) VALUES (5) ON CONFLICT DO NOTHING;
COMMIT;
