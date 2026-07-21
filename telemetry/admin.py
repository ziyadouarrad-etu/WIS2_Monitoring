from django.contrib import admin
from .models import Alert, Profile, NodeResponsible, NodeResponsibleMapping


class AlertAdmin(admin.ModelAdmin):
    list_display = ('event_time', 'severity', 'event_type', 'source', 'node', 'title')
    list_filter = ('severity', 'event_type', 'source', 'node', 'event_time')
    search_fields = ('title', 'description', 'source', 'node')
    ordering = ('-event_time',)
    date_hierarchy = 'event_time'


class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'allowed_nodes_list')
    filter_horizontal = ('allowed_nodes',)

    def allowed_nodes_list(self, obj):
        return ", ".join(n.name for n in obj.allowed_nodes.all())
    allowed_nodes_list.short_description = 'Allowed Nodes'


class NodeResponsibleMappingInline(admin.TabularInline):
    model = NodeResponsibleMapping
    extra = 1


@admin.register(NodeResponsible)
class NodeResponsibleAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'assigned_nodes', 'created_at')
    search_fields = ('name', 'email')
    inlines = [NodeResponsibleMappingInline]

    def assigned_nodes(self, obj):
        return ", ".join(m.node.name for m in obj.noderesponsiblemapping_set.select_related('node').all())
    assigned_nodes.short_description = 'Assigned Nodes'


@admin.register(NodeResponsibleMapping)
class NodeResponsibleMappingAdmin(admin.ModelAdmin):
    list_display = ('node', 'responsible', 'assigned_at')
    list_filter = ('node', 'responsible')


admin.site.register(Alert, AlertAdmin)
admin.site.register(Profile, ProfileAdmin)
