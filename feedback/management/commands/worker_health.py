from django.core.management.base import BaseCommand, CommandError
from feedback.operations import operations_snapshot


class Command(BaseCommand):
    help = 'Container health check for the notification worker.'

    def handle(self, *args, **options):
        if not operations_snapshot()['worker_healthy']:
            raise CommandError('Worker heartbeat is absent, stale, or failed.')
        self.stdout.write('ok')
