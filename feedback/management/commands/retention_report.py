"""Preview business retention candidates without erasing audit evidence."""
import json
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from feedback.models import Ticket
from feedback.permissions import can_coordinate, visible_tickets
from feedback.models import User


class Command(BaseCommand):
    help = 'Отчёт по закрытым карточкам до явно заданной даты. Данные не удаляет.'

    def add_arguments(self, parser):
        parser.add_argument('--actor', required=True)
        parser.add_argument('--closed-before', required=True, help='Согласованная дата YYYY-MM-DD')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(username=options['actor'], is_active=True)
            cutoff = timezone.make_aware(datetime.combine(datetime.strptime(options['closed_before'], '%Y-%m-%d').date(), time.min))
        except (ValueError, User.DoesNotExist) as error:
            raise CommandError('Проверьте действующего координатора и дату YYYY-MM-DD.') from error
        if not can_coordinate(actor):
            raise CommandError('Требуется роль координатора.')
        candidates = visible_tickets(actor).filter(status='CLOSED', closed_at__lt=cutoff).order_by('closed_at')
        self.stdout.write(json.dumps({'dry_run': True, 'count': candidates.count(),
            'tickets': list(candidates.values('number', 'closed_at')[:max(1, min(options['limit'], 1000))])}, default=str))
