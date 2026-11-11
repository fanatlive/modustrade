"""Real row-lock behavior, executed only against PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from feedback.models import Category, Event, Queue, Ticket, User
from feedback.services import DomainError, create_ticket, execute_command


@skipUnless(connection.vendor == 'postgresql', 'Row locking requires PostgreSQL')
class ConcurrentCommandsTests(TransactionTestCase):
    def setUp(self):
        self.author = User.objects.create_user('race-author', role='author')
        self.processor = User.objects.create_user('race-processor', role='processor')
        self.queue = Queue.objects.create(code='default', name='Разбор', owner=self.processor)
        self.queue.members.add(self.processor)
        self.category = Category.objects.create(code='race', name='Параллельные запросы')

    def race(self, work):
        barrier = Barrier(2)
        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return work()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(run) for _ in range(2)]
            return [job.result(timeout=20) for job in jobs]

    def test_concurrent_creation_returns_one_identity(self):
        def work():
            actor = User.objects.get(pk=self.author.pk)
            ticket = create_ticket(actor=actor, idempotency_key='racing-create', data={
                'category': self.category.pk, 'request_type': 'PROBLEM',
                'description': 'Одинаковая параллельная отправка одного обращения.'})
            return str(ticket.pk)
        result = self.race(work)
        self.assertEqual(result[0], result[1])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Event.objects.count(), 1)

    def test_concurrent_mutation_has_one_winner(self):
        ticket = create_ticket(actor=self.author, idempotency_key='initial', data={
            'category': self.category, 'request_type': 'SUGGESTION',
            'description': 'Конкурентное изменение не перезаписывает карточку.'})
        def work():
            try:
                execute_command(actor=User.objects.get(pk=self.processor.pk), ticket_id=ticket.pk,
                                command='start', expected_version=1, data={})
                return 'saved'
            except DomainError as exc:
                return exc.status
        self.assertCountEqual(self.race(work), ['saved', 409])
        ticket.refresh_from_db()
        self.assertEqual(ticket.version, 2)
        self.assertEqual(ticket.events.count(), 2)
