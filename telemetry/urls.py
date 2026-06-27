from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('alert/<uuid:alert_id>/', views.alert_detail, name='alert_detail'),
    path('api/alerts/', views.api_alerts, name='api_alerts'),
]
