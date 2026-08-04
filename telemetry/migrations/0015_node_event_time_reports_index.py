from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0014_alertretentionpolicy_ttl_active_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            """
            CREATE INDEX IF NOT EXISTS idx_wgt_node_event_time_reports
            ON alerts (node, event_time DESC)
            WHERE (summary IS NOT NULL OR tests IS NOT NULL)
            """,
            "DROP INDEX IF EXISTS idx_wgt_node_event_time_reports",
        ),
    ]