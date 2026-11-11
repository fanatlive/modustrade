from django.conf import settings
from django.urls import reverse

from .permissions import can_coordinate, has_role, visible_notifications


def workspace(request):
    user = getattr(request, "user", None)
    authenticated = bool(user and user.is_authenticated)
    unread = 0
    if authenticated:
        unread = visible_notifications(user).filter(read_at__isnull=True).count()
    return {
        "demo_mode": getattr(settings, "DEMO_MODE", settings.DEBUG),
        "unread_notifications": unread,
        "can_manage_settings": authenticated and (user.is_superuser or has_role(user, "admin")),
        "is_operator": authenticated and (user.is_superuser or has_role(user, "processor", "coordinator", "manager")),
        "can_manage_deliveries": authenticated and can_coordinate(user),
        "current_path": request.path,
    }
