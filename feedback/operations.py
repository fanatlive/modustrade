"""Aggregate infrastructure indicators; no subjects, messages, or identities."""
from __future__ import annotations
from datetime import timedelta
from django.conf import settings
from django.db.models import Count, Min
from django.utils import timezone


def operations_snapshot() -> dict:
    from .models import Outbox, WorkerHeartbeat
    now = timezone.now()
    beat = WorkerHeartbeat.objects.filter(key='default').first()
    states = {row['state']: row['count'] for row in Outbox.objects.order_by().values('state').annotate(count=Count('pk'))}
    pending = Outbox.objects.filter(state__in=['pending', 'processing'])
    oldest = pending.aggregate(at=Min('event__created_at'))['at']
    ready = pending.aggregate(at=Min('next_attempt_at'))['at']
    heartbeat_ok = bool(beat and not beat.last_error and beat.last_seen >= now-timedelta(seconds=settings.WORKER_HEARTBEAT_MAX_AGE))
    age = max(0, int((now-oldest).total_seconds())) if oldest else 0
    ready_age = max(0, int((now-ready).total_seconds())) if ready else 0
    return {'worker_healthy': heartbeat_ok, 'outbox': states, 'oldest_pending_seconds': age,
            'ready_job_lag_seconds': ready_age,
            'alerts': {'worker_stale': not heartbeat_ok, 'failed_deliveries': states.get('failed', 0)>0,
                       'delivery_lag': age>120}}
