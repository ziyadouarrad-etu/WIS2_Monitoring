from django.core.validators import MinValueValidator
from django.db import migrations, models


RENAME_SQL = """
DO $$
DECLARE
    r record;
BEGIN
    IF to_regclass('alerts') IS NOT NULL THEN
        ALTER TABLE alerts RENAME TO events;
    END IF;
    IF to_regclass('alert_retention_policy') IS NOT NULL THEN
        ALTER TABLE alert_retention_policy RENAME TO event_retention_policy;
    END IF;
    IF to_regclass('incident_events') IS NOT NULL THEN
        ALTER TABLE incident_events RENAME TO incident_activities;
        ALTER TABLE incident_activities RENAME COLUMN alert_id TO event_id;
    END IF;
    -- Hygiene: rename indexes whose names still mention "alert".
    FOR r IN
        SELECT indexname FROM pg_indexes
        WHERE tablename IN ('events', 'incident_activities', 'event_retention_policy')
          AND indexname ILIKE '%alert%'
    LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I',
                       r.indexname,
                       replace(r.indexname, 'alert', 'event'));
    END LOOP;
END
$$;
"""

REVERSE_SQL = """
DO $$
BEGIN
    IF to_regclass('events') IS NOT NULL THEN
        ALTER TABLE events RENAME TO alerts;
    END IF;
    IF to_regclass('event_retention_policy') IS NOT NULL THEN
        ALTER TABLE event_retention_policy RENAME TO alert_retention_policy;
    END IF;
    IF to_regclass('incident_activities') IS NOT NULL THEN
        ALTER TABLE incident_activities RENAME COLUMN event_id TO alert_id;
        ALTER TABLE incident_activities RENAME TO incident_events;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0017_subject_subjectresponsible_subjectresponsiblemapping_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(RENAME_SQL, REVERSE_SQL),
            ],
            state_operations=[
                migrations.RenameModel(old_name='Alert', new_name='Event'),
                migrations.RenameModel(old_name='AlertRetentionPolicy', new_name='EventRetentionPolicy'),
                migrations.RenameModel(old_name='IncidentEvent', new_name='IncidentActivity'),
                migrations.AlterModelTable('event', 'events'),
                migrations.AlterModelTable('eventretentionpolicy', 'event_retention_policy'),
                migrations.AlterModelTable('incidentactivity', 'incident_activities'),
                migrations.RenameField('incidentactivity', 'alert', 'event'),
                migrations.AlterField(
                    model_name='incidentactivity',
                    name='event',
                    field=models.ForeignKey(blank=True, db_column='event_id', db_constraint=False, null=True, on_delete=models.CASCADE, to='telemetry.event'),
                ),
                migrations.AlterField(
                    model_name='eventretentionpolicy',
                    name='ttl_active',
                    field=models.BooleanField(default=False, help_text='Enable the event TTL. When disabled, events are kept forever.'),
                ),
                migrations.AlterField(
                    model_name='eventretentionpolicy',
                    name='retention_days',
                    field=models.PositiveIntegerField(blank=True, help_text='Delete events older than this many days. Required when TTL is active.', null=True, validators=[MinValueValidator(1)]),
                ),
                migrations.AlterModelOptions(
                    name='eventretentionpolicy',
                    options={'verbose_name': 'event retention policy', 'verbose_name_plural': 'event retention policy'},
                ),
            ],
        ),
    ]
