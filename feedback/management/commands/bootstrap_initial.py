"""Initialize fallback queues with an explicitly designated real coordinator."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from feedback.models import Category, Queue, User
from feedback.permissions import has_role
from feedback.services import _queue_viable


class Command(BaseCommand):
    help = "Создать резервные очереди без демо-данных и паролей; существующие настройки не перезаписываются."

    def add_arguments(self, parser):
        parser.add_argument("--coordinator", required=True, help="Логин действующего координатора с конфиденциальным допуском.")
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            owner = User.objects.select_for_update().get(username=options["coordinator"], is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("Действующий координатор не найден. Сначала создайте личную учётную запись.") from exc
        if not has_role(owner, "coordinator") or not owner.restricted_access:
            raise CommandError("Назначенному пользователю нужны роль coordinator и restricted_access=True.")
        category, _ = Category.objects.get_or_create(code="unknown", defaults={"name": "Не знаю", "active": True})
        if not category.active:
            raise CommandError("Системная категория unknown отключена. Проверьте справочник вручную.")
        result = {"dry_run": bool(options["dry_run"]), "queues": [], "demo_records": 0}
        for code, name, restricted in (
            ("default", "Первичный разбор", False),
            ("restricted", "Конфиденциальный разбор", True),
        ):
            queue, created = Queue.objects.get_or_create(code=code, defaults={
                "name": name, "restricted": restricted, "owner": owner,
            })
            if queue.restricted != restricted:
                raise CommandError(f"Очередь {code} имеет другую конфиденциальность; конфигурация сохранена без изменений.")
            if created:
                queue.members.add(owner)
            if not _queue_viable(queue, restricted):
                raise CommandError(f"Очередь {code} не имеет допустимого действующего обработчика; настройте состав вручную.")
            result["queues"].append({"code": code, "created": created, "ready": True})
        if options["dry_run"]:
            transaction.set_rollback(True)
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
