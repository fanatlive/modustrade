"""The processor's "next step" panel keeps the main decision visible and the page quiet."""
from uuid import uuid4

from django.test import TestCase

from feedback.models import Category, Queue, User
from feedback.services import create_ticket, execute_command


class StaffNextStepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user('ns-author', password='safe-test-password', role='author')
        cls.processor = User.objects.create_user('ns-processor', password='safe-test-password', role='processor')
        cls.coordinator = User.objects.create_user('ns-coordinator', password='safe-test-password', role='coordinator', restricted_access=True)
        cls.category = Category.objects.create(code='ns', name='Проверки')
        queue = Queue.objects.create(code='default', name='Разбор', owner=cls.coordinator)
        queue.members.add(cls.processor, cls.coordinator)
        Queue.objects.create(code='restricted', name='Конфиденциально', restricted=True, owner=cls.coordinator).members.add(cls.coordinator)

    def make_ticket(self, request_type='SUGGESTION'):
        return create_ticket(actor=self.author, data={'category': self.category, 'request_type': request_type,
            'description': 'Предлагаю улучшить проверяемый рабочий процесс.', 'reported_priority': 'MEDIUM',
            'confidentiality': 'STANDARD'}, idempotency_key=str(uuid4()))

    def start(self, ticket):
        execute_command(actor=self.processor, ticket_id=ticket.pk, command='start', expected_version=ticket.version, data={})
        ticket.refresh_from_db()
        return ticket

    def page(self, ticket, user):
        self.client.force_login(user)
        return self.client.get(f'/requests/{ticket.pk}/')

    def test_review_shows_decision_buttons_on_top(self):
        ticket = self.start(self.make_ticket())
        html = self.page(ticket, self.processor).content.decode()
        self.assertIn('Ваш следующий шаг', html)
        self.assertIn('Примите решение', html)
        for anchor in ('#action-approve', '#action-decline', '#action-ask'):
            self.assertIn(f'href="{anchor}"', html)
        # The decision panel comes before the description and the side column.
        self.assertLess(html.index('id="next-step"'), html.index('<h2>Описание</h2>'))
        self.assertLess(html.index('id="next-step"'), html.index('class="detail-aside"'))
        self.assertNotIn('Работа с обращением', html)

    def test_new_ticket_offers_start(self):
        html = self.page(self.make_ticket(), self.processor).content.decode()
        self.assertIn('Новое обращение ждёт разбора', html)
        self.assertIn('value="start"', html)

    def test_author_does_not_see_staff_panel(self):
        ticket = self.start(self.make_ticket())
        response = self.page(ticket, self.author)
        self.assertContains(response, 'Что происходит дальше')
        self.assertNotContains(response, 'Ваш следующий шаг')
        self.assertNotContains(response, 'value="approve"')

    def test_praise_offers_acknowledge_not_approve(self):
        ticket = self.start(self.make_ticket('PRAISE'))
        response = self.page(ticket, self.processor)
        self.assertContains(response, 'Обработайте положительный отзыв')
        self.assertContains(response, 'href="#action-acknowledge"')
        self.assertNotContains(response, 'href="#action-approve"')

    def test_validation_error_reopens_the_same_form(self):
        ticket = self.start(self.make_ticket())
        self.client.force_login(self.processor)
        response = self.client.post(f'/requests/{ticket.pk}/command/', {
            'command': 'approve', 'version': ticket.version, 'reason': 'Сделаем', 'criteria': '', 'plan_due_at': ''})
        self.assertEqual(response.status_code, 422)
        html = response.content.decode()
        self.assertIn('class="decision-panel is-open" id="action-approve"', html)
        self.assertIn('class="decision-panel" id="action-decline"', html)

    def test_empty_sections_are_hidden(self):
        ticket = self.start(self.make_ticket())
        html = self.page(ticket, self.processor).content.decode()
        self.assertNotIn('Связанные обращения', html)
        self.assertNotIn('Циклы рассмотрения', html)
        self.assertIn('<details class="surface content-section history-disclosure history-panel" id="history" >', html)

    def test_coordinator_sees_waiting_info_actions(self):
        ticket = self.start(self.make_ticket())
        execute_command(actor=self.processor, ticket_id=ticket.pk, command='ask_info', expected_version=ticket.version,
                        data={'question': 'Какой результат ожидается?'})
        ticket.refresh_from_db()
        response = self.page(ticket, self.coordinator)
        self.assertContains(response, 'Ждём ответа автора')
        for anchor in ('#action-resume', '#action-extend', '#action-no-response'):
            self.assertContains(response, f'href="{anchor}"')
        processor_view = self.page(ticket, self.processor)
        self.assertNotContains(processor_view, 'href="#action-resume"')
