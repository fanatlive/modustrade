from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from feedback.models import Category, Comment, Notification, Ticket, User
from feedback.services import create_ticket, execute_command, process_outbox


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class InterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_demo', stdout=StringIO())
        cls.author = User.objects.get(username='author')
        cls.other = User.objects.get(username='author2')
        cls.processor = User.objects.get(username='processor')
        cls.admin = User.objects.get(username='admin')

    def test_login_required_and_login_page_renders(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_list_detail_and_filter_are_scoped(self):
        self.client.force_login(self.author)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['summary']['total'], 4)
        foreign = Ticket.objects.filter(author=self.other).first()
        self.assertEqual(self.client.get(reverse('feedback:detail', args=[foreign.pk])).status_code, 404)
        response = self.client.get('/', {'q': foreign.description[:30]})
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_internal_notes_never_render_for_author(self):
        ticket = Ticket.objects.get(source_record_id='A-002')
        execute_command(actor=self.processor, ticket_id=ticket.pk, command='comment',
                        expected_version=ticket.version, data={'message': 'СЕКРЕТНАЯ-ЗАМЕТКА-9821', 'visibility': 'internal'})
        self.client.force_login(self.author)
        response = self.client.get(reverse('feedback:detail', args=[ticket.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'СЕКРЕТНАЯ-ЗАМЕТКА-9821')
        self.assertEqual(response.context['comments'].count(), 0)
        self.assertFalse(response.context['events'].filter(visibility='internal').exists())

    def test_operator_pages_and_admin_scope(self):
        self.client.force_login(self.author)
        self.assertEqual(self.client.get('/queue/').status_code, 403)
        self.assertEqual(self.client.get('/settings/').status_code, 403)
        self.client.force_login(self.processor)
        for path in ('/', '/queue/', '/analytics/', '/notifications/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        self.assertEqual(self.client.get('/').context['summary']['total'], 0)

    def test_invalid_form_preserves_description(self):
        self.client.force_login(self.author)
        text = 'Введённый текст должен остаться после ошибки категории.'
        response = self.client.post('/requests/new/', {'description': text, 'request_type': 'PROBLEM',
            'category': '999999', 'idempotency_key': str(uuid4()), 'reported_priority': 'MEDIUM', 'confidentiality': 'STANDARD'})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, text, status_code=422)

    def test_csrf_is_required_for_mutation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post('/requests/new/', {'description': 'CSRF'}).status_code, 403)

    def test_analytics_separates_demo_and_unknown_dates(self):
        self.client.force_login(self.processor)
        demo = self.client.get('/analytics/', {'mode': 'demo'})
        self.assertEqual(demo.status_code, 200)
        self.assertEqual(demo.context['total'], 20)
        self.assertEqual(demo.context['known_count'], 0)
        self.assertEqual(demo.context['response_mean'], 'Нет данных')
        production = self.client.get('/analytics/', {'mode': 'production'})
        self.assertEqual(production.context['total'], 0)

    def test_new_interactive_demo_is_not_a_production_record(self):
        ticket = create_ticket(actor=self.author, idempotency_key='demo-only', data={
            'category': Category.objects.first(), 'request_type': 'PROBLEM',
            'description': 'Интерактивная демо-карточка не является реальными данными.'})
        self.assertTrue(ticket.is_demo)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get('/analytics/', {'mode': 'production'}).context['total'], 0)

    def test_analytics_aggregation_counts_tickets_not_queue_memberships(self):
        self.client.force_login(self.processor)
        response = self.client.get('/analytics/', {'mode': 'demo'})
        self.assertEqual(response.context['total'], 20)
        statuses = {row['code']: row['count'] for row in response.context['status_rows']}
        types = {row['code']: row['count'] for row in response.context['type_rows']}
        self.assertEqual(statuses, {'NEW': 5, 'REVIEW': 8, 'WAITING_INFO': 2, 'IN_PROGRESS': 3, 'CLOSED': 2})
        self.assertEqual(types, {'PROBLEM': 9, 'SUGGESTION': 10, 'PRAISE': 1})
        self.assertEqual(sum(statuses.values()), response.context['total'])
        self.assertEqual(sum(row['n'] for row in response.context['outcomes']), 2)
        self.assertEqual(response.context['closed'], 2)
        self.assertEqual(response.context['implemented'], 1)
        self.assertTrue(all(row['n'] <= 2 for row in response.context['category_rows']))

    def test_successful_creation_and_repeat_redirect_to_same_card(self):
        self.client.force_login(self.author)
        payload = {'description': 'Полный HTTP-сценарий создания обращения сотрудника.', 'request_type': 'SUGGESTION',
            'category': Category.objects.first().pk, 'idempotency_key': str(uuid4()), 'reported_priority': 'MEDIUM', 'confidentiality': 'STANDARD'}
        count = Ticket.objects.count()
        first = self.client.post('/requests/new/', payload)
        second = self.client.post('/requests/new/', payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, second.url)
        self.assertEqual(Ticket.objects.count(), count+1)

    def test_notification_mark_read_checks_owner(self):
        ticket = Ticket.objects.get(source_record_id='A-002')
        execute_command(actor=self.processor, ticket_id=ticket.pk, command='comment',
                        expected_version=ticket.version, data={'message': 'Ответ по существу обращения', 'visibility': 'public'})
        process_outbox()
        item = Notification.objects.filter(user=self.author).first()
        self.assertIsNotNone(item)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('feedback:read_notification', args=[item.pk])).status_code, 404)
        item.refresh_from_db()
        self.assertIsNone(item.read_at)

    def test_invalid_transition_and_stale_version_are_explicit(self):
        ticket = Ticket.objects.get(source_record_id='A-001')
        self.client.force_login(self.processor)
        url = reverse('feedback:command', args=[ticket.pk])
        invalid = self.client.post(url, {'command': 'implement', 'version': ticket.version, 'result': 'Готово', 'evidence': 'Проверено'})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.post(url, {'command': 'start', 'version': ticket.version}).status_code, 302)
        stale = self.client.post(url, {'command': 'comment', 'version': ticket.version, 'message': 'Устаревшая форма'})
        self.assertEqual(stale.status_code, 409)

    def test_rate_limit_login_is_shared_database_state(self):
        from feedback.models import RateLimitBucket
        from django.utils import timezone
        from datetime import timedelta
        import hashlib
        key = 'login:' + hashlib.sha256(b'127.0.0.1').hexdigest()
        RateLimitBucket.objects.create(key=key, count=20, expires_at=timezone.now()+timedelta(minutes=2))
        response = self.client.post('/accounts/login/', {'username': 'unknown', 'password': 'invalid'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
