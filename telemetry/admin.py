from django.contrib import admin
from .models import Alert


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('event_time', 'severity', 'event_type', 'source', 'subject', 'title')
    list_filter = ('severity', 'event_type', 'subtype')
    search_fields = ('title', 'description', 'source', 'subject')
    ordering = ('-event_time',)
    date_hierarchy = 'event_time'
