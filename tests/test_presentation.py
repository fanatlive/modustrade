from datetime import timedelta
from io import StringIO
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.utils import timezone
from feedback.models import Ticket, User
from feedback.presentation import author_summary, RESULT_SOURCE
from tests import test_workflow as workflow


@override_settings(DEBUG=True, DEMO_MODE=True)
class AuthorSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workflow.WorkflowTests.setUpTestData.__func__(cls)
    make_ticket = workflow.WorkflowTests.make_ticket
    command = workflow.WorkflowTests.command

    def test_next_step_changes_with_workflow(self):
        ticket = self.make_ticket()
        self.client.force_login(self.author)
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Что происходит дальше')
        self.assertContains(response, 'Первый ответ ожидается до')
        self.command(ticket, 'start')
        self.command(ticket, 'ask_info', question='Какой документ устарел?')
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Нужен ваш ответ')
        self.assertContains(response, 'href="#info-answer"')
        self.command(ticket, 'answer_info', actor=self.author, message='Версия инструкции за прошлый год')
        self.command(ticket, 'approve', reason='Проверим', criteria='Новая инструкция доступна', plan_due_at=timezone.now()+timedelta(days=3))
        self.assertContains(self.client.get(f'/requests/{ticket.pk}/'), 'Решение принято, работа выполняется')
        self.command(ticket, 'implement', result='Инструкция обновлена', evidence='Проверено в тесте')
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Результат рассмотрения готов')
        content = response.content.decode()
        self.assertLess(content.index('id="result"'), content.index('id="conversation"'))
        self.assertNotContains(response, 'Открыть памятку — результат демо')

    def test_expired_reopen_does_not_offer_failing_action(self):
        ticket = self.make_ticket()
        self.command(ticket, 'start')
        self.command(ticket, 'decline', reason='Причина отказа для проверки')
        Ticket.objects.filter(pk=ticket.pk).update(closed_at=timezone.now()-timedelta(days=30))
        self.client.force_login(self.author)
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Срок самостоятельного повторного открытия истёк')
        self.assertNotContains(response, 'id="reopen-reason"')
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(f'/requests/{ticket.pk}/'), 'id="reopen-reason"')

    def test_real_deadline_overdue_and_unassigned_are_visible(self):
        ticket = self.make_ticket()
        cycle = ticket.current_cycle
        cycle.first_response_due_at = timezone.now()-timedelta(days=1)
        cycle.save()
        ticket.assignee = None
        ticket.save()
        self.client.force_login(self.author)
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Срок прошёл')
        self.assertContains(response, 'Координатор очереди')
        self.assertTrue(author_summary(ticket, cycle, None)['overdue'])


@override_settings(DEBUG=True, DEMO_MODE=True)
class DemoPresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_hackathon_demo', stdout=StringIO())

    def test_result_requires_access_and_completed_demo(self):
        ticket = Ticket.objects.get(source_record_id=RESULT_SOURCE)
        self.client.force_login(User.objects.get(username='hack_author'))
        self.assertContains(self.client.get(f'/requests/{ticket.pk}/'), 'Открыть памятку — результат демо')
        self.assertContains(self.client.get(f'/requests/{ticket.pk}/demo-result/'), 'Как согласовать закупку')
        self.assertContains(self.client.get('/demo/'), 'Готовый результат')
        stranger = User.objects.create_user('stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(f'/requests/{ticket.pk}/demo-result/').status_code, 404)
        self.assertNotContains(self.client.get('/demo/'), ticket.description)
        self.client.force_login(User.objects.get(username='hack_author'))
        with override_settings(DEMO_MODE=False):
            self.assertEqual(self.client.get('/demo/').status_code, 404)
            self.assertEqual(self.client.get(f'/requests/{ticket.pk}/demo-result/').status_code, 404)
        root = Ticket.objects.get(source_record_id='HACKATHON-V1-1')
        self.assertEqual(self.client.get(f'/requests/{root.pk}/demo-result/').status_code, 404)

    def test_demo_cards_have_readable_numbers(self):
        numbers = dict(Ticket.objects.filter(source_record_id__startswith='HACKATHON-').values_list('source_record_id', 'number'))
        self.assertEqual(numbers, {f'HACKATHON-V1-{i}': f'MT-D-00{i}' for i in range(1, 6)} | {RESULT_SOURCE: 'MT-D-006'})
        author = User.objects.get(username='hack_author')
        self.client.force_login(author)
        self.assertContains(self.client.get('/'), 'MT-D-006')

    def test_repeat_preserves_result_and_deadlines(self):
        before = list(Ticket.objects.order_by('pk').values())
        call_command('load_hackathon_demo', stdout=StringIO())
        self.assertEqual(before, list(Ticket.objects.order_by('pk').values()))
        self.assertEqual(Ticket.objects.count(), 6)
        self.assertEqual(User.objects.get(username='hack_author').first_name, 'Анна — автор демо')
