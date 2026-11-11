import time
from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections
from django.utils import timezone
from feedback.models import WorkerHeartbeat
from feedback.services import process_outbox


class Command(BaseCommand):
    help = "Доставляет уведомления и проверяет истёкшие уточнения."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Один проход (по умолчанию)")
        parser.add_argument("--loop", action="store_true", help="Непрерывный обработчик")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--interval", type=float, default=5)

    def handle(self, *args, **options):
        try:
            while True:
                try:
                    counts = process_outbox(limit=max(1,min(options["limit"],1000)))
                    WorkerHeartbeat.objects.update_or_create(key="default",defaults={"last_seen":timezone.now(),"last_error":"","last_counts":counts})
                except DatabaseError as exc:
                    # No exception text, recipient names, or payloads enter diagnostics.
                    try:
                        close_old_connections()
                        WorkerHeartbeat.objects.filter(key="default").update(last_error=type(exc).__name__)
                    except DatabaseError:
                        pass
                    if not options["loop"] or options["once"]:
                        raise
                    self.stderr.write("База данных временно недоступна. Повтор после восстановления соединения.")
                    close_old_connections()
                    time.sleep(max(1,options["interval"]))
                    continue
                self.stdout.write(str(counts))
                if not options["loop"] or options["once"]:
                    break
                time.sleep(max(0.2,options["interval"]))
        except KeyboardInterrupt:
            self.stdout.write("Обработчик остановлен.")
