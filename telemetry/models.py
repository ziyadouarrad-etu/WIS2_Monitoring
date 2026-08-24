from django.db import models
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver
from django.contrib.auth.models import User, Group
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError


class Subject(models.Model):
    name = models.CharField(max_length=255, primary_key=True)
    responsibles = models.ManyToManyField(
        'SubjectResponsible',
        through='SubjectResponsibleMapping',
        through_fields=('subject', 'responsible'),
    )

    class Meta:
        managed = False
        db_table = 'subjects'

    def __str__(self):
        return self.name


class SubjectResponsible(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_column='created_at')

    class Meta:
        managed = False
        db_table = 'subject_responsibles'

    def __str__(self):
        return self.name


class SubjectResponsibleMapping(models.Model):
    id = models.AutoField(primary_key=True)
    subject = models.ForeignKey(
        Subject, on_delete=models.CASCADE,
        db_column='subject_name', to_field='name',
    )
    responsible = models.ForeignKey(
        SubjectResponsible, on_delete=models.CASCADE,
        db_column='responsible_id',
    )
    assigned_at = models.DateTimeField(auto_now_add=True, db_column='assigned_at')

    class Meta:
        managed = False
        db_table = 'subject_responsible_mapping'
        unique_together = [('subject', 'responsible')]

    def __str__(self):
        return f"{self.subject.name} - {self.responsible.name}"

class Event(models.Model):
    id = models.UUIDField(primary_key=True)
    specversion = models.CharField(max_length=10)
    event_type = models.CharField(max_length=255)
    source = models.CharField(max_length=255)
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        db_column='subject',
        to_field='name',
        db_index=True,
    )
    event_time = models.DateTimeField()
    datacontenttype = models.CharField(max_length=100)
    dataschema = models.TextField(null=True, blank=True)
    conforms_to = models.JSONField(null=True, blank=True)
    severity = models.CharField(max_length=50)
    subtype = models.CharField(max_length=100, null=True, blank=True)
    channel = models.TextField(null=True, blank=True)
    title = models.TextField(null=True, blank=True)
    display_title = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    wnm = models.JSONField(null=True, blank=True)
    errors = models.JSONField(null=True, blank=True)
    tests = models.JSONField(null=True, blank=True)
    summary = models.JSONField(null=True, blank=True)
    links = models.JSONField(null=True, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)
    incident_hash = models.TextField(null=True, blank=True, db_index=True)
    raw_json = models.JSONField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'events'
        ordering = ['-event_time']

    def __str__(self):
        return f"[{self.severity}] {self.title or self.event_type} — {self.subject}"

class Profile(models.Model):
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE)
    allowed_subjects = models.ManyToManyField(Subject, blank=True)

    class Meta:
        managed = False

    def __str__(self):
        return f"Profile for {self.user.username}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(m2m_changed, sender=User.groups.through)
def sync_staff_on_group_change(sender, instance, action, **kwargs):
    pk_set = kwargs.get('pk_set')

    if isinstance(instance, User):
        if action in ('post_add', 'post_remove', 'post_clear'):
            is_admin = instance.groups.filter(name='Admin').exists()
            if instance.is_staff != is_admin or instance.is_superuser != is_admin:
                instance.is_staff = is_admin
                instance.is_superuser = is_admin
                instance.save(update_fields=['is_staff', 'is_superuser'])

    elif isinstance(instance, Group) and instance.name == 'Admin':
        if action == 'post_add' and pk_set:
            User.objects.filter(pk__in=pk_set).update(is_staff=True, is_superuser=True)
        elif action == 'post_remove' and pk_set:
            still_admin = set(instance.user_set.values_list('pk', flat=True))
            removed = pk_set - still_admin
            User.objects.filter(pk__in=removed).update(is_staff=False, is_superuser=False)


class IncidentActivity(models.Model):
    EVENT_TYPES = [
        ('comment', 'Comment'),
        ('email_sent', 'Email Sent'),
        ('note_added', 'Note Added'),
        ('note_removed', 'Note Removed'),
        ('muted', 'Muted'),
        ('unmuted', 'Unmuted'),
        ('viewed', 'Viewed'),
        ('jira_ticket', 'Jira Ticket'),
    ]
    incident_hash = models.TextField(db_index=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, null=True, blank=True, db_column='event_id', db_constraint=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    text = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'incident_activities'
        ordering = ['created_at']

    def __str__(self):
        return f"[{self.event_type}] {self.incident_hash} by {self.user.username}"


class IncidentMute(models.Model):
    incident_hash = models.TextField(db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    muted_until = models.DateTimeField()

    class Meta:
        db_table = 'incident_mutes'
        unique_together = [('incident_hash', 'user')]

    def __str__(self):
        return f"{self.incident_hash} muted by {self.user.username} until {self.muted_until}"


class EventRetentionPolicy(models.Model):
    ttl_active = models.BooleanField(
        default=False,
        help_text="Enable the event TTL. When disabled, events are kept forever.",
    )
    retention_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Delete events older than this many days. Required when TTL is active.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'event_retention_policy'
        verbose_name = 'event retention policy'
        verbose_name_plural = 'event retention policy'

    def clean(self):
        if self.ttl_active and not self.retention_days:
            raise ValidationError(
                {'retention_days': 'Set a number of days when TTL is active.'}
            )

    def __str__(self):
        if self.ttl_active and self.retention_days:
            return f"TTL active: delete events older than {self.retention_days} days"
        return "TTL disabled: events are kept forever"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def get_retention_days():
    """Return the configured event TTL in days, or None when TTL is off (persistence)."""
    obj = EventRetentionPolicy.objects.first()
    if obj is None or not obj.ttl_active or not obj.retention_days:
        return None
    return obj.retention_days
