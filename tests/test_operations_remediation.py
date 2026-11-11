from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone

from feedback.models import Category, Event, Outbox, Queue, RateLimitBucket, Ticket, User, WorkerHeartbeat
from feedback.operations import operations_snapshot


@override_settings(RATE_LOGIN_IP=2, RATE_LOGIN_ACCOUNT=3, TRUST_PROXY=False,
                   STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class OperationsRemediationTests(TestCase):
    def test_backoff_does_not_hide_actual_delivery_age(self):
        user = User.objects.create_user('ops-recipient')
        queue = Queue.objects.create(code='ops', name='Operations')
        category = Category.objects.get(code='unknown')
        ticket = Ticket.objects.create(number='OPS-AGE', author=user, category=category, queue=queue,
                                       request_type='PROBLEM', description='Synthetic delivery-age check')
        event = Event.objects.create(ticket=ticket, kind='CREATE', summary='Synthetic', sequence=1,
                                     created_at=timezone.now()-timedelta(minutes=20))
        Outbox.objects.create(event=event, recipient=user, ticket=ticket, message='Synthetic', attempts=7,
                              next_attempt_at=timezone.now()+timedelta(minutes=5))
        WorkerHeartbeat.objects.create()
        result = operations_snapshot()
        self.assertTrue(result['alerts']['delivery_lag'])
        self.assertGreaterEqual(result['oldest_pending_seconds'], 1199)
        self.assertEqual(result['ready_job_lag_seconds'], 0)

    def test_admin_and_regular_login_share_ip_limit(self):
        self.client.post('/accounts/login/', {'username': 'missing', 'password': 'bad'})
        self.client.post('/admin/login/', {'username': 'missing', 'password': 'bad'})
        response = self.client.post('/admin/login/', {'username': 'missing', 'password': 'bad'})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response['Retry-After']), 0)
        self.assertEqual(RateLimitBucket.objects.filter(key__startswith='login:').count(), 1)

    def test_account_limit_survives_ip_changes(self):
        for index in range(3):
            self.client.post('/accounts/login/', {'username': ' Missing ', 'password': 'bad'}, REMOTE_ADDR=f'10.0.0.{index}')
        response = self.client.post('/admin/login/', {'username': 'missing', 'password': 'bad'}, REMOTE_ADDR='10.0.0.9')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(RateLimitBucket.objects.filter(key__startswith='login-account:').count(), 1)

    def test_untrusted_proxy_cannot_rotate_identity(self):
        for index in range(2):
            self.client.post('/accounts/login/', {}, HTTP_X_REAL_IP=f'10.0.0.{index}')
        self.assertEqual(self.client.post('/admin/login/', {}, HTTP_X_REAL_IP='10.0.0.8').status_code, 429)

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'))
    def test_readiness_is_exempt_from_tls_redirect_and_proxy_is_recognized(self):
        self.assertEqual(self.client.get('/health/').status_code, 200)
        self.assertEqual(self.client.get('/accounts/login/').status_code, 301)
        self.assertEqual(self.client.get('/accounts/login/', HTTP_X_FORWARDED_PROTO='https').status_code, 200)

    def test_failed_database_is_unavailable(self):
        with patch('config.health.connection.cursor', side_effect=DatabaseError):
            self.assertEqual(self.client.get('/health/').status_code, 503)

    @override_settings(OPERATIONS_TOKEN='test-private-operations-token')
    def test_heartbeat_and_metrics_have_no_private_payload(self):
        self.assertEqual(self.client.get('/health/worker/').status_code, 503)
        beat = WorkerHeartbeat.objects.create()
        self.assertEqual(self.client.get('/health/worker/').status_code, 200)
        self.assertEqual(self.client.get('/operations/metrics/').status_code, 404)
        self.assertEqual(self.client.get('/operations/metrics/', HTTP_AUTHORIZATION='Bearer wrong').status_code, 404)
        self.assertEqual(self.client.get('/operations/metrics/', HTTP_AUTHORIZATION='Bearer é').status_code, 404)
        result = self.client.get('/operations/metrics/', HTTP_AUTHORIZATION='Bearer test-private-operations-token')
        self.assertTrue(result.json()['worker_healthy'])
        beat.last_seen = timezone.now()-timedelta(minutes=3)
        beat.save()
        self.assertEqual(self.client.get('/health/worker/').status_code, 503)
        self.assertTrue(operations_snapshot()['alerts']['worker_stale'])
