from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0008_node_noderesponsible_noderesponsiblemapping_profile'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE alerts DROP COLUMN IF EXISTS category;",
            reverse_sql="ALTER TABLE alerts ADD COLUMN IF NOT EXISTS category TEXT NULL;",
        ),
    ]
