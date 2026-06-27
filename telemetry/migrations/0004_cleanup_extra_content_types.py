from django.db import migrations


def remove_extra_content_types(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    for model_name in ('wis2telemetryevent',):
        ct = ContentType.objects.filter(app_label='telemetry', model=model_name).first()
        if ct:
            Permission.objects.filter(content_type=ct).delete()
            ct.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0003_cleanup_old_content_type'),
    ]

    operations = [
        migrations.RunPython(remove_extra_content_types),
    ]
