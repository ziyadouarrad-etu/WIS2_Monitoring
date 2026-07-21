from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from . import views

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
    path('api/alert-exists/<uuid:alert_id>/', views.alert_exists, name='alert_exists'),
    path('api/alerts/per-day/', views.api_alerts_per_day, name='api_alerts_per_day'),
    path('api/alerts/', views.api_alerts, name='api_alerts'),
    path('api/alerts/filter-options/', views.filter_options_api, name='filter_options_api'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),
    path('account/', views.account_view, name='account'),
]
