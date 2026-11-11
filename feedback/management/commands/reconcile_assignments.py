import json

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import PermissionDenied

from feedback.models import User
from feedback.services import reconcile_assignments


class Command(BaseCommand):
    help = 'Проверяет открытые назначения; изменение только с явным --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--actor', required=True, help='Логин действующего координатора')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        try:
            actor = User.objects.get(username=options['actor'], is_active=True)
            result = reconcile_assignments(actor=actor, apply=options['apply'], limit=options['limit'])
        except (User.DoesNotExist, PermissionDenied) as error:
            raise CommandError('Требуется действующий координатор с доступом к очередям.') from error
        self.stdout.write(json.dumps({'applied': options['apply'], 'assignments': result}, ensure_ascii=False))
