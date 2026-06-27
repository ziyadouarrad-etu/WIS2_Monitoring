from django.db import migrations


def remove_old_content_type(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    ct = ContentType.objects.filter(app_label='telemetry', model='wis2globaltelemetry').first()
    if ct:
        Permission.objects.filter(content_type=ct).delete()
        ct.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0002_indexes'),
    ]

    operations = [
        migrations.RunPython(remove_old_content_type),
    ]
