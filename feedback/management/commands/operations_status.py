import json
from django.core.management.base import BaseCommand, CommandError
from feedback.operations import operations_snapshot


class Command(BaseCommand):
    help = 'Aggregate worker/queue indicators for a monitoring agent; contains no ticket text.'

    def add_arguments(self, parser):
        parser.add_argument('--fail-on-alert', action='store_true')

    def handle(self, *args, **options):
        result = operations_snapshot()
        self.stdout.write(json.dumps(result, sort_keys=True))
        if options['fail_on_alert'] and any(result['alerts'].values()):
            raise CommandError('Operations alert; inspect scoped delivery queue and worker.')
