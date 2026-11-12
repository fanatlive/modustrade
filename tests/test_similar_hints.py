"""Similar-request hints: create-form preview and the handler's card stay inside access scope."""
from uuid import uuid4

from django.test import TestCase, override_settings

from feedback.search import suggest_similar
from feedback.services import create_ticket
from feedback.models import Ticket
from tests import test_workflow as workflow

INSTRUCTION = 'Сотрудники не находят актуальную инструкцию согласования закупок на портале.'
PARAPHRASE = 'Актуальная инструкция согласования закупок спрятана, найти её на портале сложно.'
UNRELATED = 'Предлагаю поставить кулер с водой рядом с переговорной комнатой.'


@override_settings(DEBUG=True, DEMO_MODE=False)
class SimilarHintsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workflow.WorkflowTests.setUpTestData.__func__(cls)
    make_ticket = workflow.WorkflowTests.make_ticket
    command = workflow.WorkflowTests.command

    def other_ticket(self, description, **extra):
        data = {'category': self.category, 'request_type': 'PROBLEM', 'description': description,
                'reported_priority': 'MEDIUM', 'confidentiality': 'STANDARD', **extra}
        return create_ticket(actor=self.other, data=data, idempotency_key=str(uuid4()))

    def preview(self, user, text):
        self.client.force_login(user)
        return self.client.get('/requests/similar-preview/', {'text': text}).json()['results']

    def test_ranking_needs_two_shared_keywords(self):
        match = self.make_ticket(description=INSTRUCTION)
        self.make_ticket(description=UNRELATED)
        found = suggest_similar(Ticket.objects.all(), PARAPHRASE)
        self.assertEqual([t.pk for t in found], [match.pk])
        self.assertEqual(suggest_similar(Ticket.objects.all(), 'Нужна новая переговорная'), [])

    def test_author_preview_only_shows_own_tickets(self):
        own = self.make_ticket(description=INSTRUCTION)
        foreign = self.other_ticket(INSTRUCTION + ' Ещё раз.')
        results = self.preview(self.author, PARAPHRASE)
        self.assertEqual([r['number'] for r in results], [own.number])
        self.assertNotIn(foreign.number, str(results))
        self.assertEqual(self.preview(self.admin, PARAPHRASE), [])

    def test_preview_requires_login_and_meaningful_text(self):
        self.make_ticket(description=INSTRUCTION)
        response = self.client.get('/requests/similar-preview/', {'text': PARAPHRASE})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.preview(self.author, 'инструкция'), [])

    def test_restricted_ticket_hidden_from_processor_without_clearance(self):
        secret = self.other_ticket(INSTRUCTION, confidentiality='RESTRICTED')
        self.assertNotIn(secret.number, str(self.preview(self.processor, PARAPHRASE)))
        self.assertIn(secret.number, str(self.preview(self.coordinator, PARAPHRASE)))

    def test_handler_card_lists_repeats_author_card_does_not(self):
        ticket = self.make_ticket(description=PARAPHRASE)
        repeat = self.other_ticket(INSTRUCTION)
        self.client.force_login(self.processor)
        response = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(response, 'Возможные повторы')
        self.assertContains(response, repeat.number)
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(f'/requests/{ticket.pk}/'), 'Возможные повторы')

    def test_linked_duplicate_is_not_suggested_again(self):
        root = self.make_ticket(description=PARAPHRASE)
        repeat = self.other_ticket(INSTRUCTION)
        self.command(root, 'start')
        self.command(repeat, 'start')
        self.command(repeat, 'duplicate', canonical_id=str(root.pk), reason='Та же инструкция')
        self.client.force_login(self.processor)
        self.assertNotContains(self.client.get(f'/requests/{root.pk}/'), 'Возможные повторы')

    def test_known_duplicates_point_to_main_ticket_only(self):
        root = self.make_ticket(description=INSTRUCTION)
        repeat = self.make_ticket(description=INSTRUCTION + ' Повтор.')
        self.command(root, 'start')
        self.command(repeat, 'start')
        self.command(repeat, 'duplicate', canonical_id=str(root.pk), reason='Та же инструкция')
        self.assertEqual([r['number'] for r in self.preview(self.author, PARAPHRASE)], [root.number])
