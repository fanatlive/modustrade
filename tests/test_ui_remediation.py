"""Regression scenarios from the 2026-11-11 audit, through HTTP boundaries."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from feedback.forms import TicketFilterForm
from feedback.models import Category, Comment, Decision, Event, InfoRequest, Notification, Outbox, ProcessingCycle, Queue, Ticket, User
from feedback.permissions import has_role, operational_users, visible_notifications, visible_tickets
from feedback.services import create_ticket, execute_command


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AuditUIRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_demo", stdout=StringIO())
        cls.author = User.objects.get(username="author")
        cls.other = User.objects.get(username="author2")
        cls.processor = User.objects.get(username="processor")
        cls.coordinator = User.objects.get(username="coordinator")

    def command(self, ticket, command, actor=None, **data):
        execute_command(actor=actor or self.processor, ticket_id=ticket.pk, command=command,
                        expected_version=ticket.version, data=data)
        ticket.refresh_from_db()

    def active_ticket(self):
        ticket = Ticket.objects.get(source_record_id="A-001")
        self.command(ticket, "start")
        self.command(ticket, "approve", reason="Одобрено после проверки", criteria="Результат проверяем",
                     plan_due_at=timezone.now()+timedelta(days=10))
        return ticket

    def test_private_draft_remains_private_after_conflict_and_retry(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        stale = ticket.version
        self.command(ticket, "comment", message="Параллельное публичное изменение")
        self.client.force_login(self.processor)
        url = reverse("feedback:command", args=[ticket.pk])
        data = {"command": "comment", "version": stale, "message": "PRIVATE-DRAFT-409", "visibility": "internal"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '<option value="internal" selected', status_code=409)
        self.assertContains(response, "PRIVATE-DRAFT-409", status_code=409)
        data["version"] = response.context["ticket"].version
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(ticket.comments.get(message=data["message"]).visibility, "internal")
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(reverse("feedback:detail", args=[ticket.pk])), "PRIVATE-DRAFT-409")

    def test_private_draft_visibility_survives_form_validation_error(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        self.client.force_login(self.processor)
        response = self.client.post(reverse("feedback:command", args=[ticket.pk]),
            {"command": "comment", "version": 0, "message": "PRIVATE-DRAFT-422", "visibility": "internal"})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, '<option value="internal" selected', status_code=422)
        self.assertFalse(ticket.comments.filter(message="PRIVATE-DRAFT-422").exists())

    def test_authorship_overrides_additional_operational_role(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        self.command(ticket, "comment", message="INTERNAL-AUTHOR-EXCLUSION", visibility="internal")
        self.author.extra_roles = ["processor"]
        self.author.save(update_fields=["extra_roles"])
        ticket.queue.members.add(self.author)
        self.client.force_login(self.author)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertFalse(response.context["can_view_internal"])
        self.assertNotContains(response, "INTERNAL-AUTHOR-EXCLUSION")
        denied = self.client.post(reverse("feedback:command", args=[ticket.pk]), {
            "command": "comment", "version": ticket.version, "message": "PRIVATE-AUTHOR-DRAFT", "visibility": "internal"})
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(ticket.comments.filter(message="PRIVATE-AUTHOR-DRAFT").exists())

    def test_withdrawal_request_is_reviewable_and_executable_by_coordinator(self):
        ticket = self.active_ticket()
        self.command(ticket, "withdraw", actor=self.author, reason="AUTHOR-WITHDRAW-REASON")
        self.client.force_login(self.coordinator)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertContains(response, "AUTHOR-WITHDRAW-REASON")
        self.assertContains(response, "Согласовать отзыв и закрыть")
        response = self.client.post(reverse("feedback:command", args=[ticket.pk]), {
            "command": "withdraw", "version": ticket.version, "reason": "Остановку работ согласовали"})
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.resolution_code, "WITHDRAWN")

    def test_prior_cycle_result_and_evidence_visible_after_reopen(self):
        ticket = self.active_ticket()
        self.command(ticket, "implement", result="PREVIOUS-CYCLE-RESULT", evidence="PREVIOUS-CYCLE-EVIDENCE")
        self.command(ticket, "reopen", actor=self.author, reason="Нужна дополнительная проверка")
        self.client.force_login(self.author)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertContains(response, "PREVIOUS-CYCLE-RESULT")
        self.assertContains(response, "PREVIOUS-CYCLE-EVIDENCE")
        self.assertContains(response, "Цикл 1")
        self.assertContains(response, "Цикл 2")

    def test_coordinator_can_resume_information_wait_through_ui(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        self.command(ticket, "ask_info", question="Уточните проверяемый результат")
        self.client.force_login(self.coordinator)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertContains(response, 'value="resume_info"')
        self.assertContains(response, 'value="extend_info"')
        response = self.client.post(reverse("feedback:command", args=[ticket.pk]), {
            "command": "resume_info", "version": ticket.version, "reason": "Данных достаточно для рассмотрения"})
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "REVIEW")

    def test_internal_legacy_notification_hidden_after_access_revocation(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        event = Event.objects.create(ticket=ticket, kind="COMMENT", summary="Internal metadata", sequence=99, visibility="internal")
        job = Outbox.objects.create(ticket=ticket, event=event, recipient=self.author, message="LEGACY-INTERNAL-METADATA", state="sent")
        item = Notification.objects.create(ticket=ticket, outbox=job, user=self.author, message=job.message)
        self.client.force_login(self.author)
        self.assertFalse(visible_notifications(self.author).filter(pk=item.pk).exists())
        self.assertNotContains(self.client.get("/notifications/"), job.message)
        self.assertEqual(self.client.post(reverse("feedback:read_notification", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.get("/").context["unread_notifications"], 0)

    def test_failed_delivery_is_scoped_and_coordinator_retry_requires_reason(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        self.command(ticket, "comment", message="Для проверки повторной доставки")
        job = Outbox.objects.filter(ticket=ticket, event__kind="COMMENT").first()
        job.state, job.attempts = "failed", 8
        job.save(update_fields=["state", "attempts"])
        self.client.force_login(self.author)
        self.assertEqual(self.client.get("/deliveries/failed/").status_code, 403)
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get("/deliveries/failed/"), ticket.number)
        url = reverse("feedback:retry_delivery", args=[job.pk])
        self.assertEqual(self.client.post(url, {"reason": ""}).status_code, 422)
        self.assertEqual(self.client.post(url, {"reason": "Причина сбоя устранена"}).status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.state, "pending")

    def test_author_assignee_and_queue_filters_use_visible_objects(self):
        self.client.force_login(self.author)
        form = TicketFilterForm(user=self.author)
        self.assertEqual(set(form.fields["author"].queryset.values_list("pk", flat=True)), {self.author.pk})
        self.assertFalse(form.fields["author"].queryset.filter(pk=self.other.pk).exists())
        self.client.force_login(self.processor)
        ticket = Ticket.objects.get(source_record_id="A-002")
        response = self.client.get("/", {"author": self.author.pk, "assignee": self.processor.pk, "queue": ticket.queue_id})
        self.assertTrue(response.context["filters"].is_valid())
        self.assertTrue(all(item.author_id == self.author.pk and item.assignee_id == self.processor.pk for item in response.context["page_obj"]))

    def test_analytics_respects_cohort_dates_and_invalid_range(self):
        self.client.force_login(self.processor)
        self.assertEqual(self.client.get("/analytics/", {"mode": "demo", "date_from": "2100-01-01", "date_to": "2100-12-31"}).context["total"], 0)
        response = self.client.get("/analytics/", {"date_from": "2026-12-01", "date_to": "2026-01-01"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.context["total"], 0)

    def test_historical_analytics_excludes_future_result_and_classification(self):
        start = timezone.now().replace(second=0, microsecond=0)-timedelta(days=5)
        with patch("feedback.services.timezone.now", return_value=start):
            ticket = create_ticket(actor=self.author, idempotency_key="historical-cut", data={"category": Category.objects.filter(code="communications").first() or Category.objects.first(),
                "request_type": "PROBLEM", "description": "Историческая проверка классификации и закрытия", "reported_priority": "LOW"})
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=1)):
            self.command(ticket, "start")
        cutoff = start+timedelta(hours=2)
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=3)):
            self.command(ticket, "reclassify", actor=self.coordinator, category_id=ticket.category_id, effective_priority="HIGH", reason="Повысился риск для бизнеса")
            self.command(ticket, "decline", reason="Недостаточно ожидаемой пользы")
        self.client.force_login(self.processor)
        response = self.client.get("/analytics/", {"mode": "demo", "date_from": timezone.localdate(start).isoformat(), "date_to": timezone.localdate(start).isoformat(), "as_of": timezone.localtime(cutoff).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["closed"], 0)
        self.assertEqual(response.context["reviewed"], 0)
        self.assertEqual(response.context["answered_count"], 0)
        self.assertEqual({row["code"]: row["count"] for row in response.context["priority_rows"]}, {"LOW": 1, "MEDIUM": 0, "HIGH": 0})

    def test_historical_analytics_keeps_prior_closed_cycle_after_reopen(self):
        start = timezone.now().replace(second=0, microsecond=0)-timedelta(days=5)
        with patch("feedback.services.timezone.now", return_value=start):
            ticket = create_ticket(actor=self.author, idempotency_key="historical-reopen", data={"category": Category.objects.first(),
                "request_type": "PROBLEM", "description": "Исторический цикл не подменяется после повторного открытия"})
            self.command(ticket, "start")
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=1)):
            self.command(ticket, "decline", reason="Исходный вариант не подходит")
        cutoff = start+timedelta(hours=2)
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=3)):
            self.command(ticket, "reopen", actor=self.author, reason="Появились новые существенные сведения")
        self.client.force_login(self.processor)
        response = self.client.get("/analytics/", {"mode": "demo", "date_from": timezone.localdate(start).isoformat(), "date_to": timezone.localdate(start).isoformat(), "as_of": timezone.localtime(cutoff).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["closed"], 1)
        self.assertEqual(response.context["reviewed"], 1)
        self.assertEqual(response.context["awaiting_count"], 0)
        self.assertEqual(ticket.cycle_no, 2)

    def test_historical_unassigned_state_is_not_replaced_by_future_assignment(self):
        start = timezone.now().replace(hour=3, minute=0, second=0, microsecond=0)-timedelta(days=4)
        with patch("feedback.services.timezone.now", return_value=start):
            ticket = create_ticket(actor=self.author, idempotency_key="historical-unassigned", data={"category": Category.objects.first(),
                "request_type": "PROBLEM", "description": "Историческое отсутствие исполнителя должно сохраняться"})
            self.command(ticket, "start")
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=1)):
            self.command(ticket, "assign", actor=self.coordinator, queue_id=ticket.queue_id, reason="Уточнение ответственного")
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=3)):
            self.command(ticket, "assign", actor=self.coordinator, queue_id=ticket.queue_id, assignee_id=self.processor.pk, reason="Назначен ответственный за результат")
        self.client.force_login(self.processor)
        response = self.client.get("/analytics/", {"mode": "demo", "date_from": timezone.localdate(start).isoformat(), "date_to": timezone.localdate(start).isoformat(),
            "as_of": timezone.localtime(start+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unassigned"], 1)

    def test_historical_reclassification_does_not_erase_response_breach(self):
        start = timezone.now().replace(hour=3, minute=0, second=0, microsecond=0)-timedelta(days=7)
        # Select a working morning independently of the test execution weekday.
        while start.weekday() >= 5:
            start -= timedelta(days=1)
        with patch("feedback.services.timezone.now", return_value=start):
            ticket = create_ticket(actor=self.author, idempotency_key="historical-breach", data={"category": Category.objects.first(),
                "request_type": "PROBLEM", "description": "Смена приоритета не должна стирать нарушение ответа", "reported_priority": "HIGH"})
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=6)):
            self.command(ticket, "reclassify", actor=self.coordinator, category_id=ticket.category_id, effective_priority="LOW", reason="Снижение риска после диагностики")
        self.client.force_login(self.processor)
        response = self.client.get("/analytics/", {"mode": "demo", "date_from": timezone.localdate(start).isoformat(), "date_to": timezone.localdate(start).isoformat(),
            "as_of": timezone.localtime(start+timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overdue"], 1)
        self.assertEqual(response.context["sla_due_count"], 1)
        self.assertEqual(response.context["sla_rate"], 0)

    def test_legacy_snapshot_restores_implementation_after_answer(self):
        start = timezone.now().replace(hour=3, minute=0, second=0, microsecond=0)-timedelta(days=20)
        with patch("feedback.services.timezone.now", return_value=start):
            ticket = create_ticket(actor=self.author, idempotency_key="legacy-answer-cut", data={"category": Category.objects.first(),
                "request_type": "PROBLEM", "description": "Восстановление старого исторического среза после ответа"})
            self.command(ticket, "start")
            self.command(ticket, "approve", reason="Результат полезен", criteria="Проверяемый критерий", plan_due_at=start+timedelta(days=30))
            self.command(ticket, "ask_info", question="Какой вариант следует реализовать?")
        with patch("feedback.services.timezone.now", return_value=start+timedelta(hours=1)):
            self.command(ticket, "answer_info", actor=self.author, message="Первый вариант подходит для реализации")
        for event in ticket.events.all():
            event.payload.pop("state_snapshot", None)
            event.save(update_fields=["payload"])
        self.client.force_login(self.processor)
        response = self.client.get("/analytics/", {"mode": "demo", "date_from": timezone.localdate(start).isoformat(), "date_to": timezone.localdate(start).isoformat(),
            "as_of": timezone.localtime(start+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual({row["code"]: row["count"] for row in response.context["status_rows"]}["IN_PROGRESS"], 1)
        self.assertEqual(response.context["historical_fallback_count"], 1)

    def test_malformed_additional_roles_cannot_crash_or_expand_access(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        ticket.queue.members.add(self.author)
        for malformed in ("processor", {"processor": True}, [{}], ["processor", {}], ["unknown-role"], 42):
            with self.subTest(extra_roles=malformed):
                self.author.extra_roles = malformed
                self.assertFalse(has_role(self.author, "processor", "coordinator", "manager"))
                self.assertEqual(visible_tickets(self.author).count(), 4)

    def test_operational_selector_excludes_malformed_legacy_role_json(self):
        malformed = ["processor", {"processor": True}, [{}], ["processor", {}], ["unknown-role"], 42, None]
        people = [User(username=f"invalid-extra-{index}", role="author", extra_roles=roles)
                  for index, roles in enumerate(malformed) if roles is not None]
        User.objects.bulk_create(people)
        valid = User.objects.create(username="valid-extra-processor", role="author", extra_roles=["processor"])
        ids = set(operational_users().values_list("pk", flat=True))
        self.assertIn(valid.pk, ids)
        self.assertTrue(ids.isdisjoint(person.pk for person in people))
        self.assertIn(self.processor.pk, ids)

    def test_future_analytics_snapshot_is_rejected(self):
        self.client.force_login(self.processor)
        future = timezone.localtime(timezone.now()+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.get("/analytics/", {"mode": "demo", "as_of": future})
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "Дата среза не может находиться в будущем", status_code=422)
        self.assertEqual(response.context["total"], 0)

    def long_history_ticket(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        now = timezone.now()
        ProcessingCycle.objects.bulk_create([ProcessingCycle(ticket=ticket, number=number, started_at=now-timedelta(days=13-number),
            closed_at=now-timedelta(days=12-number) if number < 12 else None,
            resolution_code="DECLINED" if number < 12 else "", resolution_summary=f"OLD-CYCLE-RESULT-{number}" if number < 12 else "") for number in range(2, 13)])
        ticket.cycle_no = 12
        ticket.save(update_fields=["cycle_no"])
        Comment.objects.bulk_create([Comment(ticket=ticket, author=self.author, message=f"PUBLIC-HISTORY-COMMENT-{number}", created_at=now+timedelta(seconds=number)) for number in range(55)])
        Comment.objects.create(ticket=ticket, author=self.processor, visibility="internal", message="PRIVATE-HISTORY-COMMENT")
        Event.objects.bulk_create([Event(ticket=ticket, actor=self.processor, kind="COMMENT", summary=f"PUBLIC-HISTORY-EVENT-{number}", sequence=1100+number) for number in range(55)])
        Event.objects.create(ticket=ticket, actor=self.processor, kind="COMMENT", visibility="internal", summary="PRIVATE-HISTORY-EVENT", sequence=9999)
        Event.objects.bulk_create([Event(ticket=ticket, actor=self.processor, kind="IMPLEMENT", summary=f"CYCLE-RESULT-{number}", payload={"cycle_no": number, "evidence": f"CYCLE-EVIDENCE-{number}"}, sequence=1000+number) for number in range(1, 13)])
        Decision.objects.bulk_create([Decision(ticket=ticket, actor=self.processor, code="DECLINED", reason=f"HISTORICAL-DECISION-{number}", cycle_no=number) for number in range(1, 12)])
        InfoRequest.objects.bulk_create([InfoRequest(ticket=ticket, actor=self.processor, question=f"OLD-QUESTION-{number}", response=f"OLD-ANSWER-{number}", resume_status="REVIEW", due_at=now, answered_at=now,
            created_at=now+timedelta(seconds=number)) for number in range(30)])
        return ticket

    def test_detail_history_is_bounded_and_last_pages_are_scoped(self):
        ticket = self.long_history_ticket()
        self.client.force_login(self.author)
        url = reverse("feedback:detail", args=[ticket.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["comments_page"]), 50)
        self.assertEqual(len(response.context["events_page"]), 50)
        self.assertEqual(len(response.context["cycles"]), 10)
        self.assertEqual(len(response.context["info_page"]), 25)
        self.assertTrue(all(len(cycle.history_results) <= 1 for cycle in response.context["cycles"]))
        self.assertEqual(response.context["decisions"], [])
        for text in ("PRIVATE-HISTORY-COMMENT", "PRIVATE-HISTORY-EVENT", "OLD-QUESTION-0"):
            self.assertNotContains(response, text)
        last = self.client.get(url, {"comments_page": 2, "events_page": 2, "cycles_page": 2, "info_page": 2})
        self.assertContains(last, "OLD-QUESTION-0")
        self.assertContains(last, "PUBLIC-HISTORY-COMMENT-0")
        self.assertContains(last, "Цикл 1")
        self.assertNotContains(last, "PRIVATE-HISTORY-COMMENT")
        self.assertNotContains(last, "PRIVATE-HISTORY-EVENT")
        previous = urlparse(last.context["comments_navigation"]["previous"])
        self.assertEqual(previous.path, url)
        self.assertEqual(parse_qs(previous.query), {"comments_page": ["1"], "events_page": ["2"], "cycles_page": ["2"], "info_page": ["2"]})

    def test_conflict_keeps_private_draft_and_independent_history_pages(self):
        ticket = self.long_history_ticket()
        stale = ticket.version
        self.command(ticket, "comment", message="Concurrent change before historical-page retry")
        self.client.force_login(self.processor)
        command_url = reverse("feedback:command", args=[ticket.pk])+"?comments_page=2&events_page=2&cycles_page=2&info_page=2"
        response = self.client.post(command_url, {"command": "comment", "version": stale, "message": "PRIVATE-HISTORY-DRAFT", "visibility": "internal"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "PRIVATE-HISTORY-DRAFT", status_code=409)
        self.assertContains(response, '<option value="internal" selected', status_code=409)
        for panel in ("comments_page", "events_page", "cycles_page", "info_page"):
            self.assertEqual(response.context[panel].number, 2)
        self.assertContains(response, "comments_page=2&amp;events_page=2&amp;cycles_page=2&amp;info_page=2", status_code=409)
        self.assertTrue(response.context["comments_navigation"]["previous"].startswith(reverse("feedback:detail", args=[ticket.pk])+"?"))

    def test_praise_shows_only_allowed_decision_actions(self):
        ticket = create_ticket(actor=self.author, idempotency_key="praise-ui-matrix", data={"category": Category.objects.first(),
            "request_type": "PRAISE", "description": "Благодарность за качественную помощь сотрудникам"})
        self.command(ticket, "start")
        self.client.force_login(self.processor)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertContains(response, 'value="acknowledge"')
        self.assertNotContains(response, 'value="approve"')
        self.assertNotContains(response, 'value="decline"')

    def test_reassignment_marker_keeps_approved_result_and_hides_duplicate_decision(self):
        ticket = self.active_ticket()
        ticket.status = "REVIEW"
        ticket.assignee = None
        ticket.save(update_fields=["status", "assignee"])
        cycle = ticket.current_cycle
        cycle.policy["assignment_resume_status"] = "IN_PROGRESS"
        cycle.save(update_fields=["policy"])
        self.client.force_login(self.coordinator)
        response = self.client.get(reverse("feedback:detail", args=[ticket.pk]))
        self.assertContains(response, "Требуется переназначение исполнителя")
        self.assertContains(response, 'value="assign"')
        self.assertNotContains(response, 'value="approve"')
        self.assertNotContains(response, 'value="decline"')
        self.assertNotContains(response, 'value="ask_info"')
        self.assertNotContains(response, 'value="duplicate"')

    def test_current_cycle_prefetch_avoids_per_card_query(self):
        self.client.force_login(self.processor)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        per_card = [item["sql"] for item in captured.captured_queries
                    if item["sql"].split(" FROM ", 1)[-1].startswith('"feedback_processingcycle"') and "LIMIT 1" in item["sql"]]
        self.assertEqual(per_card, [])

    def test_queue_membership_does_not_multiply_scope_or_force_ticket_body_sort(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        ticket.queue.members.add(self.author, self.other)
        ids = list(visible_tickets(self.processor).values_list("pk", flat=True))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 20)
        sql = str(visible_tickets(self.processor).query).upper()
        self.assertNotIn("SELECT DISTINCT", sql)
        self.assertNotIn("JOIN", sql)
        self.client.force_login(self.processor)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(captured), 36)

    def test_canonical_search_reaches_beyond_first_hundred_and_is_scoped(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        copies = [Ticket(number=f"ZZ-TARGET-{n:03}", author=self.author, category=ticket.category, queue=ticket.queue,
                         assignee=self.processor, request_type="PROBLEM", description=f"Кандидат номер {n} для проверки выбора") for n in range(105)]
        Ticket.objects.bulk_create(copies)
        self.client.force_login(self.processor)
        response = self.client.get(reverse("feedback:canonical_search", args=[ticket.pk]), {"q": "ZZ-TARGET-104"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["count"], 1)
        self.assertIn("ZZ-TARGET-104", response.json()["results"][0]["label"])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("feedback:canonical_search", args=[ticket.pk])).status_code, 404)

    def test_full_text_and_similar_search_do_not_expand_object_scope(self):
        ticket = Ticket.objects.get(source_record_id="A-002")
        ticket.description = "Уведомления о согласовании заявок сотрудниками"
        ticket.save(update_fields=["description"])
        own = Ticket.objects.create(author=self.author, category=ticket.category, queue=ticket.queue, request_type="PROBLEM", description="Уведомление о согласовании заявки потерялось")
        hidden = Ticket.objects.create(author=self.other, category=ticket.category, queue=ticket.queue, request_type="PROBLEM", description="Уведомление о согласовании заявки скрыто")
        self.client.force_login(self.author)
        results = self.client.get("/", {"q": "согласовании"}).context["page_obj"]
        self.assertIn(own.pk, [item.pk for item in results])
        self.assertNotIn(hidden.pk, [item.pk for item in results])
        response = self.client.get(reverse("feedback:similar", args=[ticket.pk]))
        self.assertContains(response, own.number)
        self.assertNotContains(response, hidden.number)
