"""E-mail, Telegram and Bitrix24 delivery on top of the in-app outbox."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from feedback import channels
from feedback.models import Category, ExternalDelivery, IntegrationState, Queue, User
from feedback.services import create_ticket, execute_command, process_outbox

TG = {"TELEGRAM_BOT_TOKEN": "123:test-token", "TELEGRAM_BOT_USERNAME": "modus_test_bot"}
BX = {"BITRIX24_WEBHOOK_URL": "https://portal.example/rest/1/secret"}


class FakeHttp:
    """Records outbound calls; answers like Telegram / Bitrix24."""
    def __init__(self, responses=None):
        self.calls, self.responses = [], responses or {}

    def __call__(self, url, data):
        method = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls.append((method, data))
        response = self.responses.get(method, (200, {"ok": True, "result": {}}))
        return response(data) if callable(response) else response

    def methods(self):
        return [m for m, _ in self.calls]


class ExternalChannelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user('ext-author', password='safe-test-password', role='author',
                                              email='author@example.com', first_name='Анна')
        cls.processor = User.objects.create_user('ext-processor', password='safe-test-password', role='processor')
        cls.coordinator = User.objects.create_user('ext-coordinator', password='safe-test-password', role='coordinator', restricted_access=True)
        cls.category = Category.objects.create(code='ext', name='Проверки')
        cls.queue = Queue.objects.create(code='default', name='Разбор', owner=cls.coordinator)
        cls.queue.members.add(cls.processor, cls.coordinator)
        restricted = Queue.objects.create(code='restricted', name='Конфиденциально', restricted=True, owner=cls.coordinator)
        restricted.members.add(cls.coordinator)

    def make_ticket(self, **overrides):
        data = {'category': self.category, 'request_type': 'SUGGESTION', 'reported_priority': 'MEDIUM',
                'description': 'Секретное описание, которое не должно уйти наружу.', 'confidentiality': 'STANDARD'}
        data.update(overrides)
        return create_ticket(actor=self.author, data=data, idempotency_key=str(uuid4()))

    def approve(self, ticket):
        for command, extra in (('start', {}), ('approve', {'reason': 'Полезно', 'criteria': 'Проверяемый результат',
                                                           'plan_due_at': (timezone.now()+timedelta(days=10)).isoformat()})):
            execute_command(actor=self.processor, ticket_id=ticket.pk, command=command, expected_version=ticket.version, data=extra)
            ticket.refresh_from_db()
        return ticket

    def test_approval_is_emailed_to_author_without_ticket_text(self):
        ticket = self.approve(self.make_ticket())
        process_outbox()
        subjects = [m.subject for m in mail.outbox if m.to == ['author@example.com']]
        self.assertTrue(any('Предложение одобрено' in s and ticket.number in s for s in subjects), subjects)
        for message in mail.outbox:
            self.assertNotIn('Секретное описание', message.body)
            self.assertIn(str(ticket.pk), message.body)
        self.assertFalse(ExternalDelivery.objects.exclude(state='sent').exists())
        process_outbox()  # idempotent: nothing is sent twice
        self.assertEqual(len(mail.outbox), ExternalDelivery.objects.count())

    def test_opt_out_and_missing_address_skip_email(self):
        User.objects.filter(pk=self.author.pk).update(notify_email=False)
        self.approve(self.make_ticket())
        process_outbox()
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(ExternalDelivery.objects.exists())  # processor/coordinator have no e-mail either

    def test_restricted_ticket_sends_only_neutral_text(self):
        ticket = self.make_ticket(confidentiality='RESTRICTED')
        User.objects.filter(pk=self.coordinator.pk).update(email='coord@example.com')
        process_outbox()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(ticket.number, mail.outbox[0].subject + mail.outbox[0].body)
        self.assertIn('ограниченным доступом', mail.outbox[0].subject)

    def test_revoked_access_skips_queued_delivery(self):
        ticket = self.approve(self.make_ticket())
        with patch('feedback.services.deliver_external', return_value={}):
            process_outbox()
        User.objects.filter(pk=self.author.pk).update(is_active=False)
        channels.deliver_external()
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(ExternalDelivery.objects.filter(outbox__ticket=ticket, state='skipped').exists())

    @override_settings(**TG)
    def test_telegram_link_flow_and_delivery(self):
        http = FakeHttp()
        link = channels.start_telegram_link(self.author)
        self.assertTrue(link.startswith('https://t.me/modus_test_bot?start='))
        token = link.rsplit('=', 1)[1]
        http.responses["getUpdates"] = (200, {"ok": True, "result": [
            {"update_id": 7, "message": {"chat": {"id": 555, "type": "private"}, "text": f"/start {token}"}}]})
        with patch('feedback.channels._http_post', http):
            self.assertEqual(channels.poll_telegram(), 1)
        self.author.refresh_from_db()
        self.assertEqual((self.author.telegram_chat_id, self.author.telegram_link_token), ('555', ''))
        self.assertEqual(IntegrationState.objects.get(key='telegram_updates').value['offset'], 8)
        http.responses["getUpdates"] = (200, {"ok": True, "result": []})
        ticket = self.approve(self.make_ticket())
        with patch('feedback.channels._http_post', http):
            process_outbox()
        texts = [d['text'] for m, d in http.calls if m == 'sendMessage' and d['chat_id'] == '555']
        self.assertTrue(any('Предложение одобрено' in t and ticket.number in t for t in texts), texts)
        self.assertFalse(any('Секретное описание' in t for t in texts))

    @override_settings(**TG)
    def test_expired_telegram_token_does_not_link(self):
        channels.start_telegram_link(self.author)
        self.author.refresh_from_db()
        User.objects.filter(pk=self.author.pk).update(telegram_link_expires_at=timezone.now()-timedelta(minutes=1))
        with patch('feedback.channels._http_post', FakeHttp()):
            channels.handle_bot_message('999', f'/start {self.author.telegram_link_token}')
        self.author.refresh_from_db()
        self.assertEqual(self.author.telegram_chat_id, '')

    @override_settings(**TG)
    def test_blocked_bot_fails_without_retry(self):
        User.objects.filter(pk=self.author.pk).update(telegram_chat_id='555', notify_email=False)
        http = FakeHttp({"sendMessage": (403, {"ok": False, "error_code": 403}), "getUpdates": (200, {"ok": True, "result": []})})
        self.approve(self.make_ticket())
        with patch('feedback.channels._http_post', http):
            process_outbox()
        delivery = ExternalDelivery.objects.filter(channel='telegram').first()
        self.assertEqual((delivery.state, delivery.last_error), ('failed', 'telegram_403'))

    @override_settings(**BX)
    def test_bitrix_resolves_user_by_email_and_notifies(self):
        User.objects.filter(pk=self.author.pk).update(notify_email=False)
        http = FakeHttp({"user.get": (200, {"result": [{"ID": "42"}]}), "im.notify.system.add": (200, {"result": 1})})
        ticket = self.approve(self.make_ticket())
        with patch('feedback.channels._http_post', http):
            process_outbox()
        self.author.refresh_from_db()
        self.assertEqual(self.author.bitrix_user_id, '42')
        notes = [d for m, d in http.calls if m == 'im.notify.system.add']
        self.assertTrue(notes and all(d['USER_ID'] == '42' for d in notes))
        self.assertTrue(any(str(ticket.pk) in d['MESSAGE'] for d in notes))
        self.assertEqual(http.methods().count('user.get'), 1)

    @override_settings(**BX)
    def test_bitrix_transient_error_is_retried(self):
        User.objects.filter(pk=self.author.pk).update(notify_email=False, bitrix_user_id='42')
        http = FakeHttp({"im.notify.system.add": (503, {"error": "QUERY_LIMIT_EXCEEDED"})})
        self.approve(self.make_ticket())
        with patch('feedback.channels._http_post', http):
            process_outbox()
        delivery = ExternalDelivery.objects.filter(channel='bitrix24').first()
        self.assertEqual((delivery.state, delivery.last_error), ('pending', 'bitrix_QUERY_LIMIT_EXCEEDED'))
        self.assertGreater(delivery.next_attempt_at, timezone.now())


class NotificationSettingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('ext-view', password='safe-test-password', role='author')

    def setUp(self):
        self.client.force_login(self.user)

    def test_save_email_and_send_test(self):
        url = reverse('feedback:notification_settings')
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'operation': 'save', 'email': 'me@example.com', 'notify_email': 'on'})
        self.assertRedirects(response, url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'me@example.com')
        self.client.post(url, {'operation': 'test'})
        self.assertEqual([m.to for m in mail.outbox], [['me@example.com']])

    @override_settings(**TG)
    def test_telegram_link_button_shows_deep_link(self):
        url = reverse('feedback:notification_settings')
        self.client.post(url, {'operation': 'telegram_link'})
        page = self.client.get(url).content.decode()
        self.assertIn('https://t.me/modus_test_bot?start=', page)
