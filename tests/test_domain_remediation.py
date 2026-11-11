"""Regression tests for the domain findings in the 2026-11-11 audit."""
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from feedback.models import Category, Event, Notification, Outbox, Queue, RoutingRule, Ticket, TicketRelation, User
from feedback.permissions import visible_tickets
from feedback.services import (DomainError, create_ticket, execute_command, process_outbox,
                               reconcile_assignments, retry_delivery, revise_routing_rule)
from feedback.sla import add_business_hours, business_seconds, policy_snapshot, reopen_deadline, subtract_business_hours


class DomainRemediationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user('domain-author', role='author')
        cls.processor = User.objects.create_user('domain-processor', role='processor')
        cls.coordinator = User.objects.create_user('domain-coordinator', role='coordinator', restricted_access=True)
        cls.outsider = User.objects.create_user('domain-outsider', role='coordinator')
        cls.admin = User.objects.create_user('domain-admin', role='admin')
        cls.queue = Queue.objects.create(code='default', name='General', owner=cls.coordinator)
        cls.queue.members.add(cls.processor, cls.coordinator)
        cls.category = Category.objects.create(code='domain-category', name='General category')

    def ticket(self, **overrides):
        data = {'category': self.category, 'request_type': 'SUGGESTION',
                'description': 'A complete synthetic request to verify domain invariants.'}
        data.update(overrides)
        return create_ticket(actor=self.author, data=data, idempotency_key=str(uuid4()))

    def cmd(self, ticket, command, actor=None, **data):
        result = execute_command(actor=actor or self.processor, ticket_id=ticket.pk,
                                 command=command, expected_version=ticket.version, data=data)
        ticket.refresh_from_db()
        return result

    def review(self, ticket):
        self.cmd(ticket, 'start')
        return ticket

    def approve(self, ticket):
        self.review(ticket)
        self.cmd(ticket, 'approve', reason='Approved after review', criteria='Observable result',
                 plan_due_at=timezone.now()+timedelta(days=14))
        return ticket

    def waiting(self, ticket=None):
        ticket = self.review(ticket or self.ticket())
        self.cmd(ticket, 'ask_info', question='Please provide the missing details')
        return ticket, ticket.info_requests.get(answered_at__isnull=True)

    def test_praise_cannot_be_approved_or_implemented(self):
        ticket = self.review(self.ticket(request_type='PRAISE'))
        for command in ('approve', 'implement', 'decline'):
            with self.subTest(command=command), self.assertRaises(DomainError) as caught:
                self.cmd(ticket, command, reason='Reason', criteria='Result',
                         plan_due_at=timezone.now()+timedelta(days=1), result='Done', evidence='Verified')
            self.assertEqual(caught.exception.code, 'invalid_type_command')
        self.assertFalse(ticket.decisions.exists())
        self.cmd(ticket, 'acknowledge', reason='Feedback passed to the team')
        self.assertEqual(ticket.resolution_code, 'ACKNOWLEDGED')

    def test_coordinator_can_resume_info_with_reason_and_end_pause(self):
        ticket, info = self.waiting()
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'resume_info', actor=self.coordinator, reason='')
        self.cmd(ticket, 'resume_info', actor=self.coordinator, reason='Existing details are sufficient')
        info.refresh_from_db()
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertIsNotNone(info.answered_at)
        self.assertNotIn('active_pause', ticket.current_cycle.policy)
        self.assertEqual(ticket.events.get(kind='RESUME_INFO').payload['reason'], 'Existing details are sufficient')

    def test_processor_cannot_resume_or_extend_info(self):
        ticket, info = self.waiting()
        for command in ('resume_info', 'extend_info'):
            with self.subTest(command=command), self.assertRaises(PermissionDenied):
                self.cmd(ticket, command, reason='Reason', due_at=info.due_at+timedelta(hours=1))
        self.assertEqual(ticket.status, 'WAITING_INFO')

    def test_extension_resets_reminder_identity_and_keeps_total_pause_cap(self):
        ticket, info = self.waiting()
        previous_due = info.due_at
        at = add_business_hours(info.created_at, 8, policy=ticket.current_cycle.policy)
        due = add_business_hours(at, 40, policy=ticket.current_cycle.policy)
        with patch('feedback.services.timezone.now', return_value=at):
            self.cmd(ticket, 'extend_info', actor=self.coordinator, reason='Author needs extra time', due_at=due)
        info.refresh_from_db()
        self.assertEqual(info.reminder_generation, 2)
        self.assertIsNone(info.reminder_delivered_at)
        cycle = ticket.current_cycle
        pause = cycle.policy['active_pause']
        self.assertLessEqual(cycle.policy['pause_used_seconds']+pause['allocated_seconds'], 40*3600)
        self.assertEqual(parse_datetime(ticket.events.get(kind='EXTEND_INFO').payload['previous_due_at']), previous_due)

    def test_approve_rejects_revoked_role_queue_and_clearance(self):
        for field in ('role', 'membership'):
            ticket = self.review(self.ticket())
            with self.subTest(field=field):
                if field == 'role':
                    self.processor.role = 'author'
                    self.processor.save(update_fields=['role'])
                else:
                    self.queue.members.remove(self.processor)
                with self.assertRaises(DomainError) as caught:
                    self.cmd(ticket, 'approve', actor=self.coordinator, reason='Approve', criteria='Result',
                             plan_due_at=timezone.now()+timedelta(days=1))
                self.assertEqual(caught.exception.code, 'assignee_ineligible')
                self.assertFalse(ticket.decisions.exists())
                self.processor.role = 'processor'
                self.processor.save(update_fields=['role'])
                self.queue.members.add(self.processor)

    def test_started_implementation_cannot_be_unassigned(self):
        ticket = self.approve(self.ticket())
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'assign', actor=self.coordinator, queue_id=self.queue.pk, reason='No one')
        self.cmd(ticket, 'ask_info', question='Need a specific detail')
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'assign', actor=self.coordinator, queue_id=self.queue.pk, reason='No one')

    def test_restricted_approval_rejects_lost_clearance(self):
        queue = Queue.objects.create(code='restricted', name='Restricted', restricted=True, owner=self.coordinator)
        queue.members.add(self.coordinator, self.processor)
        self.processor.restricted_access = True
        self.processor.save(update_fields=['restricted_access'])
        ticket = self.review(self.ticket(confidentiality='RESTRICTED'))
        self.processor.restricted_access = False
        self.processor.save(update_fields=['restricted_access'])
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'approve', actor=self.coordinator, reason='Approve', criteria='Result',
                     plan_due_at=timezone.now()+timedelta(days=1))

    def test_malformed_ticket_and_duplicate_ids_return_domain_errors(self):
        with self.assertRaises(DomainError) as caught:
            execute_command(actor=self.coordinator, ticket_id='invalid-uuid', command='start', expected_version=1, data={})
        self.assertEqual(caught.exception.status, 404)
        ticket = self.review(self.ticket())
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'duplicate', canonical_id='invalid-uuid', reason='Malformed request')

    def test_assignment_event_preserves_before_after_and_reason(self):
        ticket = self.review(self.ticket())
        self.cmd(ticket, 'assign', actor=self.coordinator, queue_id=self.queue.pk,
                 assignee_id=self.coordinator.pk, reason='Processor is on leave')
        payload = ticket.events.get(kind='ASSIGN').payload
        self.assertEqual(payload['before'], {'queue_id': self.queue.pk, 'assignee_id': self.processor.pk})
        self.assertEqual(payload['after'], {'queue_id': self.queue.pk, 'assignee_id': self.coordinator.pk})
        self.assertEqual(payload['reason'], 'Processor is on leave')

    def test_failed_delivery_retry_is_scoped_audited_and_idempotent(self):
        ticket = self.ticket()
        job = Outbox.objects.filter(ticket=ticket).first()
        Outbox.objects.filter(pk=job.pk).update(state='failed', attempts=8, last_error='RuntimeError')
        with self.assertRaises(PermissionDenied):
            retry_delivery(actor=self.outsider, outbox_id=job.pk, reason='Retry')
        retry_delivery(actor=self.coordinator, outbox_id=job.pk, reason='Channel is healthy again')
        job.refresh_from_db()
        self.assertEqual((job.state, job.attempts, job.last_error), ('pending', 0, ''))
        event = ticket.events.get(kind='DELIVERY_RETRY')
        self.assertEqual(event.visibility, 'internal')
        self.assertEqual(event.payload['previous_attempts'], 8)
        with self.assertRaises(DomainError):
            retry_delivery(actor=self.coordinator, outbox_id=job.pk, reason='Duplicate retry')
        process_outbox()
        process_outbox()
        self.assertEqual(Notification.objects.filter(outbox=job).count(), 1)

    def test_late_worker_must_deliver_reminder_and_wait_grace(self):
        ticket, info = self.waiting()
        late = info.due_at+timedelta(days=2)
        with patch('feedback.services.timezone.now', return_value=late):
            process_outbox()
            ticket.refresh_from_db()
            info.refresh_from_db()
            self.assertTrue(info.expired_notified)
            self.assertIsNotNone(info.reminder_delivered_at)
            self.assertTrue(ticket.events.get(kind='INFO_REMINDER').payload['late'])
            with self.assertRaises(DomainError):
                self.cmd(ticket, 'no_response', actor=self.coordinator, reason='Not enough detail')
        grace = add_business_hours(info.reminder_delivered_at, 8, policy=ticket.current_cycle.policy)
        with patch('feedback.services.timezone.now', return_value=grace):
            self.cmd(ticket, 'no_response', actor=self.coordinator, reason='No reply after delivered reminder')
        self.assertEqual(ticket.resolution_code, 'NO_RESPONSE')

    def test_failed_reminder_delivery_does_not_allow_no_response(self):
        ticket, info = self.waiting()
        late = info.due_at+timedelta(days=2)
        with patch('feedback.services.timezone.now', return_value=late), patch(
                'feedback.services.Notification.objects.get_or_create', side_effect=RuntimeError('Unavailable')):
            process_outbox()
            ticket.refresh_from_db()
            info.refresh_from_db()
            self.assertIsNone(info.reminder_delivered_at)
            with self.assertRaises(DomainError):
                self.cmd(ticket, 'no_response', actor=self.coordinator, reason='No reply')

    def test_reminder_selection_does_not_scan_future_question_cycles(self):
        from feedback.services import _remind_info_requests
        ticket, info = self.waiting()
        self.assertGreater(info.reminder_due_at, timezone.now())
        with self.assertNumQueries(1):
            _remind_info_requests()
        self.assertFalse(ticket.events.filter(kind='INFO_REMINDER').exists())

    def test_old_pending_reminder_is_skipped_after_extension(self):
        from feedback.services import _remind_info_requests
        ticket, info = self.waiting()
        reminder_at = info.reminder_due_at
        with patch('feedback.services.timezone.now', return_value=reminder_at):
            _remind_info_requests()
            ticket.refresh_from_db()
            self.cmd(ticket, 'extend_info', actor=self.coordinator, reason='Additional investigation',
                     due_at=add_business_hours(reminder_at, 40, policy=ticket.current_cycle.policy))
            process_outbox()
        self.assertFalse(Notification.objects.filter(ticket=ticket, outbox__event__kind='INFO_REMINDER').exists())
        info.refresh_from_db()
        self.assertEqual(info.reminder_generation, 2)
        self.assertIsNone(info.reminder_delivered_at)

    def test_backfill_preserves_existing_reminder_and_actual_delivery(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        ticket, info = self.waiting()
        with patch('feedback.services.timezone.now', return_value=info.reminder_due_at):
            process_outbox()
        delivered = ticket.events.get(kind='INFO_REMINDER')
        notification = Notification.objects.get(user=self.author, outbox__event=delivered)
        type(info).objects.filter(pk=info.pk).update(reminder_due_at=None, reminder_delivered_at=None)
        migration = import_module('feedback.migrations.0005_info_reminder_schedule')
        migration.schedule_existing_questions(apps, connection.schema_editor())
        info.refresh_from_db()
        self.assertIsNone(info.reminder_due_at)
        self.assertEqual(info.reminder_delivered_at, notification.created_at)
        ticket.refresh_from_db()
        process_outbox()
        self.assertEqual(ticket.events.filter(kind='INFO_REMINDER').count(), 1)

    def test_backward_calendar_crosses_weekend_and_saved_holiday(self):
        policy = policy_snapshot()
        policy['holidays'] = ['2026-11-16']
        due = datetime(2026, 11, 17, 10, tzinfo=ZoneInfo(policy['timezone']))
        reminder = subtract_business_hours(due, 8, policy=policy)
        self.assertEqual((reminder.date().isoformat(), reminder.hour), ('2026-11-13', 10))
        self.assertEqual(business_seconds(reminder, due, policy=policy), 8*3600)

    def test_reopened_duplicate_archives_relation_and_allows_reverse_link(self):
        first, second = self.review(self.ticket()), self.review(self.ticket())
        self.cmd(first, 'duplicate', canonical_id=second.pk, reason='Initially same issue')
        self.cmd(first, 'reopen', actor=self.author, reason='Actually a distinct issue')
        relation = TicketRelation.objects.get(ticket=first, canonical=second)
        self.assertFalse(relation.active)
        self.assertIsNotNone(relation.archived_at)
        self.cmd(second, 'duplicate', canonical_id=first.pk, reason='First is now the primary issue')
        self.assertEqual(second.resolution_code, 'DUPLICATE')

    def test_number_collision_retries_without_losing_atomic_writes(self):
        existing = self.ticket()
        with patch('feedback.services.ticket_number', side_effect=[existing.number, 'MT-'+'F'*28]):
            ticket = self.ticket()
        self.assertNotEqual(existing.number, ticket.number)
        self.assertEqual(ticket.events.count(), 1)
        self.assertEqual(ticket.cycles.count(), 1)

    def test_exhausted_number_collisions_return_retryable_domain_error(self):
        existing = self.ticket()
        with patch('feedback.services.ticket_number', return_value=existing.number):
            with self.assertRaises(DomainError) as caught:
                self.ticket()
        self.assertEqual((caught.exception.code, caught.exception.status), ('number_unavailable', 503))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_reopen_uses_saved_calendar_after_timezone_change(self):
        instant = datetime(2026, 11, 11, 10, tzinfo=ZoneInfo('Asia/Bishkek'))
        with patch('feedback.services.timezone.now', return_value=instant):
            ticket = self.review(self.ticket())
            self.cmd(ticket, 'decline', reason='Not needed currently')
        saved = ticket.current_cycle.policy
        deadline = reopen_deadline(ticket.closed_at, policy=saved)
        with override_settings(BUSINESS_TIMEZONE='UTC'), patch('feedback.services.timezone.now', return_value=deadline):
            self.cmd(ticket, 'reopen', actor=self.author, reason='Original time window still applies')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertEqual(ticket.cycles.get(number=1).policy['timezone'], 'Asia/Bishkek')

    def test_pause_calculation_uses_saved_calendar_after_timezone_change(self):
        instant = datetime(2026, 11, 11, 10, tzinfo=ZoneInfo('Asia/Bishkek'))
        with patch('feedback.services.timezone.now', return_value=instant):
            ticket, info = self.waiting()
        saved = ticket.current_cycle.policy
        answered = add_business_hours(info.created_at, 3, policy=saved)
        with override_settings(BUSINESS_TIMEZONE='UTC'), patch('feedback.services.timezone.now', return_value=answered):
            self.cmd(ticket, 'answer_info', actor=self.author, message='The requested information')
        info.refresh_from_db()
        self.assertEqual(info.paused_seconds, 3*3600)
        self.assertEqual(business_seconds(info.created_at, answered, policy=saved), 3*3600)

    def test_reclassification_keeps_reported_priority_and_audits_route(self):
        ticket = self.review(self.ticket())
        category = Category.objects.create(code='different', name='Different category')
        self.cmd(ticket, 'reclassify', actor=self.coordinator, category_id=category.pk,
                 effective_priority='HIGH', reason='Time sensitive business impact')
        self.assertEqual((ticket.category_id, ticket.effective_priority, ticket.reported_priority),
                         (category.pk, 'HIGH', 'MEDIUM'))
        event = ticket.events.get(kind='RECLASSIFY')
        self.assertEqual(event.payload['before']['category_id'], self.category.pk)
        self.assertEqual(event.payload['after']['effective_priority'], 'HIGH')
        self.assertIn('routing', event.payload)
        self.assertIn('state_snapshot', event.payload)
        with self.assertRaises(PermissionDenied):
            self.cmd(ticket, 'reclassify', effective_priority='LOW', reason='Not authorized')

    def test_reclassification_cannot_change_approved_type(self):
        ticket = self.approve(self.ticket())
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'reclassify', actor=self.coordinator, request_type='PRAISE', reason='Wrong type')
        self.assertEqual(ticket.request_type, 'SUGGESTION')

    def test_priority_change_preserves_manual_queue_and_assignee(self):
        ticket = self.review(self.ticket())
        manual = Queue.objects.create(code='manual', name='Special handling', owner=self.coordinator)
        manual.members.add(self.coordinator)
        self.cmd(ticket, 'assign', actor=self.coordinator, queue_id=manual.pk,
                 assignee_id=self.coordinator.pk, reason='Special handling is required')
        self.cmd(ticket, 'reclassify', actor=self.coordinator, effective_priority='HIGH', reason='Urgent impact')
        self.assertEqual((ticket.queue_id, ticket.assignee_id), (manual.pk, self.coordinator.pk))
        self.assertTrue(ticket.events.get(kind='RECLASSIFY').payload['routing']['kept'])

    def test_route_can_assign_eligible_queue_owner_without_member_role_processor(self):
        self.queue.members.clear()
        ticket = self.ticket()
        self.assertEqual(ticket.assignee_id, self.coordinator.pk)
        self.cmd(ticket, 'start', actor=self.coordinator)
        self.cmd(ticket, 'approve', actor=self.coordinator, reason='Owner handles request', criteria='Observable result',
                 plan_due_at=timezone.now()+timedelta(days=1))
        self.assertEqual(ticket.status, 'IN_PROGRESS')

    def test_reopen_reassignment_has_before_after_audit(self):
        ticket = self.review(self.ticket())
        self.cmd(ticket, 'decline', reason='Closed for now')
        self.processor.is_active = False
        self.processor.save(update_fields=['is_active'])
        self.cmd(ticket, 'reopen', actor=self.author, reason='Need a second review')
        assignment = ticket.events.get(kind='REOPEN').payload['assignment']
        self.assertEqual(assignment['before']['assignee_id'], self.processor.pk)
        self.assertEqual(assignment['after']['assignee_id'], self.coordinator.pk)
        self.assertTrue(assignment['reason'])

    def test_routing_revision_preserves_immutable_source_version(self):
        rule = RoutingRule.objects.create(category=self.category, queue=self.queue, priority=100)
        replacement = revise_routing_rule(actor=self.admin, rule_id=rule.pk, data={'priority': 101})
        rule.refresh_from_db()
        self.assertEqual((rule.priority, rule.version, rule.active), (100, 1, False))
        self.assertEqual((replacement.priority, replacement.version, replacement.supersedes_id), (101, 2, rule.pk))
        rule.priority = 999
        with self.assertRaises(ValidationError):
            rule.save()
        with self.assertRaises(DomainError):
            revise_routing_rule(actor=self.admin, rule_id=rule.pk, data={'priority': 102})

    def test_reconciliation_defaults_to_dry_run_then_audited_assignment(self):
        ticket = self.approve(self.ticket())
        self.processor.is_active = False
        self.processor.save(update_fields=['is_active'])
        rows = reconcile_assignments(actor=self.coordinator)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assignee_id, self.processor.pk)
        self.assertEqual(rows[0]['status'], 'ready')
        rows = reconcile_assignments(actor=self.coordinator, apply=True)
        ticket.refresh_from_db()
        self.assertEqual((rows[0]['status'], ticket.assignee_id), ('reassigned', self.coordinator.pk))
        self.assertTrue(ticket.events.filter(kind='ASSIGN').exists())

    def test_author_answer_in_review_survives_legitimate_unassignment(self):
        ticket, info = self.waiting()
        self.cmd(ticket, 'assign', actor=self.coordinator, queue_id=self.queue.pk,
                 reason='Coordinator will choose the next reviewer')
        self.assertIsNone(ticket.assignee_id)
        self.cmd(ticket, 'answer_info', actor=self.author, message='The requested answer must be saved')
        info.refresh_from_db()
        self.assertEqual(info.response, 'The requested answer must be saved')
        self.assertIsNotNone(info.answered_at)
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertIsNone(ticket.assignee_id)
        self.assertNotIn('active_pause', ticket.current_cycle.policy)

    def test_author_answer_in_implementation_replaces_deactivated_assignee(self):
        ticket = self.approve(self.ticket())
        criteria, plan = ticket.criteria, ticket.plan_due_at
        self.cmd(ticket, 'ask_info', question='Which delivery variant is needed?')
        info = ticket.info_requests.get(answered_at__isnull=True)
        self.processor.is_active = False
        self.processor.save(update_fields=['is_active'])
        self.cmd(ticket, 'answer_info', actor=self.author, message='The second delivery variant')
        info.refresh_from_db()
        self.assertEqual(info.response, 'The second delivery variant')
        self.assertIsNotNone(info.answered_at)
        self.assertEqual((ticket.status, ticket.assignee_id), ('IN_PROGRESS', self.coordinator.pk))
        self.assertEqual((ticket.decision, ticket.criteria, ticket.plan_due_at), ('APPROVED', criteria, plan))
        event = ticket.events.get(kind='ANSWER_INFO')
        self.assertEqual(event.payload['assignment']['before']['assignee_id'], self.processor.pk)
        self.assertEqual(event.payload['assignment']['after']['assignee_id'], self.coordinator.pk)

    def hold_implementation_without_eligible_staff(self):
        ticket = self.approve(self.ticket())
        self.cmd(ticket, 'ask_info', question='Please provide the missing implementation detail')
        self.processor.is_active = False
        self.processor.save(update_fields=['is_active'])
        self.coordinator.role = 'author'
        self.coordinator.save(update_fields=['role'])
        self.cmd(ticket, 'answer_info', actor=self.author, message='Answer saved while the whole queue is unavailable')
        return ticket

    def restore_coordinator(self):
        self.coordinator.role = 'coordinator'
        self.coordinator.save(update_fields=['role'])

    def test_answer_without_any_replacement_holds_plan_and_assignment_resumes_work(self):
        ticket = self.hold_implementation_without_eligible_staff()
        self.assertEqual((ticket.status, ticket.assignee_id, ticket.decision), ('REVIEW', None, 'APPROVED'))
        self.assertEqual(ticket.current_cycle.policy['assignment_resume_status'], 'IN_PROGRESS')
        info = ticket.info_requests.get()
        self.assertTrue(info.response)
        self.assertIsNotNone(info.answered_at)
        original_plan, original_criteria = ticket.plan_due_at, ticket.criteria
        event = ticket.events.get(kind='ANSWER_INFO')
        self.assertTrue(event.payload['awaiting_assignment'])
        self.assertEqual(event.payload['state_snapshot']['assignment_resume_status'], 'IN_PROGRESS')
        self.restore_coordinator()
        canonical = self.ticket()
        for command in ('approve', 'decline', 'ask_info', 'duplicate'):
            with self.subTest(command=command), self.assertRaises(DomainError):
                self.cmd(ticket, command, actor=self.coordinator, reason='Cannot discard an existing approval',
                         criteria='Different result', plan_due_at=timezone.now()+timedelta(days=30), question='Another question', canonical_id=canonical.pk)
        self.cmd(ticket, 'reclassify', actor=self.coordinator, effective_priority='HIGH', reason='Priority has increased')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertEqual(ticket.current_cycle.policy['assignment_resume_status'], 'IN_PROGRESS')
        revised_category = Category.objects.create(code='held-reclassification', name='Reclassified held work')
        self.cmd(ticket, 'reclassify', actor=self.coordinator, category_id=revised_category.pk, reason='Correct handling category')
        self.assertIsNone(ticket.assignee_id)
        self.assertEqual(ticket.current_cycle.policy['assignment_resume_status'], 'IN_PROGRESS')
        with self.assertRaises(DomainError):
            self.cmd(ticket, 'assign', actor=self.coordinator, reason='Cannot resume without an owner')
        self.cmd(ticket, 'assign', actor=self.coordinator, assignee_id=self.coordinator.pk,
                 reason='Coordinator takes over the approved implementation')
        self.assertEqual((ticket.status, ticket.assignee_id), ('IN_PROGRESS', self.coordinator.pk))
        self.assertNotIn('assignment_resume_status', ticket.current_cycle.policy)
        self.assertEqual((ticket.criteria, ticket.plan_due_at), (original_criteria, original_plan))
        self.assertEqual(ticket.decisions.count(), 1)
        self.cmd(ticket, 'implement', actor=self.coordinator, result='Original criterion achieved', evidence='Verified')
        self.assertEqual(ticket.resolution_code, 'IMPLEMENTED')

    def test_hold_without_assignee_does_not_hide_implementation_overdue(self):
        ticket = self.hold_implementation_without_eligible_staff()
        after_plan = ticket.plan_due_at+timedelta(minutes=1)
        with patch('feedback.services.timezone.now', return_value=after_plan):
            self.assertTrue(ticket.is_overdue)
            process_outbox()
        self.assertTrue(ticket.events.filter(kind='IMPLEMENTATION_OVERDUE').exists())

    def test_author_withdrawal_of_held_implementation_still_needs_consent(self):
        ticket = self.hold_implementation_without_eligible_staff()
        self.cmd(ticket, 'withdraw', actor=self.author, reason='Please stop the already approved work')
        self.assertEqual(ticket.status, 'REVIEW')
        self.assertTrue(ticket.events.get(kind='WITHDRAW').payload['withdraw_requested'])
        self.restore_coordinator()
        self.cmd(ticket, 'withdraw', actor=self.coordinator, reason='Stopping the work has been agreed')
        self.assertEqual((ticket.status, ticket.resolution_code), ('CLOSED', 'WITHDRAWN'))

    def test_user_clean_rejects_malformed_additional_roles(self):
        for value in ({}, 'processor', ['unknown'], [{'role':'processor'}], None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                User(username='invalid-extra-roles', extra_roles=value).clean()
        User(username='valid-extra-roles', extra_roles=['processor', 'coordinator']).clean()

    def test_author_can_reopen_without_eligible_staff_and_old_result_is_preserved(self):
        ticket = self.approve(self.ticket())
        self.cmd(ticket, 'implement', result='Completed original improvement', evidence='Original evidence must remain')
        original = ticket.cycles.get(number=1)
        original_closed, original_result = original.closed_at, original.resolution_summary
        for user in (self.processor, self.coordinator):
            user.is_active = False
            user.save(update_fields=['is_active'])
        self.cmd(ticket, 'reopen', actor=self.author, reason='A new verified issue needs investigation')
        self.assertEqual((ticket.status, ticket.assignee_id, ticket.queue_id, ticket.cycle_no),
                         ('REVIEW', None, self.queue.pk, 2))
        self.assertEqual((ticket.decision, ticket.criteria, ticket.plan_due_at), ('', '', None))
        original.refresh_from_db()
        self.assertEqual((original.closed_at, original.resolution_summary, original.resolution_code),
                         (original_closed, original_result, 'IMPLEMENTED'))
        self.assertIn('Original evidence must remain', original.resolution_summary)
        self.assertEqual(ticket.decisions.count(), 1)
        self.assertIsNotNone(ticket.current_cycle.started_at)
        event = ticket.events.get(kind='REOPEN')
        self.assertTrue(event.payload['awaiting_assignment'])
        self.assertTrue(event.payload['assignment_unavailable_reason'])
        self.assertIsNone(event.payload['assignment']['after']['assignee_id'])
        self.coordinator.is_active = True
        self.coordinator.save(update_fields=['is_active'])
        with self.assertRaises(DomainError) as caught:
            self.cmd(ticket, 'approve', actor=self.coordinator, reason='Must assign first', criteria='New result',
                     plan_due_at=timezone.now()+timedelta(days=2))
        self.assertEqual(caught.exception.code, 'assignee_ineligible')
        self.cmd(ticket, 'assign', actor=self.coordinator, assignee_id=self.coordinator.pk, reason='Coordinator accepts responsibility')
        self.cmd(ticket, 'approve', actor=self.coordinator, reason='Second cycle approved', criteria='New verifiable result',
                 plan_due_at=timezone.now()+timedelta(days=2))
        self.assertEqual(ticket.status, 'IN_PROGRESS')

    def test_restricted_author_can_reopen_after_all_staff_lose_clearance(self):
        restricted = Queue.objects.create(code='restricted', name='Restricted review', restricted=True, owner=self.coordinator)
        restricted.members.add(self.coordinator)
        ticket = self.ticket(confidentiality='RESTRICTED')
        self.cmd(ticket, 'start', actor=self.coordinator)
        self.cmd(ticket, 'decline', actor=self.coordinator, reason='Original confidential review completed')
        old_cycle = ticket.cycles.get(number=1)
        old_closed = old_cycle.closed_at
        self.coordinator.restricted_access = False
        self.coordinator.save(update_fields=['restricted_access'])
        self.cmd(ticket, 'reopen', actor=self.author, reason='Confidential issue requires a second review')
        self.assertEqual((ticket.status, ticket.assignee_id, ticket.queue_id, ticket.confidentiality),
                         ('REVIEW', None, restricted.pk, 'RESTRICTED'))
        self.assertTrue(ticket.queue.restricted)
        self.assertTrue(visible_tickets(self.author).filter(pk=ticket.pk).exists())
        self.assertFalse(visible_tickets(self.processor).filter(pk=ticket.pk).exists())
        self.assertFalse(visible_tickets(self.coordinator).filter(pk=ticket.pk).exists())
        old_cycle.refresh_from_db()
        self.assertEqual((old_cycle.closed_at, old_cycle.resolution_code, old_cycle.resolution_summary),
                         (old_closed, 'DECLINED', 'Original confidential review completed'))
        self.assertEqual(ticket.current_cycle.number, 2)
        self.assertTrue(ticket.events.get(kind='REOPEN').payload['awaiting_assignment'])
        self.coordinator.restricted_access = True
        self.coordinator.save(update_fields=['restricted_access'])
        with self.assertRaises(DomainError) as caught:
            self.cmd(ticket, 'approve', actor=self.coordinator, reason='Cannot approve unassigned', criteria='Confidential result',
                     plan_due_at=timezone.now()+timedelta(days=2))
        self.assertEqual(caught.exception.code, 'assignee_ineligible')
