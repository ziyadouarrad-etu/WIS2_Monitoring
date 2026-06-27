from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_wgt_event_time_desc '
            'ON alerts (event_time DESC)',
            'DROP INDEX IF EXISTS idx_wgt_event_time_desc',
        ),
        migrations.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_wgt_ingested_at '
            'ON alerts (ingested_at)',
            'DROP INDEX IF EXISTS idx_wgt_ingested_at',
        ),
        migrations.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_wgt_severity_event_time '
            'ON alerts (severity, event_time DESC)',
            'DROP INDEX IF EXISTS idx_wgt_severity_event_time',
        ),
        migrations.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_wgt_event_type '
            'ON alerts (event_type)',
            'DROP INDEX IF EXISTS idx_wgt_event_type',
        ),
        migrations.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_wgt_errors_not_null '
            'ON alerts (errors) '
            'WHERE errors IS NOT NULL',
            'DROP INDEX IF EXISTS idx_wgt_errors_not_null',
        ),
    ]
