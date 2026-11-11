"""Import provenance, identity and rollback contracts from audit F04/F19."""
from copy import deepcopy
import hashlib
from io import StringIO
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.conf import settings
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from feedback.management.commands.load_demo import CATEGORY_CATALOG, category_codes
from feedback.models import (Category, Event, ImportBatch, ImportRecord, OrgUnit,
                             Queue, RoutingRule, Ticket, User)
from feedback.models import TicketRelation
from feedback.models import InfoRequest
from feedback.services import create_ticket
from feedback.sla import subtract_business_hours


@override_settings(DEMO_MODE=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ImportRemediationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.original = json.loads((Path(settings.BASE_DIR) / "data/demo_requests.json").read_text(encoding="utf-8"))

    def run_import(self, payload=None, **options):
        output = StringIO()
        with TemporaryDirectory() as temp:
            if payload is not None:
                dataset = Path(temp) / "source.json"
                dataset.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                options["dataset"] = dataset
            call_command("load_demo", password="ImportTest2026!", json_report=True, stdout=output, **options)
        return json.loads(output.getvalue())

    def next_version(self):
        payload = deepcopy(self.original)
        payload["source"]["sha256"] = hashlib.sha256(b"independent-source-v2").hexdigest()
        payload["dataset_id"] = "modus-test-v2"
        payload["records"].reverse()
        return payload

    def snapshot(self):
        return {
            model.__name__: list(model.objects.order_by("pk").values())
            for model in (Category, Event, ImportBatch, ImportRecord, OrgUnit, Queue,
                          Queue.members.through, RoutingRule, Ticket, User)
        }

    def test_fresh_and_reordered_installations_have_stable_codes(self):
        first = self.run_import(self.next_version())
        self.assertTrue(first["bootstrap_routes"])
        self.assertEqual(first["created"], 20)
        self.assertEqual(Category.objects.count(), 19)
        for name, (_, code) in CATEGORY_CATALOG.items():
            self.assertEqual(Category.objects.get(name=name).code, code)
        self.assertTrue(Category.objects.get(code="unknown").active)

    def test_legacy_categories_keep_primary_keys_and_links(self):
        legacy = {}
        for name, (code, _) in CATEGORY_CATALOG.items():
            if code:
                legacy[name] = Category.objects.create(name=name, code=code).pk
        # Existing configuration requires an intentional bootstrap, rather than guessing.
        result = self.run_import(bootstrap_routes=True)
        self.assertEqual(result["created"], 20)
        for name, pk in legacy.items():
            category = Category.objects.get(pk=pk)
            self.assertEqual(category.name, name)
            self.assertEqual(category.code, CATEGORY_CATALOG[name][0])
            self.assertTrue(Ticket.objects.filter(category=category).exists())
        before = self.snapshot()
        self.run_import(self.next_version())
        after = self.snapshot()
        for model in ("Category", "Ticket", "RoutingRule", "Queue", "User", "Event"):
            self.assertEqual(before[model], after[model])

    def test_new_batch_never_restores_removed_routes_or_members(self):
        self.run_import()
        RoutingRule.objects.filter(category__name="Корпоративная среда").delete()
        queue = Queue.objects.get(code="admin")
        queue.members.clear()
        queue.name = "Рабочее название, заданное администратором"
        queue.save(update_fields=["name"])
        category = Category.objects.get(name="Обучение")
        category.active = False
        category.save(update_fields=["active"])
        actor = User.objects.get(username="author")
        actor.set_password("PrivateChangedPassword2026!")
        actor.save(update_fields=["password"])
        ticket = Ticket.objects.get(source_record_id="A-001")
        ticket.description = "Рабочее изменение не заменяется снимком источника."
        ticket.save(update_fields=["description"])
        before = self.snapshot()
        report = self.run_import(self.next_version())
        after = self.snapshot()
        self.assertFalse(report["bootstrap_routes"])
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["existing"], 20)
        self.assertEqual(report["conflicts"], [])
        for model in ("Category", "Queue", "Queue_members", "RoutingRule", "Ticket", "User", "Event"):
            self.assertEqual(before[model], after[model])
        self.assertEqual(ImportBatch.objects.count(), 2)
        self.assertEqual(ImportRecord.objects.count(), 40)

    def test_dry_run_empty_database_has_no_persistent_rows(self):
        before = self.snapshot()
        report = self.run_import(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["applied"])
        self.assertEqual(len(report["accepted"]), 20)
        self.assertEqual(before, self.snapshot())

    def test_dry_run_conflicting_version_keeps_all_data_and_passwords(self):
        self.run_import()
        payload = self.next_version()
        next(row for row in payload["records"] if row["source_id"] == "A-004")["description"] = "Новая проверенная редакция исходного текста."
        before = self.snapshot()
        report = self.run_import(payload, dry_run=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([item["source_id"] for item in report["conflicts"]], ["A-004"])

    def test_new_version_is_staged_without_overwriting_runtime_or_origin(self):
        self.run_import()
        payload = self.next_version()
        row = next(row for row in payload["records"] if row["source_id"] == "A-018")
        row["suggested_action"] = "Исправленный текст проверяется человеком отдельно."
        old = Ticket.objects.get(source_record_id="A-018")
        original_source = deepcopy(old.source_data)
        report = self.run_import(payload)
        old.refresh_from_db()
        self.assertEqual(original_source, old.source_data)
        staged = ImportRecord.objects.get(batch__checksum=payload["source"]["sha256"], source_id="A-018")
        self.assertEqual(staged.raw, row)
        self.assertEqual(staged.ticket_id, old.pk)
        self.assertTrue(any("SOURCE_VERSION_CONFLICT" in issue for issue in staged.issues))
        self.assertEqual(report["conflicts"][0]["source_id"], "A-018")

    def test_same_checksum_modified_json_is_rejected_with_machine_report(self):
        self.run_import()
        payload = deepcopy(self.original)
        payload["records"][0]["description"] = "Недопустимая незаявленная подмена источника."
        before = self.snapshot()
        output = StringIO()
        with TemporaryDirectory() as temp:
            dataset = Path(temp) / "corrupt.json"
            dataset.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("load_demo", dataset=dataset, password="ImportTest2026!", json_report=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["errors"][0]["code"], "SOURCE_CHECKSUM_REUSED")
        self.assertFalse(report["applied"])
        self.assertEqual(before, self.snapshot())

    def test_invalid_structure_and_staging_corruption_are_rejected(self):
        output = StringIO()
        with TemporaryDirectory() as temp:
            dataset = Path(temp) / "invalid.json"
            dataset.write_text("[]", encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("load_demo", dataset=dataset, password="ImportTest2026!", json_report=True, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["errors"][0]["code"], "INVALID_DATASET")
        self.assertFalse(Ticket.objects.exists())
        self.run_import()
        staged = ImportRecord.objects.first()
        staged.raw = {"tampered": True}
        staged.save(update_fields=["raw"])
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(before, self.snapshot())

    def test_legacy_collision_is_reported_without_relabeling(self):
        initial_count = Category.objects.count()
        category = Category.objects.create(code="demo-01", name="Корпоративная среда")
        with self.assertRaises(CommandError):
            self.run_import(bootstrap_routes=True)
        category.refresh_from_db()
        self.assertEqual(category.name, "Корпоративная среда")
        self.assertEqual(Category.objects.count(), initial_count + 1)
        self.assertFalse(Ticket.objects.exists())

    def test_preexisting_account_requires_explicit_route_bootstrap(self):
        User.objects.create_user("existing-admin", password="PrivateInitialPassword2026!", role="admin")
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(before, self.snapshot())
        report = self.run_import(bootstrap_routes=True)
        self.assertEqual(report["created"], 20)

    def test_unknown_category_configuration_is_never_silently_reactivated(self):
        category, _ = Category.objects.get_or_create(code="unknown", defaults={"name": "Не знаю"})
        category.active = False
        category.save(update_fields=["active"])
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.run_import(bootstrap_routes=True)
        self.assertEqual(before, self.snapshot())

    def test_new_category_normalization_uses_same_identity_and_fallback(self):
        payload = self.next_version()
        payload["records"][0]["category"] = "  Новая   Категория "
        self.run_import(payload)
        row = payload["records"][0]
        ticket = Ticket.objects.get(source_record_id=row["source_id"])
        self.assertEqual(ticket.category.code, category_codes("новая категория")[1])
        self.assertEqual(ticket.queue.code, "default")
        self.assertFalse(RoutingRule.objects.filter(category=ticket.category).exists())

    def test_provenance_review_flags_and_unknown_fallback(self):
        self.run_import()
        for source_id in ("A-004", "A-018"):
            ticket = Ticket.objects.get(source_record_id=source_id)
            row = next(item for item in self.original["records"] if item["source_id"] == source_id)
            self.assertEqual(ticket.source_data["raw"], row)
            self.assertEqual(ticket.source_data["source_sha256"], self.original["source"]["sha256"])
            self.assertEqual(ticket.status, "REVIEW")
            self.assertTrue(ticket.quality_flags)
            self.assertIsNone(ticket.submitted_at)
            self.assertIsNone(ticket.current_cycle.started_at)
        unknown = Category.objects.get(code="unknown")
        ticket = create_ticket(actor=User.objects.get(username="author"), idempotency_key="unknown-category", data={
            "category": unknown, "request_type": "PROBLEM", "description": "Не удалось определить правильную категорию обращения."})
        self.assertEqual(ticket.queue.code, "default")
        self.assertFalse(RoutingRule.objects.filter(category=unknown).exists())
        self.assertEqual(category_codes("  Новая   Категория "), category_codes("новая категория"))

    def test_replay_does_not_rewrite_batch_summary(self):
        self.run_import()
        batch = ImportBatch.objects.get()
        summary = deepcopy(batch.summary)
        self.run_import()
        batch.refresh_from_db()
        self.assertEqual(summary, batch.summary)

    def test_fresh_import_schedules_reminders_using_saved_cycle_calendar(self):
        self.run_import()
        questions = list(InfoRequest.objects.select_related("ticket"))
        self.assertEqual(len(questions), 2)
        for question in questions:
            cycle = question.ticket.current_cycle
            self.assertIsNotNone(question.reminder_due_at)
            self.assertEqual(question.reminder_due_at, max(question.created_at,
                subtract_business_hours(question.due_at, 8, policy=cycle.policy)))
            self.assertIn("timezone", cycle.policy)

    def test_fresh_mixed_praise_statuses_cannot_import_implementation_or_decline(self):
        payload = self.next_version()
        targets = {"A-003", "A-008", "A-017"}
        for row in payload["records"]:
            if row["source_id"] in targets:
                row["type"] = "Положительный отзыв"
        report = self.run_import(payload)
        self.assertTrue(targets.issubset(set(report["review_required"])))
        for ticket in Ticket.objects.filter(source_record_id__in=targets):
            row = next(item for item in payload["records"] if item["source_id"] == ticket.source_record_id)
            self.assertEqual((ticket.request_type, ticket.status, ticket.decision, ticket.resolution_code),
                             ("PRAISE", "REVIEW", "", ""))
            self.assertFalse(ticket.decisions.exists())
            self.assertEqual(ticket.source_data["raw"], row)
            self.assertFalse(ticket.source_data["canonical_source_record"])
            staged = ImportRecord.objects.get(ticket=ticket)
            self.assertEqual(staged.raw, row)
            self.assertTrue(any("SOURCE_SEMANTIC_CONFLICT" in issue for issue in staged.issues))

    def test_changed_implementation_content_never_inherits_demo_artifact(self):
        source_path = Path(settings.BASE_DIR) / "data/demo_requests.json"
        source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        payload = self.next_version()
        row = next(item for item in payload["records"] if item["source_id"] == "A-008")
        row["description"] = "Новое предложение о другой системе, не связанное с учебной памяткой."
        row["suggested_action"] = "Нужно исследовать новый сценарий; выполненного результата нет."
        self.run_import(payload)
        ticket = Ticket.objects.get(source_record_id="A-008")
        self.assertEqual((ticket.status, ticket.decision, ticket.resolution_code, ticket.criteria), ("REVIEW", "", "", ""))
        self.assertEqual(ticket.source_data["synthetic_additions"], [])
        self.assertEqual(ticket.source_data["raw"], row)
        self.assertTrue(any("SOURCE_RECORD_CHANGED" in flag for flag in ticket.quality_flags))
        self.assertFalse(ticket.decisions.exists())
        self.assertNotIn("demo_instruction", ticket.resolution_summary)
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_digest)

    def test_canonical_fingerprints_and_staged_revision_preserve_existing_source(self):
        catalog = json.loads((Path(settings.BASE_DIR) / "data/demo_record_fingerprints.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["source_sha256"], self.original["source"]["sha256"])
        for row in self.original["records"]:
            digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            self.assertEqual(catalog["records"][row["source_id"]], digest)
        self.run_import()
        ticket = Ticket.objects.get(source_record_id="A-008")
        self.assertTrue(ticket.source_data["canonical_source_record"])
        self.assertEqual(ticket.resolution_code, "IMPLEMENTED")
        self.assertIn("data/demo_instruction.md", ticket.resolution_summary)
        before = self.snapshot()
        payload = self.next_version()
        row = next(item for item in payload["records"] if item["source_id"] == "A-008")
        row["suggested_action"] = "Новая редакция требует отдельного согласования."
        report = self.run_import(payload)
        for model in ("Ticket", "Event", "RoutingRule", "User"):
            self.assertEqual(before[model], self.snapshot()[model])
        staged = ImportRecord.objects.get(batch__checksum=payload["source"]["sha256"], source_id="A-008")
        self.assertEqual(staged.raw, row)
        self.assertTrue(any("SOURCE_RECORD_CHANGED" in issue for issue in staged.issues))
        self.assertTrue(any("SOURCE_VERSION_CONFLICT" in issue for issue in staged.issues))
        self.assertIn("A-008", report["review_required"])
        snapshot = self.snapshot()
        self.run_import(payload)
        self.assertEqual(snapshot, self.snapshot())


@override_settings(DEMO_MODE=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InitialConfigurationTests(TestCase):
    def setUp(self):
        self.coordinator = User.objects.create_user("real-coordinator", role="coordinator", restricted_access=True)

    def bootstrap(self, **kwargs):
        output = StringIO()
        call_command("bootstrap_initial", coordinator=self.coordinator.username, stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def test_without_demo_configuration_allows_standard_and_restricted_creation(self):
        report = self.bootstrap()
        self.assertEqual(report["demo_records"], 0)
        self.assertFalse(ImportBatch.objects.exists())
        self.assertFalse(Ticket.objects.exists())
        author = User.objects.create_user("real-author")
        unknown = Category.objects.get(code="unknown")
        for confidentiality, queue_code in (("STANDARD", "default"), ("RESTRICTED", "restricted")):
            ticket = create_ticket(actor=author, idempotency_key=confidentiality, data={
                "category": unknown, "request_type": "PROBLEM", "confidentiality": confidentiality,
                "description": "Проверка создания реального обращения без демонстрационных записей.",
            })
            self.assertEqual(ticket.queue.code, queue_code)
        self.assertFalse(Ticket.objects.filter(is_demo=True).exists())

    def test_replay_preserves_owner_members_and_name(self):
        self.bootstrap()
        queue = Queue.objects.get(code="default")
        queue.name = "Утверждённое название"
        queue.members.clear()
        queue.save(update_fields=["name"])
        report = self.bootstrap()
        queue.refresh_from_db()
        self.assertFalse(any(item["created"] for item in report["queues"]))
        self.assertEqual(queue.name, "Утверждённое название")
        self.assertEqual(queue.members.count(), 0)

    def test_dry_run_rolls_back_and_ineligible_coordinator_is_rejected(self):
        self.assertTrue(self.bootstrap(dry_run=True)["dry_run"])
        self.assertFalse(Queue.objects.exists())
        self.coordinator.restricted_access = False
        self.coordinator.save(update_fields=["restricted_access"])
        with self.assertRaises(CommandError):
            self.bootstrap()
        self.assertFalse(Queue.objects.exists())

    def test_upgrade_archives_stale_duplicate_links_without_erasing_history(self):
        self.bootstrap()
        fields = {"author": self.coordinator, "category": Category.objects.get(code="unknown"),
                  "queue": Queue.objects.get(code="default"), "request_type": "PROBLEM",
                  "description": "Карточка для проверки сохранности графа при обновлении."}
        canonical = Ticket.objects.create(**fields)
        reopened = Ticket.objects.create(**fields, status="REVIEW")
        closed = Ticket.objects.create(**fields, status="CLOSED", resolution_code="DUPLICATE",
                                       resolution_summary="Подтверждённый дубликат", closed_at=timezone.now())
        stale = TicketRelation.objects.create(ticket=reopened, canonical=canonical)
        active = TicketRelation.objects.create(ticket=closed, canonical=canonical)
        migrate = import_module("feedback.migrations.0004_initial_category_and_legacy_relations").initialize_system_data
        migrate(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        stale.refresh_from_db()
        active.refresh_from_db()
        self.assertFalse(stale.active)
        self.assertIsNotNone(stale.archived_at)
        self.assertTrue(active.active)
        self.assertIsNone(active.archived_at)
        self.assertEqual(TicketRelation.objects.count(), 2)
        archived_at = stale.archived_at
        migrate(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        stale.refresh_from_db()
        self.assertEqual(stale.archived_at, archived_at)
