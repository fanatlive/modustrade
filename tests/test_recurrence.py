from datetime import timedelta
from io import StringIO
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from feedback.models import Ticket, TicketRelation, User
from feedback.recurrence import recurring_tickets
from tests import test_workflow as workflow


@override_settings(DEBUG=True, DEMO_MODE=True)
class RecurrenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workflow.WorkflowTests.setUpTestData.__func__(cls)

    make_ticket = workflow.WorkflowTests.make_ticket
    command = workflow.WorkflowTests.command

    def setUp(self):
        self.root = self.make_ticket()
        self.child = self.make_ticket(description='Повтор той же проверяемой проблемы.')
        self.command(self.child, 'start')
        self.command(self.child, 'duplicate', canonical_id=str(self.root.pk), reason='Одна причина')
        self.client.force_login(self.processor)

    def test_report_and_bidirectional_links(self):
        report = self.client.get('/recurring/?mode=demo')
        self.assertContains(report, 'Повторов: 1')
        self.assertContains(self.client.get(f'/requests/{self.root.pk}/'), self.child.number)
        self.assertContains(self.client.get(f'/requests/{self.child.pk}/'), self.root.number)

    def test_hidden_child_not_counted_or_linked(self):
        Ticket.objects.filter(pk=self.child.pk).update(confidentiality='RESTRICTED', queue=self.restricted, assignee=self.coordinator)
        self.assertFalse(recurring_tickets(self.processor, demo=True).exists())
        response = self.client.get(f'/requests/{self.root.pk}/')
        self.assertNotContains(response, self.child.number)
        self.assertNotContains(response, str(self.child.pk))

    def test_hidden_parent_not_exposed_to_child_author(self):
        Ticket.objects.filter(pk=self.root.pk).update(author=self.other, confidentiality='RESTRICTED', queue=self.restricted, assignee=self.coordinator)
        self.client.force_login(self.author)
        response = self.client.get(f'/requests/{self.child.pk}/')
        self.assertNotContains(response, self.root.number)
        self.assertNotContains(response, str(self.root.pk))
        self.assertFalse(recurring_tickets(self.author, demo=True).exists())

    def test_reopen_removes_active_link(self):
        self.command(self.child, 'reopen', actor=self.coordinator, reason='Нужна отдельная проверка')
        self.assertFalse(recurring_tickets(self.processor, demo=True).exists())
        self.assertNotContains(self.client.get(f'/requests/{self.root.pk}/'), self.child.number)

    def test_demo_and_production_do_not_mix(self):
        Ticket.objects.filter(pk=self.child.pk).update(is_demo=False)
        self.assertFalse(recurring_tickets(self.processor, demo=True).exists())
        self.assertFalse(recurring_tickets(self.processor, demo=False).exists())
        Ticket.objects.filter(pk=self.root.pk).update(is_demo=False)
        self.assertEqual(recurring_tickets(self.processor, demo=False).get().repeat_count, 1)

    def test_period_applies_to_repeat_not_parent_and_unknown_dates_excluded(self):
        today = timezone.localdate()
        Ticket.objects.filter(pk=self.root.pk).update(submitted_at=timezone.now()-timedelta(days=30))
        self.assertEqual(recurring_tickets(self.processor, demo=True, date_from=today).count(), 1)
        Ticket.objects.filter(pk=self.child.pk).update(submitted_at=None)
        self.assertFalse(recurring_tickets(self.processor, demo=True, date_from=today).exists())
        self.assertEqual(recurring_tickets(self.processor, demo=True).count(), 1)

    def test_invalid_period_and_anonymous(self):
        self.assertEqual(self.client.get('/recurring/?mode=demo&date_from=2026-12-01&date_to=2026-01-01').status_code, 422)
        self.client.logout()
        self.assertEqual(self.client.get('/recurring/').status_code, 302)

    def test_direct_links_not_transitive(self):
        other = self.make_ticket()
        TicketRelation.objects.create(ticket=self.root, canonical=other)
        rows = {t.pk: t.repeat_count for t in recurring_tickets(self.processor, demo=True)}
        self.assertEqual(rows, {self.root.pk: 1, other.pk: 1})


@override_settings(DEMO_MODE=True)
class HackathonDemoTests(TestCase):
    def test_import_repeat_preserves_work_password_and_source_set(self):
        call_command('load_demo', stdout=StringIO())
        before = list(Ticket.objects.filter(source_record_id__startswith='A-').order_by('pk').values())
        self.assertEqual(len(before), 20)
        call_command('load_hackathon_demo', stdout=StringIO())
        self.assertEqual(Ticket.objects.filter(source_record_id__startswith='HACKATHON-').count(), 6)
        self.assertEqual(TicketRelation.objects.filter(active=True, ticket__source_record_id__startswith='HACKATHON-').count(), 3)
        self.assertEqual(before, list(Ticket.objects.filter(source_record_id__startswith='A-').order_by('pk').values()))
        card = Ticket.objects.get(source_record_id='HACKATHON-V1-5')
        Ticket.objects.filter(pk=card.pk).update(description='Изменённый пользователем текст обращения')
        account = User.objects.get(username='hack_author')
        account.set_password('changed-password')
        account.save()
        call_command('load_hackathon_demo', stdout=StringIO())
        card.refresh_from_db(); account.refresh_from_db()
        self.assertEqual(card.description, 'Изменённый пользователем текст обращения')
        self.assertTrue(account.check_password('changed-password'))

    @override_settings(DEMO_MODE=False)
    def test_production_rejected(self):
        with self.assertRaises(CommandError):
            call_command('load_hackathon_demo', stdout=StringIO())
        self.assertFalse(User.objects.exists())
