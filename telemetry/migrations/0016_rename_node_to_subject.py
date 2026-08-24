from django.db import migrations


RENAME_SQL = """
DO $$
DECLARE
    r record;
BEGIN
    IF to_regclass('nodes') IS NOT NULL THEN
        ALTER TABLE nodes RENAME TO subjects;
    END IF;
    IF to_regclass('node_responsibles') IS NOT NULL THEN
        ALTER TABLE node_responsibles RENAME TO subject_responsibles;
    END IF;
    IF to_regclass('node_responsible_mapping') IS NOT NULL THEN
        ALTER TABLE node_responsible_mapping RENAME TO subject_responsible_mapping;
        ALTER TABLE subject_responsible_mapping RENAME COLUMN node_name TO subject_name;
    END IF;
    IF to_regclass('telemetry_profile_allowed_nodes') IS NOT NULL THEN
        ALTER TABLE telemetry_profile_allowed_nodes RENAME TO telemetry_profile_allowed_subjects;
        ALTER TABLE telemetry_profile_allowed_subjects RENAME COLUMN node_id TO subject_id;
    END IF;
    IF to_regclass('alerts') IS NOT NULL THEN
        ALTER TABLE alerts RENAME COLUMN node TO subject;
    END IF;
    -- Hygiene: rename indexes whose names still mention "node".
    FOR r IN
        SELECT indexname FROM pg_indexes
        WHERE tablename IN ('alerts', 'subjects')
          AND indexname ILIKE '%node%'
    LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I',
                       r.indexname,
                       replace(r.indexname, 'node', 'subject'));
    END LOOP;
END
$$;
"""

REVERSE_SQL = """
DO $$
BEGIN
    IF to_regclass('subjects') IS NOT NULL THEN
        ALTER TABLE subjects RENAME TO nodes;
    END IF;
    IF to_regclass('subject_responsibles') IS NOT NULL THEN
        ALTER TABLE subject_responsibles RENAME TO node_responsibles;
    END IF;
    IF to_regclass('subject_responsible_mapping') IS NOT NULL THEN
        ALTER TABLE subject_responsible_mapping RENAME COLUMN subject_name TO node_name;
        ALTER TABLE subject_responsible_mapping RENAME TO node_responsible_mapping;
    END IF;
    IF to_regclass('telemetry_profile_allowed_subjects') IS NOT NULL THEN
        ALTER TABLE telemetry_profile_allowed_subjects RENAME COLUMN subject_id TO node_id;
        ALTER TABLE telemetry_profile_allowed_subjects RENAME TO telemetry_profile_allowed_nodes;
    END IF;
    IF to_regclass('alerts') IS NOT NULL THEN
        ALTER TABLE alerts RENAME COLUMN subject TO node;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0015_node_event_time_reports_index'),
    ]

    operations = [
        migrations.RunSQL(RENAME_SQL, REVERSE_SQL),
    ]
