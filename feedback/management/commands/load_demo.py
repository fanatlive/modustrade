"""Load the explicitly synthetic hackathon dataset without overwriting runtime work."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from feedback.models import (
    Category, Decision, Event, ImportBatch, ImportRecord, InfoRequest,
    OrgUnit, ProcessingCycle, Queue, RoutingRule, Ticket, User,
)
from feedback.sla import add_business_hours, policy_snapshot, subtract_business_hours
from feedback.services import DomainError, _route


TYPE_MAP = {"Проблема": "PROBLEM", "Предложение": "SUGGESTION", "Положительный отзыв": "PRAISE"}
PRIORITY_MAP = {"Низкий": "LOW", "Средний": "MEDIUM", "Высокий": "HIGH"}
STATUS_MAP = {
    "Новое": "NEW", "На рассмотрении": "REVIEW", "В работе": "IN_PROGRESS",
    "Ожидает уточнения": "WAITING_INFO", "Реализовано": "CLOSED",
    "Отклонено": "CLOSED", "Закрыто": "REVIEW",
}
AUTHOR_MAP = {
    "Сотрудник закупок": ("author", "Закупки", "procurement"),
    "Сотрудник бухгалтерии": ("author2", "Учёт и документы", "backoffice"),
    "Менеджер по продажам": ("author_sales", "Продажи", "sales"),
    "Координатор проекта": ("author_project", "Проекты", "projects"),
    "Руководитель проекта": ("author_lead", "Проекты", "projects"),
    "Сотрудник офиса": ("author_office", "Офис", "office"),
    "Сотрудник проектирования": ("author_design", "Проектирование", "design"),
}
AUTHOR_DISPLAY_NAMES = {
    "author": ("Данил", "Колбасенко"),
}
QUEUE_MAP = {
    "Межотдельное взаимодействие": "procurement", "Информационная прозрачность": "projects",
    "Каталог и данные": "it", "Обучение": "hr", "Документооборот": "processes",
    "Регламенты": "processes", "Интеграции и IT": "it", "Знания и наставничество": "hr",
    "Договорная работа": "processes", "Задачи и коммуникации": "projects",
    "Оперативность": "projects", "Коммуникации": "it", "Проектная деятельность": "projects",
    "Планирование ресурсов": "projects", "Постановка задач": "projects",
    "Корпоративная среда": "admin", "Хранение документов": "processes",
    "Прозрачность результатов": "hr",
}
# This catalog is an explicit migration contract, never derived from input order.
# Legacy rows keep their PK and code, so references and history do not change.
CATEGORY_CATALOG = {
    "Межотдельное взаимодействие": ("demo-01", "interdepartmental"),
    "Информационная прозрачность": ("demo-02", "information-transparency"),
    "Каталог и данные": ("demo-03", "catalog-data"),
    "Обучение": ("demo-04", "training"),
    "Документооборот": ("demo-05", "document-workflow"),
    "Регламенты": ("demo-06", "regulations"),
    "Интеграции и IT": ("demo-07", "integrations-it"),
    "Знания и наставничество": ("demo-08", "knowledge-mentoring"),
    "Договорная работа": ("demo-09", "contracts"),
    "Задачи и коммуникации": ("demo-10", "tasks-communications"),
    "Оперативность": ("demo-11", "responsiveness"),
    "Коммуникации": ("demo-12", "communications"),
    "Проектная деятельность": ("demo-13", "project-activity"),
    "Планирование ресурсов": ("demo-14", "resource-planning"),
    "Постановка задач": ("demo-15", "task-definition"),
    "Корпоративная среда": ("demo-16", "corporate-environment"),
    "Хранение документов": ("demo-17", "document-storage"),
    "Прозрачность результатов": ("demo-18", "results-transparency"),
    "Не знаю": (None, "unknown"),
}


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def category_codes(name: str) -> tuple[str | None, str]:
    for label, codes in CATEGORY_CATALOG.items():
        if normalized_name(label) == normalized_name(name):
            return codes
    digest = hashlib.sha256(normalized_name(name).encode("utf-8")).hexdigest()[:32]
    return None, f"source-{digest}"


class ImportConflict(CommandError):
    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)
QUALITY_FLAGS = {
    "A-004": ["Нужна проверка: планирование семинара не подтверждает выполнение согласованного критерия. Исходный статус «Реализовано» сохранён; рабочее состояние — рассмотрение."],
    "A-018": ["Нужна проверка: исходное «Закрыто» не сопровождается подтверждением передачи отзыва. Рабочее состояние — рассмотрение."],
}


def normalize_record(row: dict, canonical_hashes: dict[str, str]) -> tuple[bool, str, list[str]]:
    """Only exact source rows may receive the fixture's synthetic enrichment."""
    fingerprint = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    canonical = canonical_hashes.get(row["source_id"]) == fingerprint
    flags = list(QUALITY_FLAGS.get(row["source_id"], [])) if canonical else [
        "SOURCE_RECORD_CHANGED: строка отличается от исходного набора; сценарные решения, вопросы и доказательства не перенесены. Требуется проверка."
    ]
    status = STATUS_MAP[row["status"]]
    # This rule is semantic, independent of A-numbers and fixture branches.
    # An unverified closed praise is kept for review rather than inventing an
    # acknowledgement, rejection or implementation evidence.
    if TYPE_MAP[row["type"]] == "PRAISE" and status in {"IN_PROGRESS", "CLOSED"}:
        flags.append("SOURCE_SEMANTIC_CONFLICT: положительный отзыв нельзя одобрить к реализации, реализовать или отклонить; требуется проверка обработки отзыва.")
    if flags:
        status = "REVIEW"
    return canonical, status, flags


class Command(BaseCommand):
    help = "Загрузить 20 синтетических обращений. Повтор не меняет существующие карточки и пароли."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Пароль только для вновь создаваемых демо-аккаунтов.")
        parser.add_argument("--dataset", type=Path, help="Путь к JSON версии 1.")
        parser.add_argument("--bootstrap-routes", action="store_true", help="Явно создать недостающие демо-очереди и маршруты; действующие настройки не меняются.")
        parser.add_argument("--dry-run", action="store_true", help="Проверить импорт с полным откатом изменений БД.")
        parser.add_argument("--json", action="store_true", dest="json_report", help="Вывести машиночитаемый JSON-отчёт.")
        parser.add_argument("--report", type=Path, help="Сохранить JSON-отчёт в указанный файл.")

    def handle(self, *args, **options):
        debug = bool(settings.DEBUG)
        demo_enabled = bool(getattr(settings, "DEMO_MODE", False))
        if not debug and not demo_enabled:
            raise CommandError("Демо-импорт при DEBUG=False требует явного DEMO_MODE=1.")
        password = options.get("password") or os.getenv("DEMO_PASSWORD")
        if not password:
            if not debug:
                raise CommandError("При DEBUG=False задайте DEMO_PASSWORD или --password.")
            password = "ModusDemo2026!"
        if len(password) < 12:
            raise CommandError("Демо-пароль должен содержать не менее 12 символов.")

        result = {"dry_run": bool(options.get("dry_run")), "applied": False,
            "created": 0, "existing": 0, "accepted": [], "conflicts": [], "errors": []}
        path = Path(options.get("dataset") or Path(settings.BASE_DIR) / "data" / "demo_requests.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.validate_payload(payload)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append({"code": "INVALID_DATASET", "message": str(exc)})
            self.write_report(result, options)
            raise CommandError(f"Некорректный набор данных: {exc}") from exc

        try:
            with transaction.atomic():
                # Serialize competing initial imports in the target PostgreSQL database.
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [631672026])
                result.update(self.load(payload, password, bootstrap_routes=bool(options.get("bootstrap_routes"))))
                if result["dry_run"]:
                    transaction.set_rollback(True)
            result["applied"] = not result["dry_run"]
        except (ImportConflict, DomainError) as exc:
            result["errors"].append({"code": exc.code, "message": str(exc)})
            self.write_report(result, options)
            raise CommandError(str(exc)) from exc
        except DatabaseError as exc:
            result["errors"].append({"code": "DATABASE_ERROR", "message": "Транзакция импорта отменена из-за ошибки БД; проверьте миграции и доступность."})
            self.write_report(result, options)
            raise CommandError(result["errors"][0]["message"]) from exc
        self.write_report(result, options)
        if not options.get("json_report"):
            prefix = "Проверка с откатом" if result["dry_run"] else "Демо"
            self.stdout.write(self.style.SUCCESS(
                f"{prefix}: создано {result['created']}, сохранено существующих {result['existing']}; "
                f"конфликтов {len(result['conflicts'])}, исходных карточек {result['source_count']}. "
                "Пароли существующих пользователей не изменены."
            ))

    def write_report(self, result: dict, options: dict) -> None:
        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
        if options.get("report"):
            try:
                Path(options["report"]).write_text(rendered + "\n", encoding="utf-8")
            except OSError as exc:
                raise CommandError(f"Не удалось записать отчёт: {exc}") from exc
        if options.get("json_report"):
            self.stdout.write(rendered)

    @staticmethod
    def validate_payload(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("source"), dict):
            raise ValueError("ожидается объект с описанием source")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("поддерживается schema_version=1")
        rows = payload["records"]
        if not isinstance(rows, list) or len(rows) != 20:
            raise ValueError("ожидаются ровно 20 карточек")
        if payload.get("record_count") != 20 or payload["source"].get("synthetic") is not True:
            raise ValueError("набор должен явно содержать 20 синтетических карточек")
        if any(not isinstance(row, dict) for row in rows) or {row["source_id"] for row in rows} != {f"A-{n:03}" for n in range(1, 21)}:
            raise ValueError("ожидаются уникальные A-001…A-020")
        source_hash = payload["source"]["sha256"]
        if not isinstance(source_hash, str) or len(source_hash) != 64 or any(c not in "0123456789abcdef" for c in source_hash):
            raise ValueError("некорректная контрольная сумма источника")
        if not isinstance(payload.get("dataset_id"), str) or not payload["dataset_id"].strip():
            raise ValueError("нужен непустой dataset_id")
        if not isinstance(payload["source"].get("name"), str) or not 1 <= len(payload["source"]["name"]) <= 255:
            raise ValueError("source.name должен содержать от 1 до 255 символов")
        required = ("category", "author_role", "description", "assignee_role", "suggested_action")
        for row in rows:
            if row["type"] not in TYPE_MAP or row["priority"] not in PRIORITY_MAP or row["status"] not in STATUS_MAP:
                raise ValueError(f"неизвестный справочник в {row['source_id']}")
            if any(not isinstance(row[field], str) or not row[field].strip() for field in required):
                raise ValueError(f"пустое поле в {row['source_id']}")
            if row["author_role"] not in AUTHOR_MAP:
                raise ValueError(f"нет явного отображения роли в {row['source_id']}")
            if len(row["category"]) > 160 or len(row["description"]) > 10000:
                raise ValueError(f"превышен размер категории/описания в {row['source_id']}")

    def load(self, payload, password, *, bootstrap_routes=False):
        # A separate checked-in fingerprint catalog is not regenerated from an
        # arbitrary --dataset file or from a changed source row during import.
        try:
            catalog = json.loads((Path(settings.BASE_DIR) / "data/demo_record_fingerprints.json").read_text(encoding="utf-8"))
            canonical_hashes = catalog["records"]
            if not isinstance(canonical_hashes, dict) or len(canonical_hashes) != 20:
                raise ValueError("invalid record catalog")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ImportConflict("Недоступен каталог отпечатков исходного набора; импорт отменён без сценарных догадок.", "CANONICAL_CATALOG_INVALID") from exc
        dataset_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous_batch = ImportBatch.objects.select_for_update().filter(checksum=payload["source"]["sha256"]).first()
        if previous_batch and previous_batch.summary.get("dataset_sha256", dataset_hash) != dataset_hash:
            raise ImportConflict("Содержимое JSON изменено при прежней контрольной сумме источника. Создайте отдельную проверенную версию набора.", "SOURCE_CHECKSUM_REUSED")
        if previous_batch:
            if previous_batch.source_name != payload["source"]["name"] or previous_batch.summary.get("dataset_id", payload["dataset_id"]) != payload["dataset_id"]:
                raise ImportConflict("Метаданные ранее принятого источника изменены; партия сохранена без изменений.", "SOURCE_METADATA_CONFLICT")
            staged = {item.source_id: item.raw for item in previous_batch.records.all()}
            if staged and staged != {row["source_id"]: row for row in payload["records"]}:
                raise ImportConflict("Снимок staging не совпадает с источником; партия заблокирована без исправления истории.", "STAGING_CONFLICT")
        # The system category is installed by migrations in every environment;
        # its presence alone is not an existing configured installation.
        empty_installation = not Category.objects.exclude(code="unknown").exists() and not any(
            model.objects.exists() for model in (User, OrgUnit, Queue, RoutingRule, Ticket, ImportBatch)
        )
        bootstrap_routes = bootstrap_routes or empty_installation
        if not bootstrap_routes and not Queue.objects.filter(code="default", restricted=False).exists():
            raise ImportConflict("Резервная очередь отсутствует. Настройте её либо явно используйте --bootstrap-routes в демо-окружении.", "ROUTING_NOT_CONFIGURED")
        categories = {name: self.resolve_category(name) for name in sorted({row["category"] for row in payload["records"]} | {"Не знаю"})}
        users = {}
        for source_role, (username, unit_name, unit_code) in AUTHOR_MAP.items():
            unit, _ = OrgUnit.objects.get_or_create(code=unit_code, defaults={"name": unit_name})
            first_name, last_name = AUTHOR_DISPLAY_NAMES.get(username, ("Демо", source_role))
            user, created = User.objects.get_or_create(username=username, defaults={
                "first_name": first_name, "last_name": last_name,
                "role": "author", "org_unit": unit,
            })
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
            users[username] = user
        for username, name, role, restricted in (
            ("processor", "Исполнитель", "processor", False),
            ("coordinator", "Координатор", "coordinator", True),
            ("manager", "Руководитель", "manager", False),
            ("admin", "Администратор", "admin", False),
        ):
            user, created = User.objects.get_or_create(username=username, defaults={
                "first_name": "Демо", "last_name": name, "role": role,
                "restricted_access": restricted,
            })
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
            users[username] = user

        queue_catalog = (
            ("default", "Первичный разбор", False),
            ("restricted", "Конфиденциальный разбор", True),
            ("procurement", "Закупки", False), ("projects", "Проекты", False),
            ("it", "IT и данные", False), ("processes", "Процессы и документы", False),
            ("hr", "Обучение и коммуникации", False), ("admin", "Офисная среда", False),
        )
        if bootstrap_routes:
            for code, name, restricted in queue_catalog:
                queue, created = Queue.objects.get_or_create(code=code, defaults={
                    "name": name, "restricted": restricted, "owner": users["coordinator"],
                })
                if queue.restricted != restricted:
                    raise ImportConflict(f"Очередь {code} имеет другую конфиденциальность; настройки сохранены.", "QUEUE_CLASS_CONFLICT")
                if created:
                    queue.members.add(users["coordinator"])
                    if not restricted:
                        queue.members.add(users["processor"], users["manager"])
            queues = {queue.code: queue for queue in Queue.objects.all()}
            for name, category in categories.items():
                if name == "Корпоративная среда":
                    for kind, code in (("SUGGESTION", "admin"), ("PRAISE", "hr")):
                        self.ensure_route(category, kind, queues[code], 100)
                elif name in QUEUE_MAP:
                    self.ensure_route(category, "", queues[QUEUE_MAP[name]], 100)
            self.ensure_route(None, "", queues["default"], 0)

        batch, _ = ImportBatch.objects.get_or_create(checksum=payload["source"]["sha256"], defaults={
            "source_name": payload["source"]["name"],
            "summary": {"dataset_id": payload["dataset_id"], "schema_version": 1, "is_demo": True},
        })
        created_count = existing_count = 0
        accepted, conflicts, review_required = [], [], []
        for row in payload["records"]:
            source_id = row["source_id"]
            canonical, status, quality_flags = normalize_record(row, canonical_hashes)
            if quality_flags:
                review_required.append(source_id)
            raw, _ = ImportRecord.objects.get_or_create(batch=batch, source_id=source_id, defaults={
                "raw": row, "issues": quality_flags,
            })
            existing = Ticket.objects.filter(source_record_id=source_id).first()
            if existing:
                source_changed = existing.source_data.get("raw") != row
                if source_changed:
                    conflicts.append({"source_id": source_id, "code": "SOURCE_VERSION_CONFLICT", "ticket": existing.number})
                # Even repeated loads must never reset changed status, comments, dates, or passwords.
                if raw.ticket_id is None:
                    raw.ticket = existing
                    if source_changed:
                        raw.issues = list(raw.issues) + ["SOURCE_VERSION_CONFLICT: рабочая карточка сохранена без перезаписи."]
                    raw.save(update_fields=["ticket", "issues"])
                existing_count += 1
                continue

            username, _, _ = AUTHOR_MAP[row["author_role"]]
            queue, assignee, routing = _route(categories[row["category"]], TYPE_MAP[row["type"]], None, False)
            synthetic = []
            if status == "IN_PROGRESS":
                synthetic.append("approval_and_implementation_plan")
            if status == "WAITING_INFO":
                synthetic.append("pending_question_and_deadline")
            if status == "CLOSED":
                synthetic.append("decision_and_completion_at_import")
            source_data = {
                "dataset_id": payload["dataset_id"], "normalization_version": 2,
                "source_sha256": payload["source"]["sha256"], "raw": row,
                "source_status": row["status"], "source_author_role": row["author_role"],
                "source_assignee_role": row["assignee_role"], "source_action_example": row["suggested_action"],
                "account_mapping": {"username": username, "synthetic": True},
                "synthetic_additions": synthetic, "synthetic_timeline": bool(synthetic),
                "source_submitted_at": None, "metrics_eligible": False,
                "canonical_source_record": canonical,
                "notice": "Снимок синтетического набора. Время импорта не является временем подачи.",
                "routing_at_import": routing,
            }
            now = timezone.now()
            ticket = Ticket.objects.create(
                number=f"MT-{source_id}", author=users[username], category=categories[row["category"]],
                author_unit_name=users[username].org_unit.name if users[username].org_unit else "",
                queue=queue, assignee=assignee if status != "NEW" else None,
                request_type=TYPE_MAP[row["type"]], description=row["description"],
                reported_priority=PRIORITY_MAP[row["priority"]], effective_priority=PRIORITY_MAP[row["priority"]],
                confidentiality="STANDARD", status="REVIEW" if status == "CLOSED" else status, submitted_at=None,
                is_demo=True, source_record_id=source_id, source_data=source_data,
                quality_flags=quality_flags,
            )
            cycle = ProcessingCycle.objects.create(ticket=ticket, number=1, started_at=None, policy={**policy_snapshot(),
                "source": "unknown_submission_time", "synthetic": True,
                "business_timezone": getattr(settings, "BUSINESS_TIMEZONE", "Asia/Bishkek"),
                "metrics_eligible": False,
            })
            if status == "IN_PROGRESS":
                ticket.decision = "APPROVED"
                ticket.criteria = "Демонстрационный критерий: ответственный описал выполненное улучшение и приложил проверяемое подтверждение."
                ticket.plan_due_at = add_business_hours(now, 40)
                Decision.objects.create(ticket=ticket, actor=users["manager"], code="APPROVED",
                    reason="Синтетическое одобрение для демонстрации этапа выполнения; не историческое решение компании.",
                    criteria=ticket.criteria, cycle_no=1)
            elif status == "WAITING_INFO":
                question = (
                    "Демонстрационный вопрос: какие виды документов и этапы согласования нужно уточнить?"
                    if source_id == "A-005" else
                    "Демонстрационный вопрос: в каком формате хранится спецификация и как определяется её версия?"
                )
                due = add_business_hours(now, 40)
                InfoRequest.objects.create(ticket=ticket, actor=users["processor"], question=question,
                    due_at=due, pause_until=due, resume_status="REVIEW", created_at=now,
                    reminder_due_at=max(now, subtract_business_hours(due, 8, policy=cycle.policy)))
            elif status == "CLOSED":
                ticket.status = "CLOSED"
                ticket.closed_at = now
                cycle.closed_at = now
                if source_id == "A-008" and canonical:
                    ticket.decision = "APPROVED"
                    ticket.criteria = "В демонстрационном репозитории существует учебная памятка для передачи знаний."
                    ticket.resolution_code = "IMPLEMENTED"
                    ticket.resolution_summary = "Синтетический результат: подготовлен тестовый артефакт data/demo_instruction.md. Он создан для проверки сценария закрытия и не является документом компании."
                else:
                    ticket.decision = "DECLINED"
                    ticket.resolution_code = "DECLINED"
                    ticket.resolution_summary = "Демонстрационный мотивированный отказ: свободного помещения в условном сценарии нет; вопрос можно пересмотреть при перепланировке. Это не фактическое решение компании."
                Decision.objects.create(ticket=ticket, actor=users["manager"], code=ticket.decision,
                    reason=ticket.resolution_summary, criteria=ticket.criteria, cycle_no=1)
                cycle.resolution_code = ticket.resolution_code
                cycle.resolution_summary = ticket.resolution_summary
                cycle.save(update_fields=["closed_at", "resolution_code", "resolution_summary"])
            ticket.save()
            Event.objects.create(ticket=ticket, actor=users["coordinator"], kind="IMPORTED_SNAPSHOT",
                summary="Импортирован синтетический снимок. Исходные даты неизвестны; дополнения отмечены отдельно.",
                visibility="public", sequence=1, origin="import", payload={
                    "batch_id": batch.pk, "source_id": source_id, "synthetic_additions": synthetic,
                    "source_status": row["status"], "quality_flags": quality_flags,
                })
            raw.ticket = ticket
            raw.save(update_fields=["ticket"])
            created_count += 1
            accepted.append(source_id)
        result = {"created": created_count, "existing": existing_count, "source_count": 20,
            "accepted": accepted, "conflicts": conflicts, "errors": [], "bootstrap_routes": bootstrap_routes,
            "review_required": review_required, "unknown_submission_dates": 20,
            "normalization_version": 2, "dataset_sha256": dataset_hash}
        # A batch is provenance, not a mutable last-run counter. Replays only report.
        if previous_batch is None:
            batch.summary = {**batch.summary, **result}
            batch.save(update_fields=["summary"])
        return result

    @staticmethod
    def resolve_category(name: str) -> Category:
        legacy_code, stable_code = category_codes(name)
        candidates = list(Category.objects.filter(code__in=[code for code in (legacy_code, stable_code) if code]))
        named = [category for category in Category.objects.all() if normalized_name(category.name) == normalized_name(name)]
        matches = {category.pk: category for category in candidates + named}
        if any(normalized_name(category.name) != normalized_name(name) for category in candidates) or len(matches) > 1:
            raise ImportConflict(f"Неоднозначное соответствие категории «{name}»; справочник сохранён без изменений.", "CATEGORY_CONFLICT")
        if matches:
            category = next(iter(matches.values()))
            if stable_code == "unknown" and (category.code != "unknown" or not category.active):
                raise ImportConflict("Системной категории «Не знаю» нужен активный code=unknown; измените справочник осознанно.", "UNKNOWN_CATEGORY_CONFLICT")
            return category
        return Category.objects.create(code=stable_code, name=name)

    @staticmethod
    def ensure_route(category, request_type, queue, priority):
        if not RoutingRule.objects.filter(category=category, request_type=request_type, affected_unit=None, queue__restricted=queue.restricted).exists():
            rule = RoutingRule(category=category, request_type=request_type, affected_unit=None,
                priority=priority, queue=queue, active=True, version=1)
            from django.core.exceptions import ValidationError
            try:
                rule.full_clean()
            except ValidationError as exc:
                raise ImportConflict("Демо-маршрут пересекается с рабочим правилом; настройки сохранены без изменений.", "ROUTING_CONFLICT") from exc
            rule.save()
