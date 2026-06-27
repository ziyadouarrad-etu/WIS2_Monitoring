from django.db import migrations


def remove_extra_content_types(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    for app_label, model_name in (
        ('monitor', 'alertcomment'),
        ('monitor', 'watchlist'),
    ):
        ct = ContentType.objects.filter(app_label=app_label, model=model_name).first()
        if ct:
            Permission.objects.filter(content_type=ct).delete()
            ct.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0005_cleanup_monitor_content_type'),
    ]

    operations = [
        migrations.RunPython(remove_extra_content_types),
    ]
