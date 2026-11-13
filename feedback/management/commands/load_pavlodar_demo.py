"""Load 20 synthetic requests of a Pavlodar branch through the real domain commands.

Unlike load_demo (a fixed, fingerprinted import), every card here is created with
create_ticket and moved with execute_command, so routing, deadlines, history and
notifications are exactly what a real employee and processor would produce.
Repeated runs skip cards that already exist and never touch passwords.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from feedback.models import Category, OrgUnit, Queue, RoutingRule, Ticket, User
from feedback.services import DomainError, create_ticket, execute_command

TYPES = {"Проблема": "PROBLEM", "Предложение": "SUGGESTION", "Положительный отзыв": "PRAISE"}
PRIORITIES = {"Низкий": "LOW", "Средний": "MEDIUM", "Высокий": "HIGH"}
STATUSES = {"Новое", "На рассмотрении", "Ожидает уточнения", "В работе", "Реализовано", "Отклонено"}
DEFAULT_PATH = Path(settings.BASE_DIR) / "data" / "demo_requests_pavlodar.json"


class Command(BaseCommand):
    help = "Загрузить 20 синтетических обращений филиала в Павлодаре. Повтор не создаёт дубликатов."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", type=Path, default=DEFAULT_PATH, help="Путь к JSON набора.")
        parser.add_argument("--password", help="Пароль только для вновь создаваемых авторов.")

    def handle(self, *args, **options):
        if not (settings.DEBUG or getattr(settings, "DEMO_MODE", False)):
            raise CommandError("Демо-данные загружаются только при DEBUG=1 или DEMO_MODE=1.")
        password = options.get("password") or os.getenv("DEMO_PASSWORD") or ("ModusDemo2026!" if settings.DEBUG else "")
        if len(password) < 12:
            raise CommandError("Задайте DEMO_PASSWORD или --password не короче 12 символов.")
        try:
            payload = json.loads(Path(options["dataset"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(f"Не удалось прочитать набор: {exc}") from exc
        self.validate(payload)
        if not Queue.objects.filter(code="default").exists():
            raise CommandError("Сначала загрузите основной демо-набор: python manage.py load_demo")

        unit, _ = OrgUnit.objects.get_or_create(code=payload["unit"]["code"], defaults={"name": payload["unit"]["name"]})
        authors = {}
        for username, info in payload["authors"].items():
            user, created = User.objects.get_or_create(username=username, defaults={
                "first_name": info["first_name"], "last_name": info["last_name"], "role": "author", "org_unit": unit})
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
            authors[username] = user
        for name, spec in payload["new_categories"].items():
            category, _ = Category.objects.get_or_create(name=name, defaults={"code": spec["code"], "active": True})
            queue = Queue.objects.get(code=spec["queue"])
            if not RoutingRule.objects.filter(category=category, active=True).exists():
                RoutingRule.objects.create(category=category, request_type="", queue=queue, priority=100, active=True, version=1)

        created = skipped = 0
        for row in payload["records"]:
            if Ticket.objects.filter(source_record_id=row["source_id"]).exists():
                skipped += 1
                continue
            try:
                with transaction.atomic():
                    self.load_one(row, authors[row["author"]], payload["dataset_id"])
            except (DomainError, Category.DoesNotExist) as exc:
                raise CommandError(f"{row['source_id']}: {exc}") from exc
            created += 1
        self.stdout.write(self.style.SUCCESS(
            f"Павлодар: создано {created}, уже было {skipped}. Авторы: {', '.join(authors)} (пароль — DEMO_PASSWORD)."))

    @staticmethod
    def validate(payload):
        rows = payload.get("records")
        if not isinstance(rows, list) or len(rows) != 20 or len({r.get("source_id") for r in rows}) != 20:
            raise CommandError("Ожидается ровно 20 обращений с уникальными source_id.")
        for row in rows:
            sid = row.get("source_id", "?")
            if row.get("type") not in TYPES or row.get("priority") not in PRIORITIES or row.get("status") not in STATUSES:
                raise CommandError(f"{sid}: неизвестный тип, приоритет или статус.")
            if row.get("author") not in payload.get("authors", {}):
                raise CommandError(f"{sid}: автор не описан в разделе authors.")
            if not str(row.get("description", "")).strip():
                raise CommandError(f"{sid}: пустое описание.")
            needs = {"Ожидает уточнения": ["question"], "В работе": ["decision", "criteria"],
                     "Реализовано": ["decision", "criteria", "result", "evidence"], "Отклонено": ["decline_reason"]}
            missing = [field for field in needs.get(row["status"], []) if not row.get(field)]
            if missing:
                raise CommandError(f"{sid}: для статуса «{row['status']}» нужны поля {', '.join(missing)}.")

    def load_one(self, row, author, dataset_id):
        ticket = create_ticket(actor=author, idempotency_key=f"pavlodar-{row['source_id']}", data={
            "category": Category.objects.get(name=row["category"], active=True),
            "request_type": TYPES[row["type"]], "description": row["description"],
            "proposed_solution": row.get("proposed_solution", ""), "reported_priority": PRIORITIES[row["priority"]],
            "confidentiality": "STANDARD",
        })
        Ticket.objects.filter(pk=ticket.pk).update(is_demo=True, source_record_id=row["source_id"],
            source_data={"dataset_id": dataset_id, "synthetic": True, "branch": "Павлодар"})
        handler = ticket.assignee or ticket.queue.owner

        def run(command, actor=handler, **data):
            ticket.refresh_from_db()
            execute_command(actor=actor, ticket_id=ticket.pk, command=command, expected_version=ticket.version, data=data)

        status = row["status"]
        if status == "Новое":
            return
        run("start")
        if row.get("comment"):
            run("comment", actor=author, message=row["comment"], visibility="public")
        if status == "Ожидает уточнения":
            run("ask_info", question=row["question"])
        elif status == "Отклонено":
            run("decline", reason=row["decline_reason"])
        elif status in {"В работе", "Реализовано"}:
            run("approve", reason=row["decision"], criteria=row["criteria"],
                plan_due_at=(timezone.localtime() + timedelta(days=21)).replace(second=0, microsecond=0).isoformat())
            if status == "Реализовано":
                run("implement", result=row["result"], evidence=row["evidence"])
