from django.db import models


class Alert(models.Model):
    id = models.UUIDField(primary_key=True)
    specversion = models.CharField(max_length=10)
    event_type = models.CharField(max_length=255)
    source = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    event_time = models.DateTimeField()
    datacontenttype = models.CharField(max_length=100)
    dataschema = models.TextField(null=True, blank=True)
    conforms_to = models.JSONField(null=True, blank=True)
    severity = models.CharField(max_length=50)
    subtype = models.CharField(max_length=100, null=True, blank=True)
    channel = models.TextField(null=True, blank=True)
    title = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    wnm = models.JSONField(null=True, blank=True)
    errors = models.JSONField(null=True, blank=True)
    tests = models.JSONField(null=True, blank=True)
    summary = models.JSONField(null=True, blank=True)
    links = models.JSONField(null=True, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)
    incident_hash = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'alerts'
        ordering = ['-event_time']

    def __str__(self):
        return f"[{self.severity}] {self.title or self.event_type} — {self.subject}"
