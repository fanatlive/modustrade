"""Add an isolated, repeatable story without rewriting the source dataset."""
import os
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from feedback.models import Category, Queue, RoutingRule, Ticket, User
from feedback.services import create_ticket, execute_command

from feedback.presentation import RESULT_SOURCE

PREFIX = 'HACKATHON-V1-'
# Readable numbers for the presentation set, in the style of imported MT-A-0XX cards.
NUMBER_PREFIX = 'MT-D-'
RESULT_NUMBER = NUMBER_PREFIX + '006'
DISPLAY_NAMES = {'author': 'Анна — автор демо', 'processor': 'Илья — исполнитель демо', 'coordinator': 'Мария — координатор демо'}
DESCRIPTIONS = [
    'Синтетический пример: сотрудники не могут найти актуальную инструкцию согласования закупок.',
    'Синтетический повтор: в закупках используется устаревшая инструкция согласования.',
    'Синтетический повтор: бухгалтерия не знает, где актуальная инструкция согласования закупок.',
    'Синтетический повтор: ссылка на инструкцию согласования закупок ведёт на старую версию.',
    'Синтетическое новое обращение: нужна единая инструкция согласования закупок для новых сотрудников.',
]


class Command(BaseCommand):
    help = 'Добавляет 5 карточек сценария и завершённый пример с памяткой; повтор сохраняет работу.'

    def add_arguments(self, parser):
        parser.add_argument('--password', help='Пароль только новых демонстрационных пользователей')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError('Нужен DEMO_MODE=1 и отдельная демонстрационная БД.')
        password = options['password'] or os.getenv('DEMO_PASSWORD') or ('ModusDemo2026!' if settings.DEBUG else None)
        if not password:
            raise CommandError('Задайте DEMO_PASSWORD или --password.')
        users = {}
        for role in ('author', 'processor', 'coordinator'):
            user, created = User.objects.get_or_create(username='hack_' + role, defaults={
                'role': role, 'first_name': DISPLAY_NAMES[role], 'last_name': ''})
            if created:
                user.set_password(password)
                user.save(update_fields=['password'])
            if user.first_name == 'Демо защиты' and user.last_name == role:
                user.first_name, user.last_name = DISPLAY_NAMES[role], ''
                user.save(update_fields=['first_name', 'last_name'])
            if not user.is_active or user.role != role:
                raise CommandError('Демо-аккаунт изменён; используйте новую отдельную БД для чистой репетиции.')
            users[role] = user
        # Serialize repeated imports and keep all story creation atomic.
        User.objects.select_for_update().get(pk=users['author'].pk)
        existing = Ticket.objects.filter(source_record_id__startswith=PREFIX)
        if existing.exists():
            if existing.count() != 5:
                raise CommandError('Неполный набор защиты; используйте новую БД. Существующие карточки не изменены.')
            for ticket in existing.order_by('source_record_id'):
                self.stdout.write(f'{ticket.source_record_id}: /requests/{ticket.pk}/')
            self.ensure_result(users)
            self.stdout.write('Набор уже существует. Работа пользователей сохранена. Откройте /demo/.')
            return
        category, _ = Category.objects.get_or_create(code='hackathon-story', defaults={'name': 'Демо защиты — инструкции'})
        queue, created = Queue.objects.get_or_create(code='hackathon-story', defaults={'name': 'Демо защиты — разбор', 'owner': users['coordinator']})
        if created:
            queue.members.add(users['processor'], users['coordinator'])
        RoutingRule.objects.get_or_create(category=category, queue=queue, priority=10000, defaults={'request_type': ''})
        cards = []
        def command(ticket, name, **data):
            result = execute_command(actor=users['processor'], ticket_id=ticket.pk, command=name, expected_version=ticket.version, data=data)
            ticket.refresh_from_db()
            return result
        for index, description in enumerate(DESCRIPTIONS, 1):
            ticket = create_ticket(actor=users['author'], data={'category': category, 'request_type': 'PROBLEM', 'description': description}, idempotency_key=PREFIX + str(index), number=f'{NUMBER_PREFIX}{index:03d}')
            ticket.source_record_id = PREFIX + str(index)
            ticket.source_data = {'origin': 'hackathon_story_v1', 'synthetic': True, 'note': 'Не факт компании; отдельный сценарий защиты.'}
            ticket.save(update_fields=['source_record_id', 'source_data'])
            cards.append(ticket)
        root = cards[0]
        command(root, 'start')
        command(root, 'approve', reason='Синтетическое решение для демонстрации', criteria='Единая актуальная инструкция доступна по согласованной ссылке', plan_due_at=timezone.now() + timedelta(days=7))
        for ticket in cards[1:4]:
            command(ticket, 'start')
            command(ticket, 'duplicate', canonical_id=str(root.pk), reason='Проверено в синтетическом сценарии: одна и та же инструкция')
        for ticket in cards:
            self.stdout.write(f'{ticket.source_record_id}: /requests/{ticket.pk}/')
        self.ensure_result(users)
        self.stdout.write('Готово: основная в работе, 3 повтора, 1 новая карточка и завершённый пример. Откройте /demo/.')

    def ensure_result(self, users):
        existing = Ticket.objects.filter(source_record_id=RESULT_SOURCE).first()
        if existing:
            self.stdout.write(f'Готовый пример: /requests/{existing.pk}/')
            return
        category = Category.objects.get(code='hackathon-story')
        ticket = create_ticket(actor=users['author'], data={'category': category, 'request_type': 'SUGGESTION',
            'description': 'Учебный пример: опубликована единая памятка по согласованию закупки.'}, idempotency_key=RESULT_SOURCE,
            number=RESULT_NUMBER)
        ticket.source_record_id = RESULT_SOURCE
        ticket.source_data = {'origin': 'hackathon_result_v1', 'synthetic': True}
        ticket.save(update_fields=['source_record_id', 'source_data'])
        for name, data in [
            ('start', {}),
            ('approve', {'reason': 'Показать проверяемый результат в учебном сценарии',
                'criteria': 'Памятка доступна из карточки и описывает четыре этапа согласования',
                'plan_due_at': timezone.now() + timedelta(days=7)}),
            ('implement', {'result': 'Подготовлена демонстрационная памятка версии 1.0: потребность, рассмотрение, решение и проверка результата.',
                'evidence': 'Материал доступен по кнопке «Открыть памятку — результат демо». Применение в компании не проверялось.'}),
        ]:
            ticket = execute_command(actor=users['processor'], ticket_id=ticket.pk, command=name, expected_version=ticket.version, data=data)
        self.stdout.write(f'Готовый пример: /requests/{ticket.pk}/')
