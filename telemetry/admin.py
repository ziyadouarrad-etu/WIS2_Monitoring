from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta
from .models import (
    Profile, NodeResponsible, NodeResponsibleMapping, Node,
    AlertRetentionPolicy, Alert,
)


class ProfileInline(admin.StackedInline):
    model = Profile
    filter_horizontal = ('allowed_nodes',)
    max_num = 1
    fields = ('allowed_nodes',)

    def get_extra(self, request, obj=None, **kwargs):
        return 0 if obj else 1


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    add_fieldsets = (
        (None, {
            'fields': ('username', 'password1', 'password2'),
        }),
        ('Permissions', {
            'fields': ('groups',),
        }),
    )

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, Profile) and not instance.pk:
                existing = Profile.objects.filter(user=instance.user).first()
                if existing:
                    instance.pk = existing.pk
                    instance.user = existing.user
            instance.save()
        formset.save_m2m()


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


class NodeResponsibleMappingInline(admin.TabularInline):
    model = NodeResponsibleMapping
    extra = 1


@admin.register(NodeResponsible)
class NodeResponsibleAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'assigned_nodes', 'created_at')
    search_fields = ('name', 'email')
    inlines = [NodeResponsibleMappingInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('noderesponsiblemapping_set__node')

    def assigned_nodes(self, obj):
        mappings = obj.noderesponsiblemapping_set.all()
        return ", ".join(m.node.name for m in mappings)
    assigned_nodes.short_description = 'Assigned Nodes'


class AlertRetentionPolicyForm(forms.ModelForm):
    class Meta:
        model = AlertRetentionPolicy
        fields = ('ttl_active', 'retention_days')

    def clean(self):
        data = super().clean()
        if data.get('ttl_active') and not data.get('retention_days'):
            raise ValidationError({'retention_days': 'Set a number of days when TTL is active.'})
        return data


@admin.register(AlertRetentionPolicy)
class AlertRetentionPolicyAdmin(admin.ModelAdmin):
    form = AlertRetentionPolicyForm
    list_display = ('ttl_active', 'retention_days', 'impact_preview', 'updated_at')
    fields = ('ttl_active', 'retention_days', 'impact_preview', 'updated_at')
    readonly_fields = ('impact_preview', 'updated_at')

    def has_add_permission(self, request):
        return not AlertRetentionPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def impact_preview(self, obj):
        if not obj or not obj.ttl_active:
            return 'TTL disabled: alerts are kept forever'
        if not obj.retention_days:
            return 'TTL active but no retention days set'
        cutoff = timezone.now() - timedelta(days=obj.retention_days)
        count = Alert.objects.filter(ingested_at__lt=cutoff).count()
        return f"Would purge ~{count:,} alert(s) older than {obj.retention_days} days (ingested before {cutoff:%Y-%m-%d %H:%M})"
    impact_preview.short_description = 'Impact preview'
