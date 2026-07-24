from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import Profile, NodeResponsible, NodeResponsibleMapping, Node


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
