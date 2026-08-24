from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from . import views


class PostOnlyLogoutView(LogoutView):
    @method_decorator(require_POST)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('monitor_events/', views.monitor_events, name='monitor_events'),
    path('event/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('event/<uuid:event_id>/email/', views.email_responsible, name='email_responsible'),
    path('event/<uuid:event_id>/comment/', views.incident_comment, name='incident_comment'),
    path('event/<uuid:event_id>/note/', views.incident_note, name='incident_note'),
    path('event/<uuid:event_id>/note/<int:activity_id>/remove/', views.incident_note_remove, name='incident_note_remove'),
    path('event/<uuid:event_id>/mute/', views.incident_mute, name='incident_mute'),
    path('event/<uuid:event_id>/unmute/', views.incident_unmute, name='incident_unmute'),
    path('event/<uuid:event_id>/activity/', views.incident_activity, name='incident_activity'),
    path('event/<uuid:event_id>/jira/', views.create_jira_ticket, name='create_jira_ticket'),
    path('event/<uuid:event_id>/explain/', views.explain_event, name='event_explain'),
    path('api/event-search/', views.event_search, name='event_search'),
    path('api/events/per-day/', views.api_events_per_day, name='api_events_per_day'),
    path('api/event/<uuid:event_id>/history/', views.event_history_fragment, name='event_history_fragment'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', PostOnlyLogoutView.as_view(next_page='login'), name='logout'),
    path('account/', views.account_view, name='account'),
    path('catalogue/', views.alarms_catalogue, name='alarms_catalogue'),
]
