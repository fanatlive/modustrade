"""Business invariants, not snapshots of implementation details."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from feedback.models import (Category, Comment, Decision, Event, ImportRecord,
    InfoRequest, Notification, OrgUnit, Outbox, ProcessingCycle, Queue,
    RoutingRule, Ticket, User)
from feedback.permissions import can_process, visible_tickets
from feedback.services import DomainError, create_ticket, execute_command, process_outbox
from feedback.sla import add_business_hours, business_seconds


class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.unit = OrgUnit.objects.create(code='test', name='Тестовое подразделение')
        cls.author = User.objects.create_user('test-author', password='safe-test-password', role='author', org_unit=cls.unit)
        cls.other = User.objects.create_user('test-other', password='safe-test-password', role='author')
        cls.processor = User.objects.create_user('test-processor', password='safe-test-password', role='processor')
        cls.coordinator = User.objects.create_user('test-coordinator', password='safe-test-password', role='coordinator', restricted_access=True)
        cls.admin = User.objects.create_user('test-admin', password='safe-test-password', role='admin')
        cls.category = Category.objects.create(code='testing', name='Проверки')
        cls.queue = Queue.objects.create(code='default', name='Первичный разбор', owner=cls.coordinator)
        cls.queue.members.add(cls.processor, cls.coordinator)
        cls.restricted = Queue.objects.create(code='restricted', name='Конфиденциальный разбор', restricted=True, owner=cls.coordinator)
        cls.restricted.members.add(cls.coordinator)

    def make_ticket(self, **overrides):
        data = {'category': self.category, 'request_type': 'SUGGESTION',
                'description': 'Предлагаю улучшить проверяемый рабочий процесс.',
                'reported_priority': 'MEDIUM', 'confidentiality': 'STANDARD'}
        data.update(overrides)
        return create_ticket(actor=self.author, data=data, idempotency_key=str(uuid4()))

    def command(self, ticket, command, actor=None, **data):
        result = execute_command(actor=actor or self.processor, ticket_id=ticket.pk,
                                 command=command, expected_version=ticket.version, data=data)
        ticket.refresh_from_db()
        return result

    def review(self, ticket):
        self.command(ticket, 'start')
        return ticket

    def approve(self, ticket):
        self.review(ticket)
        self.command(ticket, 'approve', reason='Полезное улучшение',
                     criteria='Проверяемый результат доступен сотрудникам',
                     plan_due_at=(timezone.now()+timedelta(days=10)).isoformat())
        return ticket

    def test_registration_is_atomic_and_has_fallback(self):
        ticket = self.make_ticket()
        self.assertEqual(ticket.queue, self.queue)
        self.assertEqual(ticket.author, self.author)
        self.assertEqual(ticket.events.count(), 1)
        self.assertEqual(ticket.cycles.count(), 1)
        self.assertTrue(Outbox.objects.filter(ticket=ticket).exists())

    def test_same_creation_key_returns_one_ticket(self):
        data = {'category': self.category, 'request_type': 'PROBLEM', 'description': 'Повтор запроса не создаёт второе обращение.'}
        first = create_ticket(actor=self.author, data=data, idempotency_key='same-key')
        second = create_ticket(actor=self.author, data=data, idempotency_key='same-key')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_idempotency_key_cannot_change_payload(self):
        data = {'category': self.category, 'request_type': 'PROBLEM', 'description': 'Первое содержание проверяемого обращения.'}
        create_ticket(actor=self.author, data=data, idempotency_key='conflicting-key')
        data['description'] = 'Другое содержание с тем же ключом запроса.'
        with self.assertRaises(DomainError) as exc:
            create_ticket(actor=self.author, data=data, idempotency_key='conflicting-key')
        self.assertEqual(exc.exception.status, 409)

    def test_failed_history_write_rolls_back_registration(self):
        with patch('feedback.services.Event.objects.create', side_effect=RuntimeError('history unavailable')):
            with self.assertRaises(RuntimeError):
                self.make_ticket()
        self.assertEqual(Ticket.objects.count(), 0)

    def test_foreign_author_and_admin_do_not_gain_read_access(self):
        ticket = self.make_ticket()
        self.assertFalse(visible_tickets(self.other).filter(pk=ticket.pk).exists())
        self.assertFalse(visible_tickets(self.admin).filter(pk=ticket.pk).exists())
        self.assertFalse(can_process(self.author, ticket))

    def test_restricted_queue_and_explicit_clearance(self):
        ticket = self.make_ticket(confidentiality='RESTRICTED')
        self.assertTrue(ticket.queue.restricted)
        self.assertFalse(visible_tickets(self.processor).filter(pk=ticket.pk).exists())
        self.assertTrue(visible_tickets(self.author).filter(pk=ticket.pk).exists())
        self.assertTrue(can_process(self.coordinator, ticket))

    def test_invalid_transition_does_not_change_history(self):
        ticket = self.make_ticket()
        with self.assertRaises(DomainError):
            self.command(ticket, 'implement', result='Результат', evidence='Проверка')
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'NEW')
        self.assertEqual(ticket.events.count(), 1)

    def test_approval_needs_acceptance_criteria(self):
        ticket = self.review(self.make_ticket())
        with self.assertRaises(DomainError):
            self.command(ticket, 'approve', reason='Выполнить', criteria='', plan_due_at=(timezone.now()+timedelta(days=2)).isoformat())
        self.assertEqual(Decision.objects.count(), 0)

    def test_decline_needs_a_reason(self):
        ticket = self.review(self.make_ticket())
        with self.assertRaises(DomainError):
            self.command(ticket, 'decline', reason='')
        self.assertEqual(ticket.status, 'REVIEW')

    def test_implementation_and_reopen_preserve_cycle(self):
        ticket = self.approve(self.make_ticket())
        self.assertEqual(ticket.status, 'IN_PROGRESS')
        self.command(ticket, 'implement', result='Изменённый процесс доступен', evidence='Проверено по согласованному критерию')
        self.assertEqual(ticket.resolution_code, 'IMPLEMENTED')
        first = ticket.cycles.get(number=1)
        self.command(ticket, 'reopen', actor=self.author, reason='Обнаружен непокрытый сценарий')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertEqual(ticket.cycle_no, 2)
        first.refresh_from_db()
        self.assertEqual(first.resolution_code, 'IMPLEMENTED')
        self.assertIsNotNone(first.closed_at)

    def test_stale_version_cannot_overwrite(self):
        ticket = self.make_ticket()
        old_version = ticket.version
        self.command(ticket, 'start')
        with self.assertRaises(DomainError) as exc:
            execute_command(actor=self.processor, ticket_id=ticket.pk, command='comment', expected_version=old_version, data={'message': 'Устаревшее сообщение'})
        self.assertEqual(exc.exception.status, 409)

    def test_author_cannot_write_internal_comment(self):
        ticket = self.make_ticket()
        with self.assertRaises((PermissionDenied, DomainError)):
            self.command(ticket, 'comment', actor=self.author, message='Скрытое сообщение', visibility='internal')
        self.assertEqual(Comment.objects.count(), 0)

    def test_internal_comment_does_not_count_as_first_response(self):
        ticket = self.review(self.make_ticket())
        self.command(ticket, 'comment', message='Внутренняя служебная заметка', visibility='internal')
        self.assertIsNone(ticket.current_cycle.first_response_at)
        self.assertFalse(Outbox.objects.filter(ticket=ticket, recipient=self.author, event__kind='COMMENT').exists())

    def test_information_round_trip_and_human_response(self):
        ticket = self.review(self.make_ticket())
        self.command(ticket, 'ask_info', question='Какой конкретно этап требует изменения?', due_at=(timezone.now()+timedelta(days=4)).isoformat())
        self.assertEqual(ticket.status, 'WAITING_INFO')
        self.assertIsNotNone(ticket.current_cycle.first_response_at)
        with self.assertRaises((PermissionDenied, DomainError)):
            self.command(ticket, 'answer_info', actor=self.other, response='Чужой ответ', message='Чужой ответ')
        self.command(ticket, 'answer_info', actor=self.author, response='Этап согласования заявки', message='Этап согласования заявки')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertFalse(ticket.info_requests.filter(answered_at__isnull=True).exists())

    def test_notification_retry_does_not_duplicate(self):
        ticket = self.make_ticket()
        process_outbox()
        count = Notification.objects.count()
        self.assertGreater(count, 0)
        process_outbox()
        self.assertEqual(Notification.objects.count(), count)
        self.assertEqual(Outbox.objects.filter(ticket=ticket, state='pending').count(), 0)

    def test_revoked_recipient_cannot_receive_notification(self):
        ticket = self.make_ticket()
        self.processor.is_active = False
        self.processor.save(update_fields=['is_active'])
        process_outbox()
        self.assertFalse(Notification.objects.filter(user=self.processor, ticket=ticket).exists())

    def test_database_rejects_empty_closed_result(self):
        ticket = self.make_ticket()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.filter(pk=ticket.pk).update(status='CLOSED')

    def test_praise_is_acknowledged_not_implemented(self):
        ticket = self.review(self.make_ticket(request_type='PRAISE'))
        self.command(ticket, 'acknowledge', reason='Отзыв передан организаторам')
        self.assertEqual(ticket.resolution_code, 'ACKNOWLEDGED')

    def test_author_cannot_self_approve(self):
        ticket = self.review(self.make_ticket())
        with self.assertRaises((PermissionDenied, DomainError)):
            self.command(ticket, 'approve', actor=self.author, reason='Я решил', criteria='Мои условия', plan_due_at=(timezone.now()+timedelta(days=1)).isoformat())

    def test_withdrawal_consent_cannot_be_reused_after_reopening(self):
        ticket = self.approve(self.make_ticket())
        self.command(ticket, 'withdraw', actor=self.author, reason='Прошу остановить работу')
        self.assertEqual(ticket.status, 'IN_PROGRESS')
        self.command(ticket, 'withdraw', actor=self.coordinator, reason='Остановка согласована')
        self.command(ticket, 'reopen', actor=self.author, reason='Вопрос снова актуален')
        with self.assertRaises(PermissionDenied):
            self.command(ticket, 'withdraw', actor=self.coordinator, reason='Повторная остановка без нового запроса')
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'REVIEW')

    def test_failed_delivery_is_retried_without_losing_ticket(self):
        ticket = self.make_ticket()
        with patch('feedback.services.Notification.objects.get_or_create', side_effect=RuntimeError('channel unavailable')):
            result = process_outbox()
        self.assertGreater(result['failed'], 0)
        self.assertTrue(Ticket.objects.filter(pk=ticket.pk).exists())
        self.assertFalse(Notification.objects.exists())
        Outbox.objects.filter(ticket=ticket, state='pending').update(next_attempt_at=timezone.now())
        process_outbox()
        self.assertGreater(Notification.objects.count(), 0)
        count = Notification.objects.count()
        process_outbox()
        self.assertEqual(Notification.objects.count(), count)

    def test_expired_worker_lease_is_recovered(self):
        ticket = self.make_ticket()
        Outbox.objects.filter(ticket=ticket).update(state='processing', locked_until=timezone.now()-timedelta(minutes=3))
        result = process_outbox()
        self.assertGreater(result['sent'], 0)
        self.assertFalse(Outbox.objects.filter(ticket=ticket, state='processing').exists())

    def test_information_reminder_and_expiry_are_not_duplicated(self):
        ticket = self.review(self.make_ticket())
        self.command(ticket, 'ask_info', question='Уточните данные для рассмотрения')
        info = ticket.info_requests.get(answered_at__isnull=True)
        info.due_at = timezone.now()+timedelta(minutes=30)
        info.reminder_due_at = timezone.now()
        info.save(update_fields=['due_at', 'reminder_due_at'])
        process_outbox()
        process_outbox()
        self.assertEqual(ticket.events.filter(kind='INFO_REMINDER').count(), 1)
        info.due_at = timezone.now()-timedelta(minutes=1)
        info.save(update_fields=['due_at'])
        process_outbox()
        process_outbox()
        self.assertEqual(ticket.events.filter(kind='INFO_EXPIRED').count(), 1)
        ticket.refresh_from_db()
        with self.assertRaises(DomainError):
            self.command(ticket, 'no_response', actor=self.coordinator, reason='Напоминание доставлено недавно')
        info.refresh_from_db()
        grace = add_business_hours(info.reminder_delivered_at, 8, policy=ticket.current_cycle.policy)
        with patch('feedback.services.timezone.now', return_value=grace):
            self.command(ticket, 'no_response', actor=self.coordinator, reason='Необходимые сведения не получены после напоминания')
        self.assertEqual(ticket.resolution_code, 'NO_RESPONSE')

    def test_calendar_skips_weekends_and_lunch(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Bishkek')
        friday = datetime(2026, 11, 13, 17, tzinfo=zone)
        monday = add_business_hours(friday, 2)
        self.assertEqual((monday.weekday(), monday.hour), (0, 10))
        self.assertEqual(business_seconds(friday, monday), 7200)


class DemoImportTests(TestCase):
    def test_repeat_import_preserves_all_twenty_and_runtime_changes(self):
        output = StringIO()
        call_command('load_demo', stdout=output)
        self.assertEqual(ImportRecord.objects.count(), 20)
        self.assertEqual(Ticket.objects.filter(is_demo=True).count(), 20)
        ticket = Ticket.objects.get(source_record_id='A-004')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertTrue(ticket.quality_flags)
        self.assertIsNone(ticket.submitted_at)
        ticket.description = 'Добавленное пользователем изменение сохраняется при повторе.'
        ticket.save(update_fields=['description'])
        actor = User.objects.get(username='author')
        password = actor.password
        call_command('load_demo', stdout=output)
        ticket.refresh_from_db()
        actor.refresh_from_db()
        self.assertEqual(ImportRecord.objects.count(), 20)
        self.assertEqual(Ticket.objects.filter(is_demo=True).count(), 20)
        self.assertTrue(ticket.description.startswith('Добавленное пользователем'))
        self.assertEqual(actor.password, password)

    def test_demo_cli_rejects_unsafe_environment(self):
        from django.test import override_settings
        from django.core.management.base import CommandError
        with override_settings(DEBUG=False, DEMO_MODE=False):
            with self.assertRaises(CommandError):
                call_command('load_demo', stdout=StringIO())
