# Generated manually — creates only the new managed models
# (Node, NodeResponsible, NodeResponsibleMapping, Profile are managed=False)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('telemetry', '0006_cleanup_monitor_extra'),
        ('auth', '__first__'),
    ]

    operations = [
        migrations.CreateModel(
            name='IncidentEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('incident_hash', models.TextField(db_index=True)),
                ('event_type', models.CharField(choices=[('comment', 'Comment'), ('email_sent', 'Email Sent'), ('note_added', 'Note Added'), ('note_removed', 'Note Removed'), ('muted', 'Muted'), ('unmuted', 'Unmuted'), ('viewed', 'Viewed')], max_length=20)),
                ('text', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('active', models.BooleanField(default=True)),
                ('alert', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='telemetry.alert', db_constraint=False)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
            ],
            options={
                'db_table': 'incident_events',
                'ordering': ['created_at'],
            },
        ),
        migrations.CreateModel(
            name='IncidentMute',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('incident_hash', models.TextField()),
                ('muted_until', models.DateTimeField()),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
            ],
            options={
                'db_table': 'incident_mutes',
                'unique_together': {('incident_hash', 'user')},
            },
        ),
    ]
