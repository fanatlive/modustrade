"""Business commands are the sole public write API for an existing ticket.

State, append-only history, and the outbox share a transaction. PostgreSQL locks
serialize commands; the conditional version update additionally detects stale writes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import CharField, Exists, F, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .contracts import AssignmentReview, CommandName, CommandPayload
from .models import (Category, Comment, Decision, Event, IdempotencyRecord, InfoRequest,
                     Notification, OrgUnit, Outbox, ProcessingCycle, Queue, RoutingRule,
                     Ticket, TicketRelation, User, ticket_number)
from .permissions import can_coordinate, can_process, can_view_internal, has_role, visible_tickets
from .sla import add_business_hours, business_seconds, initial_deadlines, policy_snapshot, reopen_deadline, subtract_business_hours


class DomainError(Exception):
    def __init__(self, message: str, code: str = "validation", status: int = 422):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


class ConflictError(DomainError):
    def __init__(self, message="Карточка изменилась. Обновите страницу и повторите действие."):
        super().__init__(message, "conflict", 409)


# The same business matrix is consumed by the UI and enforced independently here.
COMMAND_TYPE_MATRIX: dict[str, frozenset[str]] = {
    "approve": frozenset({Ticket.Type.PROBLEM, Ticket.Type.SUGGESTION}),
    "implement": frozenset({Ticket.Type.PROBLEM, Ticket.Type.SUGGESTION}),
    "decline": frozenset({Ticket.Type.PROBLEM, Ticket.Type.SUGGESTION}),
    "acknowledge": frozenset({Ticket.Type.PRAISE}),
}


def _text(data, key, label, minimum=1, maximum=10000):
    value = str(data.get(key) or "").strip()
    if not minimum <= len(value) <= maximum:
        raise DomainError(f"{label}: укажите от {minimum} до {maximum} символов.")
    return value


def _date(value, label, *, future=False):
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = parse_datetime(str(value or ""))
        except (ValueError, TypeError, OverflowError):
            result = None
    if result is None:
        raise DomainError(f"{label}: укажите корректную дату и время.")
    if timezone.is_naive(result):
        result = timezone.make_aware(result, timezone.get_default_timezone())
    if future and result <= timezone.now():
        raise DomainError(f"{label}: срок должен быть в будущем.")
    return result


def _authorized_actor(actor):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Требуется действующая учётная запись.")


def _new_cycle(ticket: Ticket, at: datetime) -> ProcessingCycle:
    policy = policy_snapshot()
    response, decision = initial_deadlines(at, ticket.effective_priority, policy=policy)
    policy.update({"initial_first_response_due_at": response.isoformat(), "initial_decision_due_at": decision.isoformat(), "pause_used_seconds": 0})
    return ProcessingCycle.objects.create(ticket=ticket, number=ticket.cycle_no, started_at=at, first_response_due_at=response, decision_due_at=decision, policy=policy)


def eligible_assignee(user: User | None, queue: Queue, restricted: bool) -> bool:
    """Current eligibility, deliberately stricter than visibility of an old assignment."""
    return bool(user and user.is_active and has_role(user, "processor", "coordinator", "manager")
                and queue.restricted == restricted and (not restricted or user.restricted_access)
                and (queue.owner_id == user.pk or queue.members.filter(pk=user.pk).exists()))


def _require_assignee(ticket: Ticket, *, allow_empty: bool = False) -> None:
    if ticket.assignee is None and allow_empty:
        return
    if not eligible_assignee(ticket.assignee, ticket.queue, ticket.confidentiality == Ticket.Confidentiality.RESTRICTED):
        raise DomainError("Назначьте действующего исполнителя с ролью, членством и необходимым допуском.", "assignee_ineligible")


def _queue_viable(queue: Queue, restricted: bool) -> bool:
    candidates = User.objects.filter(is_active=True).filter(Q(queues=queue) | Q(pk=queue.owner_id)).distinct()
    if restricted:
        candidates = candidates.filter(restricted_access=True)
    return any(eligible_assignee(u, queue, restricted) for u in candidates)


def _route(category, request_type, affected_unit, restricted):
    rules = list(RoutingRule.objects.filter(active=True, queue__restricted=restricted)
                 .filter(Q(category=category) | Q(category__isnull=True))
                 .filter(Q(request_type=request_type) | Q(request_type=""))
                 .filter(Q(affected_unit=affected_unit) | Q(affected_unit__isnull=True))
                 .select_related("queue").order_by("-priority", "pk"))
    reason = "Правило не найдено"
    queue, rule = None, None
    if rules:
        highest = [r for r in rules if r.priority == rules[0].priority]
        if len(highest) == 1 and _queue_viable(highest[0].queue, restricted):
            rule, queue = highest[0], highest[0].queue
        else:
            reason = "Неоднозначный маршрут или недоступный обработчик"
    if queue is None:
        queue = Queue.objects.filter(code="restricted" if restricted else "default", restricted=restricted).first()
        if queue is None or not _queue_viable(queue, restricted):
            raise DomainError("Не настроена доступная резервная очередь. Обратитесь к администратору.", "routing_unavailable", 503)
    candidates = User.objects.filter(Q(queues=queue) | Q(pk=queue.owner_id),is_active=True).distinct().order_by("pk")
    if restricted:
        candidates = candidates.filter(restricted_access=True)
    candidates = [u for u in candidates if eligible_assignee(u,queue,restricted)]
    assignee = next((u for u in candidates if has_role(u, "processor")), candidates[0] if candidates else None)
    return queue, assignee, {"rule_id": rule.pk if rule else None, "rule_version": rule.version if rule else None, "fallback": rule is None, "reason": "" if rule else reason}


def _emit(ticket: Ticket, actor: User | None, kind: str, summary: str, *, visibility: str = "public", payload: dict[str, Any] | None = None) -> Event:
    cycle = ticket.current_cycle
    snapshot = {key: getattr(ticket, key) for key in ("status", "cycle_no", "category_id", "effective_priority", "request_type", "assignee_id", "queue_id", "decision", "resolution_code")}
    snapshot.update({"plan_due_at": ticket.plan_due_at.isoformat() if ticket.plan_due_at else None,
                     "first_response_due_at": cycle.first_response_due_at.isoformat() if cycle and cycle.first_response_due_at else None,
                     "decision_due_at": cycle.decision_due_at.isoformat() if cycle and cycle.decision_due_at else None,
                     "assignment_resume_status": cycle.policy.get("assignment_resume_status", "") if cycle else "",
                     "first_response_breached_before_reclassify": bool(cycle and cycle.policy.get("first_response_breached_before_reclassify")),
                     "decision_breached_before_pause": bool(cycle and cycle.policy.get("decision_breached_before_pause"))})
    event = Event.objects.create(ticket=ticket, actor=actor, kind=kind, summary=summary,
                                 visibility=visibility, payload={**(payload or {}),"cycle_no":ticket.cycle_no,"state_snapshot":snapshot}, sequence=ticket.version, created_at=timezone.now())
    recipients = {ticket.author_id, ticket.assignee_id, ticket.queue.owner_id}
    recipients.update(ticket.queue.members.filter(is_active=True).values_list("pk", flat=True))
    recipients.discard(None)
    recipients.discard(actor.pk if actor else None)
    for recipient in User.objects.filter(pk__in=recipients, is_active=True):
        if not visible_tickets(recipient).filter(pk=ticket.pk).exists():
            continue
        if visibility == "internal" and not can_view_internal(recipient, ticket):
            continue
        Outbox.objects.create(event=event, recipient=recipient, ticket=ticket,
                              message=f"{ticket.number}: {summary}")
    return event


def _number_collision(error: IntegrityError) -> bool:
    cause = error.__cause__
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", "") or ""
    return constraint.startswith("feedback_ticket_number_") or str(error) == "UNIQUE constraint failed: feedback_ticket.number"


@transaction.atomic
def create_ticket(*, actor, data: dict, idempotency_key: str) -> Ticket:
    _authorized_actor(actor)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise DomainError("Не указан корректный ключ отправки. Обновите форму.")
    # Per-author serialization makes a racing replay return the original result.
    User.objects.select_for_update().get(pk=actor.pk)
    canonical = {k: str(getattr(data.get(k), "pk", data.get(k)) or "") for k in
                 ["category", "request_type", "description", "proposed_solution", "reported_priority", "confidentiality", "affected_unit"]}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = IdempotencyRecord.objects.filter(actor=actor, key=key).select_related("ticket").first()
    if previous:
        if previous.payload_hash != fingerprint:
            raise ConflictError("Этот ключ отправки уже использован для другого содержимого.")
        return previous.ticket
    try:
        category = Category.objects.get(pk=getattr(data.get("category"), "pk", data.get("category")), active=True)
    except (Category.DoesNotExist, ValueError, TypeError):
        raise DomainError("Выберите действующую категорию.")
    request_type = data.get("request_type")
    priority = data.get("reported_priority") or Ticket.Priority.MEDIUM
    confidentiality = data.get("confidentiality") or Ticket.Confidentiality.STANDARD
    if request_type not in Ticket.Type.values or priority not in Ticket.Priority.values or confidentiality not in Ticket.Confidentiality.values:
        raise DomainError("Недопустимый тип, приоритет или уровень конфиденциальности.")
    affected = data.get("affected_unit")
    if affected:
        try:
            affected = OrgUnit.objects.get(pk=getattr(affected, "pk", affected))
        except (OrgUnit.DoesNotExist, ValueError, TypeError):
            raise DomainError("Подразделение не найдено.")
    else:
        affected = None
    queue, assignee, routing = _route(category, request_type, affected, confidentiality == Ticket.Confidentiality.RESTRICTED)
    at = timezone.now()
    fields = dict(author=actor, category=category, affected_unit=affected,
        author_unit_name=str(actor.org_unit or ""), queue=queue, assignee=assignee,
        request_type=request_type, description=_text(data,"description","Описание",10),
        proposed_solution=_text(data,"proposed_solution","Предлагаемое решение",0,5000),
        reported_priority=priority, effective_priority=priority, confidentiality=confidentiality, submitted_at=at,
        is_demo=bool(settings.DEMO_MODE), source_data={"origin":"interactive_demo"} if settings.DEMO_MODE else {})
    # A savepoint makes a rare public-number collision retryable without poisoning
    # the surrounding atomic event/outbox/idempotency transaction.
    for attempt in range(5):
        number = ticket_number()
        try:
            with transaction.atomic():
                ticket = Ticket.objects.create(number=number, **fields)
            break
        except IntegrityError as exc:
            if not _number_collision(exc):
                raise
            if attempt == 4:
                raise DomainError("Не удалось выделить номер. Повторите отправку с тем же ключом.", "number_unavailable", 503)
    _new_cycle(ticket, at)
    _emit(ticket,actor,"CREATED","Обращение зарегистрировано",payload={"routing":routing})
    IdempotencyRecord.objects.create(actor=actor,key=key,payload_hash=fingerprint,ticket=ticket)
    return ticket


def _first_response(ticket, actor):
    if actor.pk == ticket.author_id or not can_process(actor,ticket):
        return
    cycle = ticket.current_cycle
    if cycle and cycle.first_response_at is None:
        cycle.first_response_at = timezone.now()
        cycle.save(update_fields=["first_response_at"])


def _finish_pause(ticket: Ticket, info: InfoRequest, at: datetime) -> None:
    cycle = ticket.current_cycle
    if not cycle:
        return
    policy = dict(cycle.policy)
    pause = policy.get("active_pause")
    if not pause or pause.get("info_id") != info.pk:
        return
    pause_started = _date(pause.get("started_at") or info.created_at, "Начало паузы")
    actual = min(pause["allocated_seconds"], business_seconds(pause_started, min(at,info.pause_until), policy=policy))
    before = _date(pause["due_before"], "Срок решения")
    cycle.decision_due_at = add_business_hours(before,actual/3600,policy=policy)
    policy["pause_used_seconds"] = policy.get("pause_used_seconds",0) + actual
    policy.pop("active_pause",None)
    cycle.policy = policy
    cycle.save(update_fields=["decision_due_at","policy"])
    info.paused_seconds += actual


def _reserve_pause(ticket: Ticket, info: InfoRequest, at: datetime) -> None:
    cycle = ticket.current_cycle
    if info.resume_status != "REVIEW" or not cycle or cycle.decision_at or not cycle.decision_due_at:
        return
    policy = dict(cycle.policy)
    remaining = max(0, min(40, policy.get("review_pause_hours", 40))*3600-policy.get("pause_used_seconds", 0))
    info.pause_until = min(info.due_at, add_business_hours(at,remaining/3600,policy=policy))
    reserved = business_seconds(at,info.pause_until,policy=policy)
    policy["active_pause"] = {"info_id":info.pk,"due_before":cycle.decision_due_at.isoformat(),"started_at":at.isoformat(),"allocated_seconds":reserved}
    if at > cycle.decision_due_at:
        policy["decision_breached_before_pause"] = True
    cycle.decision_due_at = add_business_hours(cycle.decision_due_at,reserved/3600,policy=policy)
    cycle.policy = policy
    cycle.save(update_fields=["decision_due_at","policy"])
    info.save(update_fields=["pause_until"])


def _close(ticket, code, summary, at):
    ticket.status = Ticket.Status.CLOSED
    ticket.resolution_code, ticket.resolution_summary, ticket.closed_at = code, summary, at
    cycle = ticket.current_cycle
    if cycle:
        cycle.closed_at,cycle.resolution_code,cycle.resolution_summary = at,code,summary
        cycle.save(update_fields=["closed_at","resolution_code","resolution_summary"])
    for info in ticket.info_requests.filter(answered_at__isnull=True):
        _finish_pause(ticket,info,at)
        info.answered_at = at
        info.save(update_fields=["answered_at","paused_seconds"])


def _decision(ticket, actor, code, reason, criteria=""):
    Decision.objects.create(ticket=ticket,actor=actor,code=code,reason=reason,criteria=criteria,cycle_no=ticket.cycle_no)
    ticket.decision = code
    cycle = ticket.current_cycle
    if cycle and cycle.decision_at is None:
        cycle.decision_at = timezone.now()
        cycle.save(update_fields=["decision_at"])


def _require_state(ticket, *states):
    if ticket.status not in states:
        raise DomainError("Действие недоступно на текущем этапе обращения.", "invalid_transition")


def _resume_after_info(ticket: Ticket, info: InfoRequest, payload: dict[str, Any]) -> bool:
    """An author's answer is never contingent on staff-account availability.

    Work can resume only with an eligible owner. If nobody is available, keep the
    approval and delivery plan while returning the card to coordinator review.
    An explicit assignment later resumes that same approved implementation.
    """
    restricted = ticket.confidentiality == Ticket.Confidentiality.RESTRICTED
    before = {"queue_id":ticket.queue_id,"assignee_id":ticket.assignee_id}
    eligible = eligible_assignee(ticket.assignee,ticket.queue,restricted)
    if info.resume_status == Ticket.Status.IN_PROGRESS and not eligible:
        candidates = User.objects.filter(Q(queues=ticket.queue) | Q(pk=ticket.queue.owner_id),is_active=True).distinct().order_by("pk")
        ticket.assignee = next((candidate for candidate in candidates if eligible_assignee(candidate,ticket.queue,restricted)),None)
        eligible = ticket.assignee is not None
    elif ticket.assignee_id and not eligible:
        ticket.assignee = None
    if before["assignee_id"] != ticket.assignee_id:
        payload["assignment"] = {"before":before,"after":{"queue_id":ticket.queue_id,"assignee_id":ticket.assignee_id},"reason":"После ответа на уточнение недоступный исполнитель заменён или назначение передано координатору"}
    if info.resume_status != Ticket.Status.IN_PROGRESS or eligible:
        ticket.status = info.resume_status
        return False
    ticket.status = Ticket.Status.REVIEW
    cycle = ticket.current_cycle
    if cycle:
        cycle.policy = {**cycle.policy,"assignment_resume_status":Ticket.Status.IN_PROGRESS,"assignment_info_id":info.pk}
        cycle.save(update_fields=["policy"])
    payload.update({"awaiting_assignment":True,"resume_status":Ticket.Status.IN_PROGRESS,"info_id":info.pk})
    return True


@transaction.atomic
def execute_command(*, actor: User, ticket_id: Any, command: CommandName | str, expected_version: int, data: CommandPayload | dict[str, Any]) -> Ticket:
    _authorized_actor(actor)
    if command == "duplicate" and connection.vendor == "postgresql":
        # Serialize graph mutations before locking a ticket. An advisory lock avoids
        # unrelated foreign-key locks on queue rows and the A->B / B->A deadlock.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [628492317])
    try:
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    except (Ticket.DoesNotExist, ValueError, TypeError, ValidationError):
        raise DomainError("Обращение не найдено.","not_found",404)
    if not visible_tickets(actor).filter(pk=ticket.pk).exists():
        raise PermissionDenied("Нет доступа к этому обращению.")
    try:
        expected_version = int(expected_version)
    except (TypeError,ValueError):
        raise ConflictError()
    if ticket.version != expected_version:
        raise ConflictError()
    allowed = {"start","assign","approve","decline","acknowledge","ask_info","answer_info","resume_info","extend_info","implement","reopen","withdraw","duplicate","no_response","comment","reclassify"}
    if command not in allowed:
        raise DomainError("Неизвестное действие.")
    if command in COMMAND_TYPE_MATRIX and ticket.request_type not in COMMAND_TYPE_MATRIX[command]:
        raise DomainError("Действие не предусмотрено для этого типа обращения.", "invalid_type_command")
    if ticket.status == Ticket.Status.CLOSED and command != "reopen":
        raise DomainError("Обращение закрыто. Сначала откройте его повторно.","invalid_transition")
    processing = can_process(actor,ticket)
    if command not in {"comment","answer_info","reopen","withdraw"} and not processing:
        raise PermissionDenied("Нет права обработки обращения.")
    at, visibility, payload = timezone.now(), "public", {}
    summary = "Обращение обновлено"

    if command == "start":
        _require_state(ticket,"NEW")
        before = {"queue_id": ticket.queue_id, "assignee_id": ticket.assignee_id}
        if not eligible_assignee(ticket.assignee, ticket.queue, ticket.confidentiality == Ticket.Confidentiality.RESTRICTED):
            if not eligible_assignee(actor, ticket.queue, ticket.confidentiality == Ticket.Confidentiality.RESTRICTED):
                raise DomainError("Перед началом рассмотрения назначьте допущенного исполнителя.", "assignee_ineligible")
            ticket.assignee = actor
        _require_assignee(ticket)
        ticket.status = Ticket.Status.REVIEW
        summary = "Начато рассмотрение"
        if before["assignee_id"] != ticket.assignee_id:
            payload["assignment"] = {"before": before, "after": {"queue_id": ticket.queue_id, "assignee_id": ticket.assignee_id}, "reason": "Начавший рассмотрение назначен вместо недоступного исполнителя"}
    elif command == "assign":
        if not can_coordinate(actor,ticket):
            raise PermissionDenied("Переназначение доступно координатору.")
        reason = _text(data,"reason","Причина переназначения")
        try:
            queue = Queue.objects.get(pk=data.get("queue_id") or ticket.queue_id)
            assignee = User.objects.get(pk=data.get("assignee_id"),is_active=True) if data.get("assignee_id") else None
        except (Queue.DoesNotExist,User.DoesNotExist,ValueError,TypeError):
            raise DomainError("Выберите действующую очередь и исполнителя.")
        restricted = ticket.confidentiality == Ticket.Confidentiality.RESTRICTED
        if queue.restricted != restricted:
            raise DomainError("Уровень допуска очереди не соответствует обращению.")
        if not actor.is_superuser and not queue.members.filter(pk=actor.pk).exists() and queue.owner_id != actor.pk:
            raise PermissionDenied("Нет полномочий на целевую очередь.")
        if not _queue_viable(queue,restricted):
            raise DomainError("В очереди нет доступного обработчика.")
        if assignee and not eligible_assignee(assignee,queue,restricted):
            raise DomainError("Исполнитель не имеет допуска к выбранной очереди.")
        cycle = ticket.current_cycle
        pending_resume = bool(cycle and cycle.policy.get("assignment_resume_status") == "IN_PROGRESS")
        requires_assignee = pending_resume or ticket.status == "IN_PROGRESS" or (ticket.status == "WAITING_INFO" and ticket.info_requests.filter(answered_at__isnull=True,resume_status="IN_PROGRESS").exists())
        if assignee is None and requires_assignee:
            raise DomainError("При начатой реализации нельзя оставлять обращение без исполнителя.")
        before = {"queue_id": ticket.queue_id, "assignee_id": ticket.assignee_id}
        ticket.queue,ticket.assignee = queue,assignee
        payload = {"before": before, "after": {"queue_id": ticket.queue_id, "assignee_id": ticket.assignee_id}, "reason": reason}
        if pending_resume:
            cycle.policy = {key:value for key,value in cycle.policy.items() if key not in {"assignment_resume_status","assignment_info_id"}}
            cycle.save(update_fields=["policy"])
            if ticket.status == Ticket.Status.WAITING_INFO:
                ticket.info_requests.filter(answered_at__isnull=True).update(resume_status=Ticket.Status.IN_PROGRESS)
            else:
                ticket.status = Ticket.Status.IN_PROGRESS
            payload["implementation_resumed"] = True
        summary = "Изменено назначение обращения"
    elif command == "approve":
        _require_state(ticket,"REVIEW")
        if ticket.decision:
            raise DomainError("Решение уже принято. Назначьте действующего исполнителя для продолжения реализации.")
        reason = _text(data,"reason","Решение")
        ticket.criteria = _text(data,"criteria","Критерий готовности")
        ticket.plan_due_at = _date(data.get("plan_due_at"),"Срок выполнения",future=True)
        _require_assignee(ticket)
        _decision(ticket,actor,Decision.Code.APPROVED,reason,ticket.criteria)
        _first_response(ticket,actor)
        ticket.status = Ticket.Status.IN_PROGRESS
        summary = "Предложение одобрено и принято в работу"
    elif command == "decline":
        _require_state(ticket,"REVIEW")
        if ticket.decision:
            raise DomainError("Решение уже принято; повторное решение в этом цикле недоступно.")
        reason = _text(data,"reason","Причина отказа")
        _decision(ticket,actor,Decision.Code.DECLINED,reason)
        _first_response(ticket,actor)
        _close(ticket,Ticket.Resolution.DECLINED,reason,at)
        summary = "Рассмотрение завершено с мотивированным отказом"
    elif command == "acknowledge":
        _require_state(ticket,"REVIEW")
        if ticket.decision:
            raise DomainError("Решение уже принято; повторное решение в этом цикле недоступно.")
        if ticket.request_type != Ticket.Type.PRAISE:
            raise DomainError("Принять к сведению можно положительный отзыв.")
        reason = _text(data,"reason","Результат обработки отзыва")
        _decision(ticket,actor,Decision.Code.ACKNOWLEDGED,reason)
        _first_response(ticket,actor)
        _close(ticket,Ticket.Resolution.ACKNOWLEDGED,reason,at)
        summary = "Положительный отзыв обработан"
    elif command == "ask_info":
        _require_state(ticket,"REVIEW","IN_PROGRESS")
        cycle = ticket.current_cycle
        if cycle and cycle.policy.get("assignment_resume_status"):
            raise DomainError("Ответ автора уже сохранён. Сначала назначьте исполнителя для продолжения реализации.")
        if ticket.info_requests.filter(answered_at__isnull=True).exists():
            raise DomainError("Уже есть активный запрос уточнения.")
        question = _text(data,"question","Вопрос")
        cycle = ticket.current_cycle
        policy = cycle.policy if cycle else policy_snapshot()
        due = _date(data["due_at"],"Срок ответа",future=True) if data.get("due_at") else add_business_hours(at,40,policy=policy)
        if due > add_business_hours(at,40,policy=policy):
            raise DomainError("Срок ответа не должен превышать пять рабочих дней.")
        info = InfoRequest.objects.create(ticket=ticket,actor=actor,question=question,due_at=due,resume_status=ticket.status,created_at=at,reminder_due_at=max(at,subtract_business_hours(due,8,policy=policy)))
        _reserve_pause(ticket,info,at)
        _first_response(ticket,actor)
        ticket.status = Ticket.Status.WAITING_INFO
        summary = "Автору направлен запрос уточнения"
    elif command == "answer_info":
        _require_state(ticket,"WAITING_INFO")
        if actor.pk != ticket.author_id:
            raise PermissionDenied("Ответ на уточнение отправляет автор обращения.")
        info = ticket.info_requests.filter(answered_at__isnull=True).first()
        if info is None:
            raise DomainError("Активный запрос уточнения не найден.")
        info.response = _text({"message":data.get("message") or data.get("response")},"message","Ответ")
        info.answered_at = at
        _finish_pause(ticket,info,at)
        info.save(update_fields=["response","answered_at","paused_seconds"])
        held = _resume_after_info(ticket,info,payload)
        summary = "Ответ автора сохранён; реализация ожидает назначения исполнителя координатором" if held else "Автор ответил на запрос уточнения"
    elif command in {"resume_info", "extend_info"}:
        _require_state(ticket,"WAITING_INFO")
        if not can_coordinate(actor,ticket):
            raise PermissionDenied("Продолжение и продление уточнения доступны координатору.")
        reason = _text(data,"reason","Основание решения по уточнению")
        info = ticket.info_requests.filter(answered_at__isnull=True).first()
        if info is None:
            raise DomainError("Активный запрос уточнения не найден.")
        _finish_pause(ticket,info,at)
        payload = {"info_id": info.pk, "reason": reason}
        if command == "resume_info":
            info.answered_at = at
            info.save(update_fields=["answered_at","paused_seconds"])
            held = _resume_after_info(ticket,info,payload)
            summary = "Координатор завершил ожидание; реализация ожидает назначения исполнителя" if held else "Координатор продолжил обработку без ответа автора"
        else:
            cycle = ticket.current_cycle
            policy = cycle.policy if cycle else policy_snapshot()
            due = _date(data.get("due_at"),"Новый срок ответа",future=True)
            if due <= info.due_at or due > add_business_hours(at,40,policy=policy):
                raise DomainError("Новый срок должен быть позднее предыдущего и не более пяти рабочих дней от продления.")
            payload.update({"previous_due_at": info.due_at.isoformat(), "due_at": due.isoformat()})
            info.due_at,info.expired_notified,info.reminder_delivered_at = due,False,None
            info.reminder_generation += 1
            info.reminder_due_at = max(at,subtract_business_hours(due,8,policy=policy))
            info.save(update_fields=["due_at","expired_notified","reminder_delivered_at","reminder_generation","reminder_due_at","paused_seconds"])
            _reserve_pause(ticket,info,at)
            summary = "Координатор продлил срок ответа на уточнение"
    elif command == "implement":
        _require_state(ticket,"IN_PROGRESS")
        if ticket.decision != Decision.Code.APPROVED or not ticket.criteria.strip():
            raise DomainError("Нет одобренного критерия готовности.")
        result = _text(data,"result","Достигнутый результат")
        evidence = _text(data,"evidence","Подтверждение выполнения")
        payload = {"result": result, "evidence": evidence}
        _first_response(ticket,actor)
        _close(ticket,Ticket.Resolution.IMPLEMENTED,f"{result}\nПодтверждение: {evidence}",at)
        summary = "Реализация завершена с подтверждённым результатом"
    elif command == "comment":
        visibility = data.get("visibility") or "public"
        if visibility not in Comment.Visibility.values:
            raise DomainError("Недопустимая видимость сообщения.")
        if visibility == "internal" and not can_view_internal(actor,ticket):
            raise PermissionDenied("Нет доступа к внутренним заметкам.")
        if actor.pk != ticket.author_id and not processing:
            raise PermissionDenied("Нет права добавлять сообщение.")
        Comment.objects.create(ticket=ticket,author=actor,message=_text(data,"message","Сообщение"),visibility=visibility)
        if visibility == "public":
            _first_response(ticket,actor)
        summary = "Добавлена внутренняя заметка" if visibility == "internal" else "Добавлено сообщение"
    elif command == "reopen":
        _require_state(ticket,"CLOSED")
        previous_assignment = {"queue_id": ticket.queue_id,"assignee_id": ticket.assignee_id}
        reason = _text(data,"reason","Причина повторного открытия")
        if not can_coordinate(actor,ticket):
            old_cycle = ticket.current_cycle
            if actor.pk != ticket.author_id or not ticket.closed_at or at > reopen_deadline(ticket.closed_at,policy=old_cycle.policy if old_cycle else None):
                raise PermissionDenied("Повторное открытие доступно автору в течение пяти рабочих дней или координатору.")
        ticket.cycle_no += 1
        ticket.status = Ticket.Status.REVIEW
        ticket.resolution_code,ticket.resolution_summary,ticket.closed_at = "","",None
        ticket.decision,ticket.criteria,ticket.plan_due_at = "","",None
        if not eligible_assignee(ticket.assignee,ticket.queue,ticket.confidentiality==Ticket.Confidentiality.RESTRICTED):
            candidates = User.objects.filter(Q(queues=ticket.queue) | Q(pk=ticket.queue.owner_id),is_active=True).distinct().order_by("pk")
            replacement = next((candidate for candidate in candidates if eligible_assignee(candidate,ticket.queue,ticket.confidentiality==Ticket.Confidentiality.RESTRICTED)),None)
            ticket.assignee = replacement
        _require_assignee(ticket,allow_empty=True)
        TicketRelation.objects.filter(ticket=ticket,active=True).update(active=False,archived_at=at)
        _new_cycle(ticket,at)
        summary = "Обращение открыто повторно"
        payload = {"reason":reason,"cycle_no":ticket.cycle_no}
        if ticket.assignee_id is None:
            summary = "Обращение открыто повторно; требуется назначение исполнителя координатором"
            payload.update({"awaiting_assignment":True,"assignment_unavailable_reason":"В исходной очереди нет действующего исполнителя с необходимыми ролью и допуском"})
        if previous_assignment["assignee_id"] != ticket.assignee_id or ticket.assignee_id is None:
            assignment_reason = payload.get("assignment_unavailable_reason") or "При повторном открытии заменён недоступный исполнитель"
            payload["assignment"] = {"before":previous_assignment,"after":{"queue_id":ticket.queue_id,"assignee_id":ticket.assignee_id},"reason":assignment_reason}
    elif command == "withdraw":
        reason = _text(data,"reason","Причина отзыва")
        cycle = ticket.current_cycle
        pending_resume = bool(cycle and cycle.policy.get("assignment_resume_status") == "IN_PROGRESS")
        if actor.pk == ticket.author_id and (pending_resume or ticket.status == "IN_PROGRESS" or (ticket.status == "WAITING_INFO" and ticket.info_requests.filter(answered_at__isnull=True,resume_status="IN_PROGRESS").exists())):
            summary = "Автор запросил отзыв обращения; требуется согласование остановки работ"
            payload = {"reason":reason,"withdraw_requested":True}
        elif actor.pk == ticket.author_id:
            _close(ticket,Ticket.Resolution.WITHDRAWN,reason,at)
            summary = "Обращение отозвано автором"
        elif can_coordinate(actor,ticket) and ticket.events.filter(kind="WITHDRAW",payload__withdraw_requested=True,payload__cycle_no=ticket.cycle_no).exists():
            _close(ticket,Ticket.Resolution.WITHDRAWN,reason,at)
            summary = "Отзыв обращения согласован координатором"
        else:
            raise PermissionDenied("Отзыв инициирует автор; остановку начатых работ согласует координатор.")
    elif command == "duplicate":
        _require_state(ticket,"REVIEW")
        cycle = ticket.current_cycle
        if cycle and cycle.policy.get("assignment_resume_status"):
            raise DomainError("Сначала восстановите назначение начатой реализации; связывание дубликата на этом этапе недоступно.")
        reason = _text(data,"reason","Основание дублирования")
        try:
            canonical = visible_tickets(actor).get(pk=data.get("canonical_id"))
        except (Ticket.DoesNotExist,ValueError,TypeError,ValidationError):
            raise DomainError("Основное обращение не найдено или недоступно.")
        seen = {ticket.pk}
        current = canonical
        while current:
            if current.pk in seen:
                raise DomainError("Связь создаёт цикл дубликатов.")
            seen.add(current.pk)
            relation = TicketRelation.objects.filter(ticket=current,active=True).select_related("canonical").first()
            current = relation.canonical if relation else None
        TicketRelation.objects.create(ticket=ticket,canonical=canonical)
        # This payload is private audit metadata; public views expose only summary.
        payload = {"canonical_id":str(canonical.pk)}
        _first_response(ticket,actor)
        _close(ticket,Ticket.Resolution.DUPLICATE,reason,at)
        summary = "Обращение связано с основной карточкой как дубликат"
    elif command == "no_response":
        _require_state(ticket,"WAITING_INFO")
        if not can_coordinate(actor,ticket):
            raise PermissionDenied("Закрытие без ответа выполняет координатор.")
        info = ticket.info_requests.filter(answered_at__isnull=True).first()
        cycle = ticket.current_cycle
        policy = cycle.policy if cycle else policy_snapshot()
        if info is None or info.due_at > at or not info.expired_notified or info.reminder_delivered_at is None:
            raise DomainError("Срок ответа ещё не истёк либо напоминание автору не доставлено.")
        if at < add_business_hours(info.reminder_delivered_at,policy.get("no_response_grace_hours",8),policy=policy):
            raise DomainError("После доставки напоминания автору необходимо предоставить ещё один рабочий день для ответа.")
        reason = _text(data,"reason","Основание закрытия без ответа")
        _close(ticket,Ticket.Resolution.NO_RESPONSE,reason,at)
        summary = "Обращение закрыто из-за отсутствия необходимых сведений"
    elif command == "reclassify":
        if not can_coordinate(actor,ticket):
            raise PermissionDenied("Переклассификация доступна координатору.")
        reason = _text(data,"reason","Причина переклассификации")
        try:
            category = Category.objects.get(pk=data.get("category_id") or ticket.category_id,active=True)
        except (Category.DoesNotExist,ValueError,TypeError):
            raise DomainError("Выберите действующую категорию.")
        priority = data.get("effective_priority") or ticket.effective_priority
        request_type = data.get("request_type") or ticket.request_type
        if priority not in Ticket.Priority.values or request_type not in Ticket.Type.values:
            raise DomainError("Недопустимый приоритет или тип обращения.")
        if request_type != ticket.request_type and ticket.decision:
            raise DomainError("Тип обращения изменяется только до принятия решения.")
        before = {"category_id": ticket.category_id,"effective_priority": ticket.effective_priority,"request_type": ticket.request_type,"queue_id": ticket.queue_id,"assignee_id": ticket.assignee_id}
        restricted = ticket.confidentiality == Ticket.Confidentiality.RESTRICTED
        if category.pk == ticket.category_id and request_type == ticket.request_type:
            queue,assignee = ticket.queue,ticket.assignee
            routing = {"kept":True,"reason":"Условия маршрутизации не изменились"}
        else:
            queue,assignee,routing = _route(category,request_type,ticket.affected_unit,restricted)
            if queue.pk == ticket.queue_id and eligible_assignee(ticket.assignee,queue,restricted):
                assignee = ticket.assignee
        pending_cycle = ticket.current_cycle
        if pending_cycle and pending_cycle.policy.get("assignment_resume_status"):
            # Rerouting a held implementation does not silently accept ownership.
            # Keep the explicit coordinator assignment as the resumption command.
            assignee = None
        if ticket.status == "IN_PROGRESS" or (ticket.status == "WAITING_INFO" and ticket.decision == "APPROVED"):
            if not eligible_assignee(assignee,queue,restricted):
                raise DomainError("Целевой маршрут должен иметь допущенного исполнителя для начатой реализации.")
        cycle = ticket.current_cycle
        active_info = ticket.info_requests.filter(answered_at__isnull=True).first()
        if active_info:
            _finish_pause(ticket,active_info,at)
        ticket.category,ticket.effective_priority,ticket.request_type = category,priority,request_type
        ticket.queue,ticket.assignee = queue,assignee
        if cycle and cycle.started_at:
            cycle.refresh_from_db()
            policy = dict(cycle.policy)
            previous_deadlines = {"first_response_due_at": cycle.first_response_due_at.isoformat() if cycle.first_response_due_at else None,"decision_due_at": cycle.decision_due_at.isoformat() if cycle.decision_due_at else None}
            if not cycle.decision_at and cycle.decision_due_at and at > cycle.decision_due_at:
                policy["decision_breached_before_pause"] = True
            if not cycle.first_response_at and cycle.first_response_due_at and at > cycle.first_response_due_at:
                policy["first_response_breached_before_reclassify"] = True
            response_due,decision_due = initial_deadlines(cycle.started_at,priority,policy=policy)
            if cycle.first_response_at is None:
                cycle.first_response_due_at = response_due
            if cycle.decision_at is None:
                cycle.decision_due_at = add_business_hours(decision_due,policy.get("pause_used_seconds",0)/3600,policy=policy)
            policy.setdefault("reclassifications",[]).append({"at": at.isoformat(),"priority": priority,"previous_deadlines": previous_deadlines})
            cycle.policy = policy
            cycle.save(update_fields=["first_response_due_at","decision_due_at","policy"])
            if active_info:
                active_info.save(update_fields=["paused_seconds"])
                _reserve_pause(ticket,active_info,at)
        payload = {"before": before,"after": {"category_id": ticket.category_id,"effective_priority": priority,"request_type": request_type,"queue_id": ticket.queue_id,"assignee_id": ticket.assignee_id},"reason": reason,"routing": routing}
        summary = "Изменены категория, рабочий приоритет или тип обращения"

    old_version = ticket.version
    ticket.version += 1
    ticket.updated_at = at
    values = {field.attname:getattr(ticket,field.attname) for field in Ticket._meta.concrete_fields if not field.primary_key and not field.generated}
    if Ticket.objects.filter(pk=ticket.pk,version=old_version).update(**values) != 1:
        raise ConflictError()
    _emit(ticket,actor,command.upper(),summary,visibility=visibility,payload=payload)
    return ticket


def _remind_info_requests(limit: int = 100) -> None:
    """A late worker still owes the author a reminder and a delivery grace window."""
    now = timezone.now()
    # Materializing this instant at ask/extend time makes the worker independent
    # of the number of future or previously reminded questions and their calendars.
    candidates = list(InfoRequest.objects.filter(ticket__status="WAITING_INFO",answered_at__isnull=True,reminder_due_at__lte=now)
                      .order_by("reminder_due_at").values_list("pk","ticket_id")[:limit])
    for info_id,ticket_id in candidates:
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
            info = InfoRequest.objects.select_for_update().get(pk=info_id)
            at = timezone.now()
            if ticket.status != "WAITING_INFO" or info.answered_at or info.reminder_due_at is None or info.reminder_due_at > at:
                continue
            ticket.version += 1
            ticket.save(update_fields=["version","updated_at"])
            summary = "Напоминание: ответьте на уточнение; после доставки предоставляется ещё один рабочий день" if info.due_at <= at else "До срока ответа на уточнение осталось не более рабочего дня"
            _emit(ticket,None,"INFO_REMINDER",summary,payload={"info_id":info.pk,"reminder_generation":info.reminder_generation,"late":info.due_at <= at})
            info.reminder_due_at = None
            info.save(update_fields=["reminder_due_at"])


def _expire_info_requests(limit=100):
    for info_id,ticket_id in InfoRequest.objects.filter(answered_at__isnull=True,expired_notified=False,due_at__lte=timezone.now()).values_list("pk","ticket_id")[:limit]:
        with transaction.atomic():
            # Keep the same lock ordering as interactive commands: ticket then question.
            ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
            info = InfoRequest.objects.select_for_update().get(pk=info_id)
            if info.expired_notified or info.answered_at:
                continue
            _finish_pause(ticket,info,timezone.now())
            info.expired_notified = True
            info.save(update_fields=["expired_notified","paused_seconds"])
            ticket.version += 1
            ticket.save(update_fields=["version","updated_at"])
            _emit(ticket,None,"INFO_EXPIRED","Истёк срок уточнения; обращение передано на контроль координатору")


def _deadline_is_due(ticket, kind, now):
    cycle = ticket.current_cycle
    if ticket.status == Ticket.Status.CLOSED or cycle is None:
        return False
    if kind == "FIRST_RESPONSE_OVERDUE":
        return bool(cycle.first_response_due_at and not cycle.first_response_at and (cycle.first_response_due_at < now or cycle.policy.get("first_response_breached_before_reclassify")))
    if kind == "DECISION_OVERDUE":
        return bool(cycle.decision_due_at and not cycle.decision_at and (cycle.decision_due_at < now or cycle.policy.get("decision_breached_before_pause")))
    return bool(ticket.decision == "APPROVED" and ticket.status in ("IN_PROGRESS","WAITING_INFO","REVIEW") and ticket.plan_due_at and ticket.plan_due_at < now)


def scan_overdue(limit=100):
    """One alert per deadline kind and cycle, serialized with user commands."""
    now, emitted = timezone.now(), 0
    phases = [
        ("FIRST_RESPONSE_OVERDUE", "Нарушен срок первого ответа", Q(cycles__number=F("cycle_no"),cycles__first_response_at__isnull=True) & (Q(cycles__first_response_due_at__lt=now) | Q(cycles__policy__first_response_breached_before_reclassify=True))),
        ("DECISION_OVERDUE", "Нарушен срок принятия решения", Q(cycles__number=F("cycle_no"),cycles__decision_at__isnull=True) & (Q(cycles__decision_due_at__lt=now) | Q(cycles__policy__decision_breached_before_pause=True))),
        ("IMPLEMENTATION_OVERDUE", "Нарушен плановый срок реализации", Q(decision="APPROVED",status__in=["IN_PROGRESS","WAITING_INFO","REVIEW"],plan_due_at__lt=now)),
    ]
    for kind,summary,condition in phases:
        if emitted >= limit:
            break
        prior = Event.objects.annotate(alert_cycle=Cast(KeyTextTransform("cycle_no","payload"),CharField())).filter(ticket_id=OuterRef("pk"),kind=kind,alert_cycle=Cast(OuterRef("cycle_no"),CharField()))
        candidates = Ticket.objects.exclude(status="CLOSED").filter(condition).annotate(deadline_alerted=Exists(prior)).filter(deadline_alerted=False).order_by("created_at").values_list("pk",flat=True)[:limit-emitted]
        for ticket_id in list(candidates):
            with transaction.atomic():
                ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
                if not _deadline_is_due(ticket,kind,timezone.now()) or ticket.events.filter(kind=kind,payload__cycle_no=ticket.cycle_no).exists():
                    continue
                ticket.version += 1
                ticket.save(update_fields=["version","updated_at"])
                _emit(ticket,None,kind,summary,payload={"cycle_no":ticket.cycle_no})
                emitted += 1
    return emitted


def process_outbox(limit: int = 100) -> dict[str, int]:
    _remind_info_requests(limit)
    _expire_info_requests(limit)
    scan_overdue(limit)
    result = {"sent":0,"failed":0,"skipped":0}
    for _ in range(limit):
        now = timezone.now()
        with transaction.atomic():
            candidates = Outbox.objects.filter(Q(state="pending",next_attempt_at__lte=now) | Q(state="processing",locked_until__lt=now)).order_by("pk")
            if connection.features.has_select_for_update_skip_locked:
                candidates = candidates.select_for_update(skip_locked=True)
            else:
                candidates = candidates.select_for_update()
            job = candidates.first()
            if job is None:
                break
            job.state,job.locked_until,job.attempts = "processing",now+timedelta(minutes=2),job.attempts+1
            job.save(update_fields=["state","locked_until","attempts"])
        try:
            with transaction.atomic():
                job = Outbox.objects.select_related("recipient","ticket","ticket__queue","event").get(pk=job.pk)
                permitted = visible_tickets(job.recipient).filter(pk=job.ticket_id).exists()
                permitted = permitted and (job.event.visibility != "internal" or can_view_internal(job.recipient,job.ticket))
                if permitted and job.event.kind == "INFO_REMINDER":
                    permitted = InfoRequest.objects.filter(pk=job.event.payload.get("info_id"),ticket=job.ticket,
                        answered_at__isnull=True,reminder_generation=job.event.payload.get("reminder_generation",1)).exists()
                if permitted:
                    notification, _ = Notification.objects.get_or_create(outbox=job,defaults={"user":job.recipient,"ticket":job.ticket,"message":job.message,"created_at":timezone.now()})
                    if job.event.kind == "INFO_REMINDER" and job.recipient_id == job.ticket.author_id:
                        generation = job.event.payload.get("reminder_generation",1)
                        InfoRequest.objects.filter(pk=job.event.payload.get("info_id"),ticket=job.ticket,answered_at__isnull=True,reminder_generation=generation,reminder_delivered_at__isnull=True).update(reminder_delivered_at=notification.created_at)
                    result["sent"] += 1
                else:
                    result["skipped"] += 1
                job.state,job.locked_until,job.last_error = "sent",None,""
                job.save(update_fields=["state","locked_until","last_error"])
        except Exception as exc:
            # Do not log sensitive payloads or exception text supplied by external systems.
            delay = min(3600,2**min(job.attempts,10)*5)
            Outbox.objects.filter(pk=job.pk).update(state="failed" if job.attempts>=8 else "pending",locked_until=None,next_attempt_at=timezone.now()+timedelta(seconds=delay),last_error=type(exc).__name__[:500])
            result["failed"] += 1
    return result


@transaction.atomic
def retry_delivery(*, actor: User, outbox_id: Any, reason: str) -> Outbox:
    """Retry the same delivery identity; an existing Notification remains unique."""
    _authorized_actor(actor)
    explanation = _text({"reason":reason},"reason","Причина повторной доставки")
    try:
        job = Outbox.objects.select_related("ticket","event").get(pk=outbox_id)
    except (Outbox.DoesNotExist,ValueError,TypeError):
        raise DomainError("Доставка не найдена.","not_found",404)
    ticket = Ticket.objects.select_for_update().get(pk=job.ticket_id)
    job = Outbox.objects.select_for_update().get(pk=job.pk)
    if not can_coordinate(actor,ticket) or (job.event.visibility == "internal" and not can_view_internal(actor,ticket)):
        raise PermissionDenied("Нет полномочий на восстановление этой доставки.")
    if job.state != "failed":
        raise DomainError("Повтор доступен только для доставки в очереди ошибок.")
    previous_attempts, previous_error = job.attempts,job.last_error
    job.state,job.attempts,job.next_attempt_at,job.locked_until,job.last_error = "pending",0,timezone.now(),None,""
    job.save(update_fields=["state","attempts","next_attempt_at","locked_until","last_error"])
    ticket.version += 1
    ticket.save(update_fields=["version","updated_at"])
    _emit(ticket,actor,"DELIVERY_RETRY","Координатор назначил повтор доставки уведомления",visibility="internal",payload={"outbox_id":job.pk,"reason":explanation,"previous_attempts":previous_attempts,"previous_error":previous_error})
    return job


def _configuration_actor(actor: User, queue: Queue) -> None:
    _authorized_actor(actor)
    if not (actor.is_superuser or has_role(actor,"admin")):
        raise PermissionDenied("Настройка маршрутов доступна администратору.")
    if queue.restricted and not (actor.is_superuser or actor.restricted_access):
        raise PermissionDenied("Требуется допуск к конфиденциальным маршрутам.")


@transaction.atomic
def revise_routing_rule(*, actor: User, rule_id: Any, data: dict[str, Any]) -> RoutingRule:
    from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
    previous = RoutingRule.objects.select_for_update().select_related("queue").get(pk=rule_id)
    _configuration_actor(actor,previous.queue)
    if not previous.active or RoutingRule.objects.filter(supersedes=previous).exists():
        raise ConflictError("Правило уже заменено. Откройте действующую версию.")
    fields = {field: data.get(field,getattr(previous,field)) for field in ("category_id","request_type","affected_unit_id","queue_id","priority")}
    successor = RoutingRule(**fields,version=previous.version+1,supersedes=previous,active=bool(data.get("active",True)))
    _configuration_actor(actor,successor.queue)
    previous.active = False
    previous.save(update_fields=["active"])
    try:
        successor.full_clean()
        successor.save()
    except ValidationError as exc:
        raise DomainError("; ".join(exc.messages)) from exc
    LogEntry.objects.log_actions(user_id=actor.pk,queryset=RoutingRule.objects.filter(pk=previous.pk),action_flag=CHANGE,change_message=f"Заменено версией {successor.version}, правило {successor.pk}")
    LogEntry.objects.log_actions(user_id=actor.pk,queryset=RoutingRule.objects.filter(pk=successor.pk),action_flag=ADDITION,change_message=f"Новая версия правила {previous.pk}")
    return successor


@transaction.atomic
def disable_routing_rule(*, actor: User, rule_id: Any) -> RoutingRule:
    from django.contrib.admin.models import CHANGE, LogEntry
    rule = RoutingRule.objects.select_for_update().select_related("queue").get(pk=rule_id)
    _configuration_actor(actor,rule.queue)
    rule.active = False
    rule.save(update_fields=["active"])
    LogEntry.objects.log_actions(user_id=actor.pk,queryset=RoutingRule.objects.filter(pk=rule.pk),action_flag=CHANGE,change_message="Маршрут деактивирован; условия версии сохранены")
    return rule


def reconcile_assignments(*, actor: User, apply: bool = False, limit: int = 100) -> list[AssignmentReview]:
    """Dry-run by default; each actual reassignment uses the ordinary command lock."""
    _authorized_actor(actor)
    if not can_coordinate(actor):
        raise PermissionDenied("Передача назначений доступна координатору.")
    results: list[AssignmentReview] = []
    candidates = visible_tickets(actor).exclude(status="CLOSED").select_related("assignee","queue").order_by("created_at")
    for ticket in candidates.iterator(chunk_size=100):
        restricted = ticket.confidentiality == Ticket.Confidentiality.RESTRICTED
        if eligible_assignee(ticket.assignee,ticket.queue,restricted) or not can_coordinate(actor,ticket):
            continue
        users = User.objects.filter(Q(queues=ticket.queue) | Q(pk=ticket.queue.owner_id),is_active=True).distinct().order_by("pk")
        replacement = next((user for user in users if eligible_assignee(user,ticket.queue,restricted)),None)
        row: AssignmentReview = {"ticket_id":str(ticket.pk),"number":ticket.number,"previous_assignee_id":ticket.assignee_id,"assignee_id":replacement.pk if replacement else None,"status":"ready" if replacement else "no_eligible_assignee"}
        if apply and replacement:
            try:
                execute_command(actor=actor,ticket_id=ticket.pk,command="assign",expected_version=ticket.version,data={"assignee_id":replacement.pk,"queue_id":ticket.queue_id,"reason":"Передача открытого обращения после изменения доступности учётной записи"})
                row["status"] = "reassigned"
            except (DomainError,PermissionDenied) as exc:
                row["status"] = getattr(exc,"code","forbidden")
        results.append(row)
        if len(results) >= max(1,min(limit,10000)):
            break
    return results
