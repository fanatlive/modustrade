"""Generated document stays synchronized across all supported write paths."""
from uuid import uuid4

from django.contrib.postgres.search import SearchQuery
from django.db import connection
from django.test import TestCase

from feedback.models import Category, Queue, Ticket, User
from feedback.search import scoped_search
from feedback.services import create_ticket, execute_command


class GeneratedSearchDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user('search-author')
        cls.processor = User.objects.create_user('search-processor', role='processor')
        cls.queue = Queue.objects.create(code='default', name='Search queue', owner=cls.processor)
        cls.queue.members.add(cls.processor)
        cls.category = Category.objects.create(code='search-generated', name='Search tests')

    def ticket(self, **overrides):
        values = dict(author=self.author, category=self.category, queue=self.queue,
                      assignee=self.processor, request_type='PROBLEM',
                      description='Уведомления о согласовании заявки', proposed_solution='Автоматизировать маршрутизацию')
        values.update(overrides)
        return Ticket.objects.create(**values)

    def matches(self, ticket, term):
        query = Ticket.objects.filter(pk=ticket.pk)
        if connection.vendor == 'postgresql':
            return query.filter(search_document=SearchQuery(term, config='russian')).exists()
        return scoped_search(query, term).exists()

    def test_create_generates_both_texts_and_sql_update_refreshes_document(self):
        ticket = self.ticket()
        self.assertTrue(self.matches(ticket, 'Уведомления'))
        self.assertTrue(self.matches(ticket, 'маршрутизацию'))
        Ticket.objects.filter(pk=ticket.pk).update(description='Складская инвентаризация', proposed_solution='Пересчитать остатки')
        ticket.refresh_from_db()
        self.assertFalse(self.matches(ticket, 'Уведомления'))
        self.assertFalse(self.matches(ticket, 'маршрутизацию'))
        self.assertTrue(self.matches(ticket, 'инвентаризация'))
        if connection.vendor == 'sqlite':
            self.assertEqual(ticket.search_document, '')

    def test_bulk_create_and_bulk_update_are_covered_without_application_hooks(self):
        tickets = [Ticket(author=self.author, category=self.category, queue=self.queue, request_type='SUGGESTION',
                          description='Обновить каталог оборудования', proposed_solution='Добавить классификацию'),
                   Ticket(author=self.author, category=self.category, queue=self.queue, request_type='SUGGESTION',
                          description='Уточнить расписание доставки', proposed_solution='Настроить календарь')]
        Ticket.objects.bulk_create(tickets)
        self.assertTrue(self.matches(tickets[0], 'оборудования'))
        self.assertTrue(self.matches(tickets[1], 'календарь'))
        for ticket in tickets:
            ticket.description = 'Проверить безопасность склада'
            ticket.proposed_solution = 'Улучшить освещение'
        Ticket.objects.bulk_update(tickets, ['description', 'proposed_solution'])
        self.assertFalse(self.matches(tickets[0], 'оборудования'))
        self.assertFalse(self.matches(tickets[1], 'календарь'))
        self.assertTrue(all(self.matches(ticket, 'освещение') for ticket in tickets))

    def test_json_updates_do_not_overwrite_generated_value(self):
        ticket = self.ticket(source_data={'origin':'synthetic', 'nested':{'check':True}})
        ticket.refresh_from_db()
        original = ticket.search_document
        ticket.source_data = {'origin':'synthetic', 'nested':{'check':False}, 'text':"' quote"}
        ticket.save(update_fields=['source_data'])
        ticket.source_data['batch'] = 2
        Ticket.objects.bulk_update([ticket], ['source_data'])
        ticket.refresh_from_db()
        self.assertEqual(ticket.search_document, original)
        self.assertEqual(ticket.source_data['batch'], 2)
        self.assertTrue(self.matches(ticket, 'Уведомления'))

    def test_business_command_does_not_attempt_to_assign_generated_column(self):
        ticket = create_ticket(actor=self.author, idempotency_key=str(uuid4()), data={
            'category':self.category, 'request_type':'PROBLEM', 'description':'Уведомления о согласовании заявки'})
        execute_command(actor=self.processor, ticket_id=ticket.pk, command='comment', expected_version=ticket.version,
                        data={'message':'Проверяем содержательный публичный ответ'})
        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 2)
        self.assertTrue(ticket.comments.exists())
        self.assertTrue(self.matches(ticket, 'Уведомления'))

    def test_generated_schema_and_search_index_exist(self):
        field = Ticket._meta.get_field('search_document')
        self.assertTrue(field.generated)
        self.assertTrue(field.db_persist)
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname='ticket_search_document_gin'")
                self.assertIn('USING gin (search_document)', cursor.fetchone()[0])
            else:
                cursor.execute('PRAGMA table_xinfo(feedback_ticket)')
                generated = next(row for row in cursor.fetchall() if row[1] == 'search_document')
                self.assertEqual(generated[-1], 3)
