from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0011_alter_incidentevent_id_alter_incidentmute_id'),
    ]

    operations = [
        migrations.RunSQL(
            'ALTER TABLE alerts ADD COLUMN IF NOT EXISTS raw_json jsonb',
            'ALTER TABLE alerts DROP COLUMN IF EXISTS raw_json',
        ),
    ]
