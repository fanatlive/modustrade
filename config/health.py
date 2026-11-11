"""Readiness behind TLS termination; responses never include private records."""
from __future__ import annotations
import hmac
from datetime import timedelta
from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return JsonResponse({'status': 'ok'})
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)


@require_GET
def worker_health(request: HttpRequest) -> JsonResponse:
    from feedback.models import WorkerHeartbeat
    try:
        beat = WorkerHeartbeat.objects.filter(key='default').first()
        fresh = bool(beat and beat.last_seen >= timezone.now()-timedelta(seconds=settings.WORKER_HEARTBEAT_MAX_AGE)
                     and not beat.last_error)
        return JsonResponse({'status': 'ok' if fresh else 'unavailable'}, status=200 if fresh else 503)
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)


@require_GET
def metrics(request: HttpRequest) -> JsonResponse:
    supplied = request.headers.get('Authorization', '')
    if not settings.OPERATIONS_TOKEN or not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer '+settings.OPERATIONS_TOKEN).encode('utf-8')):
        return JsonResponse({'detail': 'Not found'}, status=404)
    from feedback.operations import operations_snapshot
    try:
        return JsonResponse(operations_snapshot())
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)
