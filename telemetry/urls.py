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
    path('monitor_alerts/', views.monitor_alerts, name='monitor_alerts'),
    path('alert/<uuid:alert_id>/', views.alert_detail, name='alert_detail'),
    path('alert/<uuid:alert_id>/email/', views.email_responsible, name='email_responsible'),
    path('alert/<uuid:alert_id>/comment/', views.incident_comment, name='incident_comment'),
    path('alert/<uuid:alert_id>/note/', views.incident_note, name='incident_note'),
    path('alert/<uuid:alert_id>/note/<int:event_id>/remove/', views.incident_note_remove, name='incident_note_remove'),
    path('alert/<uuid:alert_id>/mute/', views.incident_mute, name='incident_mute'),
    path('alert/<uuid:alert_id>/unmute/', views.incident_unmute, name='incident_unmute'),
    path('alert/<uuid:alert_id>/activity/', views.incident_activity, name='incident_activity'),
    path('api/alert-search/', views.alert_search, name='alert_search'),
    path('api/alerts/per-day/', views.api_alerts_per_day, name='api_alerts_per_day'),
    path('api/alert/<uuid:alert_id>/history/', views.alert_history_fragment, name='alert_history_fragment'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', PostOnlyLogoutView.as_view(next_page='login'), name='logout'),
    path('account/', views.account_view, name='account'),
    path('catalogue/', views.alarms_catalogue, name='alarms_catalogue'),
]
