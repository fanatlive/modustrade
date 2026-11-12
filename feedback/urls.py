from django.urls import path
from . import views

app_name = "feedback"
urlpatterns = [
    path("", views.ticket_list, name="list"),
    path("demo/", views.demo_home, name="demo"),
    path("requests/<uuid:pk>/demo-result/", views.demo_result, name="demo_result"),
    path("queue/", views.work_queue, name="queue"),
    path("requests/new/", views.ticket_create, name="create"),
    path("requests/<uuid:pk>/", views.ticket_detail, name="detail"),
    path("requests/<uuid:pk>/command/", views.ticket_command, name="command"),
    path("requests/<uuid:pk>/canonical/", views.canonical_search, name="canonical_search"),
    path("requests/<uuid:pk>/similar/", views.similar_requests, name="similar"),
    path("recurring/", views.recurring_problems, name="recurring"),
    path("analytics/", views.analytics, name="analytics"),
    path("settings/", views.workspace_settings, name="settings"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/<int:pk>/read/", views.read_notification, name="read_notification"),
    path("deliveries/failed/", views.failed_deliveries, name="failed_deliveries"),
    path("deliveries/<int:pk>/retry/", views.retry_delivery, name="retry_delivery"),
]
