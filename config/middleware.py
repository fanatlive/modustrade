"""Shared limits for both authentication entry points, without retained identities."""
import hashlib
from datetime import timedelta
from ipaddress import ip_address

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone


class MutationThrottle:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST':
            return self.get_response(request)
        kind, maximum, seconds, identity = None, 0, 0, ''
        if request.path_info.rstrip('/') in {'/accounts/login', '/admin/login'}:
            kind, maximum, seconds = 'login', settings.RATE_LOGIN_IP, 300
            identity = request.META.get('REMOTE_ADDR', '')
            if settings.TRUST_PROXY:
                try:
                    identity = str(ip_address(request.META.get('HTTP_X_REAL_IP', '')))
                except ValueError:
                    pass
        elif request.user.is_authenticated and request.path == '/requests/new/':
            kind, maximum, seconds = 'create', 30, 3600
            identity = str(request.user.pk)
        elif request.user.is_authenticated and request.path.endswith('/command/'):
            kind, maximum, seconds = 'command', 120, 60
            identity = str(request.user.pk)
        limits = [(kind, identity, maximum, seconds)] if kind else []
        if kind == 'login':
            account = request.POST.get('username', '').strip().casefold()
            if account:
                limits.append(('login-account', account, settings.RATE_LOGIN_ACCOUNT, 300))
        for kind, identity, maximum, seconds in limits:
            from feedback.models import RateLimitBucket
            now = timezone.now()
            # A stable key bounds storage per identity; all app processes share it.
            key = kind + ':' + hashlib.sha256(identity.encode()).hexdigest()
            with transaction.atomic():
                bucket, _ = RateLimitBucket.objects.get_or_create(key=key, defaults={'expires_at': now + timedelta(seconds=seconds)})
                bucket = RateLimitBucket.objects.select_for_update().get(pk=bucket.pk)
                if bucket.expires_at <= now:
                    bucket.count = 0
                    bucket.expires_at = now + timedelta(seconds=seconds)
                bucket.count += 1
                bucket.save(update_fields=['count', 'expires_at'])
                blocked = bucket.count > maximum
                wait = max(1, int((bucket.expires_at - now).total_seconds()))
            if blocked:
                response = HttpResponse('Слишком много запросов. Подождите немного и повторите действие.', status=429, content_type='text/plain; charset=utf-8')
                response['Retry-After'] = str(wait)
                return response
        return self.get_response(request)
