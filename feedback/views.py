"""HTML endpoints. Business mutations are delegated to transactional commands."""
from __future__ import annotations

import math

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Aggregate, Avg, BigIntegerField, BooleanField, Case, CharField, Count, DateTimeField, DurationField, ExpressionWrapper, F, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, NullIf
from django.db.models.fields.json import KeyTextTransform
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import AnalyticsFilterForm, CategoryForm, CommandForm, CreateTicketForm, PRIORITIES, QueueForm, RetryDeliveryForm, RoutingRuleForm, STATUSES, TYPES, TicketFilterForm
from .models import Category, Event, InfoRequest, Notification, OrgUnit, Outbox, ProcessingCycle, Queue, RoutingRule, Ticket, User
from .permissions import can_coordinate, can_process, can_view_internal, has_role, operational_users, staff_scope, visible_notifications, visible_tickets
from .services import COMMAND_TYPE_MATRIX, DomainError, create_ticket, execute_command
from .search import scoped_search, similar_tickets


def _overdue_q() -> Q:
    now = timezone.now()
    first = Q(first_response_at__isnull=True) & (Q(first_response_due_at__lt=now) | Q(policy__first_response_breached_before_reclassify=True))
    decision = Q(decision_at__isnull=True) & (Q(decision_due_at__lt=now) | Q(policy__decision_breached_before_pause=True))
    due_cycles = ProcessingCycle.objects.filter(number=F("ticket__cycle_no")).filter(first | decision).order_by().values("ticket_id")
    implementation = Q(status__in=["REVIEW", "IN_PROGRESS", "WAITING_INFO"], decision="APPROVED", plan_due_at__lt=now)
    return ~Q(status="CLOSED") & (Q(pk__in=due_cycles) | implementation)


def _ticket_list(request: HttpRequest, *, queue_only: bool = False) -> HttpResponse:
    base = visible_tickets(request.user)
    if queue_only:
        if not (request.user.is_superuser or has_role(request.user, "processor", "coordinator", "manager")):
            raise PermissionDenied
        base = base.filter(staff_scope(request.user))
    tickets = base.select_related("author", "category", "queue", "assignee", "affected_unit").prefetch_related("cycles")
    filters = TicketFilterForm(request.GET or None, user=request.user)
    active_filter = False
    if filters.is_bound and filters.is_valid():
        values = filters.cleaned_data
        active_filter = any(values.values())
        if values["q"]:
            tickets = scoped_search(tickets, values["q"])
        for key, field in (("status", "status"), ("request_type", "request_type"), ("priority", "effective_priority"), ("category", "category"), ("unit", "affected_unit"), ("author", "author"), ("assignee", "assignee"), ("queue", "queue")):
            if values.get(key):
                tickets = tickets.filter(**{field: values[key]})
        if values.get("date_from"):
            tickets = tickets.filter(submitted_at__date__gte=values["date_from"])
        if values.get("date_to"):
            tickets = tickets.filter(submitted_at__date__lte=values["date_to"])
        if values.get("overdue"):
            tickets = tickets.filter(_overdue_q())
    summary = base.aggregate(total=Count("pk"), active=Count("pk", filter=~Q(status="CLOSED")),
        waiting=Count("pk", filter=Q(status="WAITING_INFO")), closed=Count("pk", filter=Q(status="CLOSED")),
        overdue=Count("pk", filter=_overdue_q()))
    paginator = Paginator(tickets.order_by("-created_at", "number"), 15)
    if not active_filter:
        paginator.count = summary["total"]
    page = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "feedback/list.html", {"page_obj": page, "filters": filters, "summary": summary, "queue_only": queue_only, "querystring": params.urlencode(), "active_nav": "queue" if queue_only else "list"})


@login_required
def ticket_list(request):
    return _ticket_list(request)


@login_required
def work_queue(request):
    return _ticket_list(request, queue_only=True)


@login_required
def ticket_create(request):
    form = CreateTicketForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            key = str(data.pop("idempotency_key"))
            try:
                ticket = create_ticket(actor=request.user, data=data, idempotency_key=key)
            except DomainError as exc:
                form.add_error(None, str(exc))
                status = getattr(exc, "status", 422)
            else:
                messages.success(request, f"Обращение {ticket.number} зарегистрировано.")
                return redirect("feedback:detail", pk=ticket.pk)
        else:
            status = 422
    return render(request, "feedback/create.html", {"form": form, "active_nav": "create"}, status=status)


def _history_navigation(request, page, parameter, anchor, label, ticket_id):
    """Each independent cursor keeps the other history panels' page choices."""
    def url(number):
        params = request.GET.copy()
        params[parameter] = number
        return reverse("feedback:detail", args=[ticket_id]) + "?" + params.urlencode() + "#" + anchor
    return {"page": page.number, "pages": page.paginator.num_pages, "label": label,
        "previous": url(page.previous_page_number()) if page.has_previous() else None,
        "next": url(page.next_page_number()) if page.has_next() else None}


def _detail(request, pk, *, error=None, status=200, command_form=None):
    ticket = get_object_or_404(visible_tickets(request.user).select_related("author", "category", "queue", "assignee", "affected_unit"), pk=pk)
    internal = can_view_internal(request.user, ticket)
    comments = ticket.comments.select_related("author")
    events = ticket.events.select_related("actor")
    if not internal:
        comments = comments.filter(visibility="public")
        events = events.filter(visibility="public")
    available_queues = Queue.objects.all() if request.user.is_superuser else Queue.objects.filter(Q(owner=request.user) | Q(members=request.user)).distinct()
    if not request.user.restricted_access and not request.user.is_superuser:
        available_queues = available_queues.filter(restricted=False)
    if ticket.confidentiality == "RESTRICTED":
        available_queues = available_queues.filter(restricted=True)
    assignees = operational_users().filter(Q(queues__in=available_queues) | Q(owned_queues__in=available_queues)).distinct().order_by("first_name", "username")
    if ticket.confidentiality == "RESTRICTED":
        assignees = assignees.filter(restricted_access=True)
    comments_page = Paginator(comments.order_by("-created_at", "-pk"), 50).get_page(request.GET.get("comments_page"))
    events_page = Paginator(events.order_by("-sequence"), 50).get_page(request.GET.get("events_page"))
    cycles_page = Paginator(ticket.cycles.order_by("-number"), 10).get_page(request.GET.get("cycles_page"))
    info_page = Paginator(ticket.info_requests.filter(answered_at__isnull=False).select_related("actor").order_by("-created_at", "-pk"), 25).get_page(request.GET.get("info_page"))
    cycles = list(cycles_page.object_list)
    cycle_numbers = [cycle.number for cycle in cycles]
    current_cycle = next((cycle for cycle in cycles if cycle.number == ticket.cycle_no), None) or ticket.current_cycle
    decisions = list(ticket.decisions.filter(cycle_no=ticket.cycle_no).select_related("actor").order_by("created_at"))
    cycle_decisions = list(ticket.decisions.filter(cycle_no__in=cycle_numbers).select_related("actor").order_by("created_at"))
    implementations = events.filter(kind="IMPLEMENT", payload__cycle_no__in=cycle_numbers)
    latest_implementation = implementations.order_by().values("payload__cycle_no").annotate(last=Max("sequence")).values("last")
    history_results = {event.payload["cycle_no"]: event for event in implementations.filter(sequence__in=Subquery(latest_implementation))}
    decisions_by_cycle = {}
    for decision in cycle_decisions:
        decisions_by_cycle.setdefault(decision.cycle_no, []).append(decision)
    for cycle in cycles:
        cycle.history_decisions = decisions_by_cycle.get(cycle.number, [])
        cycle.history_results = [history_results[cycle.number]] if cycle.number in history_results else []
    pending_withdraw = ticket.events.filter(kind="WITHDRAW", payload__withdraw_requested=True, payload__cycle_no=ticket.cycle_no).order_by("-sequence").first() if can_coordinate(request.user, ticket) and ticket.status != "CLOSED" else None
    candidates = visible_tickets(request.user).exclude(pk=ticket.pk)
    selected_canonical = command_form.cleaned_data.get("canonical_id") if command_form and hasattr(command_form, "cleaned_data") else None
    if selected_canonical:
        candidates = candidates.order_by(Case(When(pk=selected_canonical, then=Value(0)), default=Value(1)), "number")
    else:
        candidates = candidates.order_by("number")
    canonical_page = Paginator(candidates, 25).get_page(request.GET.get("canonical_page"))
    return render(request, "feedback/detail.html", {
        "ticket": ticket, "comments": comments, "events": events.order_by("-sequence"),
        "comments_page": comments_page, "events_page": events_page, "cycles_page": cycles_page, "info_page": info_page,
        "comments_navigation": _history_navigation(request, comments_page, "comments_page", "conversation", "Страницы обсуждения", ticket.pk),
        "events_navigation": _history_navigation(request, events_page, "events_page", "event-history", "Страницы событий", ticket.pk),
        "cycles_navigation": _history_navigation(request, cycles_page, "cycles_page", "cycle-history", "Страницы циклов", ticket.pk),
        "info_navigation": _history_navigation(request, info_page, "info_page", "info-history", "Страницы уточнений", ticket.pk),
        "detail_querystring": request.GET.urlencode(),
        "decisions": decisions, "cycles": cycles, "cycle": current_cycle,
        "info_request": ticket.info_requests.filter(answered_at__isnull=True).first(),
        "info_history": info_page.object_list,
        "can_process_ticket": can_process(request.user, ticket), "can_coordinate_ticket": can_coordinate(request.user, ticket),
        "type_commands": {name for name, types in COMMAND_TYPE_MATRIX.items() if ticket.request_type in types and not (ticket.status == "REVIEW" and ticket.decision == "APPROVED" and name in {"approve", "decline", "acknowledge"})},
        "awaiting_reassignment": bool(current_cycle and current_cycle.policy.get("assignment_resume_status") == "IN_PROGRESS"),
        "can_view_internal": internal, "is_author": ticket.author_id == request.user.pk,
        "assignees": assignees if internal else User.objects.none(),
        "available_queues": available_queues if internal else Queue.objects.none(),
        "canonical_candidates": canonical_page if internal else [], "pending_withdraw": pending_withdraw,
        "categories": Category.objects.filter(active=True).order_by("name") if internal else [], "priorities": PRIORITIES,
        "private_draft_without_access": bool(command_form and command_form["command"].value() == "comment" and command_form["visibility"].value() == "internal" and not internal),
        "command_form": command_form, "command_error": error, "active_nav": "list",
    }, status=status)


@login_required
def ticket_detail(request, pk):
    return _detail(request, pk)


@login_required
@require_POST
def ticket_command(request, pk):
    get_object_or_404(visible_tickets(request.user), pk=pk)
    form = CommandForm(request.POST)
    if not form.is_valid():
        return _detail(request, pk, error="Проверьте заполненные поля. Даты указываются в часовом поясе рабочего календаря.", status=422, command_form=form)
    data = dict(form.cleaned_data)
    command, version = data.pop("command"), data.pop("version")
    # Omit empty optional values so domain defaults (e.g. five-day wait) apply.
    data = {key: value for key, value in data.items() if value is not None and value != ""}
    if command == "answer_info" and data.get("response"):
        data.setdefault("message", data["response"])
    try:
        execute_command(actor=request.user, ticket_id=pk, command=command, expected_version=version, data=data)
    except DomainError as exc:
        return _detail(request, pk, error=str(exc), status=getattr(exc, "status", 422), command_form=form)
    messages.success(request, "Изменения сохранены.")
    target = reverse("feedback:detail", args=[pk])
    return redirect(target + ("?" + request.GET.urlencode() if request.GET else ""))


def _duration_label(seconds):
    if seconds is None:
        return "Нет данных"
    hours = seconds / 3600
    if seconds < 60:
        return "< 1 мин"
    return f"{hours:.1f} ч" if hours >= 1 else f"{round(seconds / 60)} мин"


class _Percentile(Aggregate):
    template = "PERCENTILE_CONT(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"
    output_field = DurationField()

    def __init__(self, expression, percentile):
        super().__init__(expression, percentile=percentile)


def _response_statistics(cycles, as_of):
    answers = cycles.filter(first_response_at__lte=as_of, first_response_at__gte=F("started_at")).annotate(
        elapsed=ExpressionWrapper(F("first_response_at") - F("started_at"), output_field=DurationField()))
    count = answers.count()
    if not count:
        return 0, None, None, None
    if connection.vendor == "postgresql":
        result = answers.aggregate(average=Avg("elapsed"), median=_Percentile("elapsed", .5), p95=_Percentile("elapsed", .95))
        return count, *(result[key].total_seconds() for key in ("average", "median", "p95"))
    # SQLite development fallback: sorting and offsets happen in SQL; at most
    # three durations enter Python, regardless of the number of answers.
    average = answers.aggregate(value=Avg("elapsed"))["value"].total_seconds()
    ordered = answers.order_by("elapsed", "pk").values_list("elapsed", flat=True)
    middle = [ordered[(count - 1) // 2], ordered[count // 2]]
    return count, average, sum(value.total_seconds() for value in middle) / 2, ordered[math.ceil(count * .95) - 1].total_seconds()


def _analytics_snapshot(tickets, as_of):
    """Use saved event states; reconstruct legacy transitions where available."""
    cycle = ProcessingCycle.objects.filter(ticket_id=OuterRef("pk"), started_at__lte=as_of).order_by("-number")
    snapshot = Event.objects.filter(ticket_id=OuterRef("pk"), created_at__lte=as_of, payload__state_snapshot__status__isnull=False).order_by("-sequence")
    transitions = Event.objects.filter(ticket_id=OuterRef("pk"), created_at__lte=as_of,
        kind__in=["START", "APPROVE", "DECLINE", "ACKNOWLEDGE", "ASK_INFO", "ANSWER_INFO", "RESUME_INFO", "IMPLEMENT", "REOPEN", "WITHDRAW", "DUPLICATE", "NO_RESPONSE"]).filter(
            Q(payload__withdraw_requested__isnull=True) | ~Q(payload__withdraw_requested=True)).order_by("-sequence")
    answer_cycle = ProcessingCycle.objects.filter(ticket_id=OuterRef("ticket_id"), started_at__lte=as_of).order_by("-number")
    answered = InfoRequest.objects.filter(ticket_id=OuterRef("pk"), answered_at__lte=as_of,
        created_at__gte=Subquery(answer_cycle.values("started_at")[:1])).order_by("-answered_at", "-pk")
    def saved_value(key):
        value = Subquery(snapshot.annotate(value=KeyTextTransform(key, KeyTextTransform("state_snapshot", "payload"))).values("value")[:1])
        # SQLite JSON_TYPE represents JSON null as the literal string "null".
        # Normalize it before numeric casts; otherwise null assignee becomes 0.
        return NullIf(value, Value("null"), output_field=CharField())
    tickets = tickets.annotate(metric_cycle=Subquery(cycle.values("number")[:1]),
        _closed_at=Subquery(cycle.values("closed_at")[:1]), _resolution=Subquery(cycle.values("resolution_code")[:1]),
        _transition=Subquery(transitions.values("kind")[:1]), _resume_status=Subquery(answered.values("resume_status")[:1]), _snapshot_id=Subquery(snapshot.values("pk")[:1]),
        _state=saved_value("status"), _snapshot_resolution=saved_value("resolution_code"))
    fallback = Case(When(_closed_at__lte=as_of, then=Value("CLOSED")),
        When(_transition="ASK_INFO", then=Value("WAITING_INFO")), When(_transition="APPROVE", then=Value("IN_PROGRESS")),
        When(_transition__in=["START", "REOPEN"], then=Value("REVIEW")),
        When(_transition__in=["ANSWER_INFO", "RESUME_INFO"], then=Coalesce("_resume_status", Value("UNKNOWN"))), default=Value("NEW"), output_field=CharField())
    return tickets.annotate(metric_status=Coalesce("_state", fallback, output_field=CharField()), metric_resolution=Coalesce("_snapshot_resolution", "_resolution", Value(""), output_field=CharField()),
        metric_type=Coalesce(saved_value("request_type"), F("request_type"), output_field=CharField()),
        metric_priority=Coalesce(saved_value("effective_priority"), F("effective_priority"), output_field=CharField()),
        metric_decision=Coalesce(saved_value("decision"), F("decision"), output_field=CharField()),
        metric_category=Coalesce(Cast(saved_value("category_id"), BigIntegerField()), F("category_id"), output_field=BigIntegerField()),
        metric_assignee=Case(When(_snapshot_id__isnull=False, then=Cast(saved_value("assignee_id"), BigIntegerField())), default=F("assignee_id"), output_field=BigIntegerField()),
        metric_plan_due=Case(When(_snapshot_id__isnull=False, then=Cast(saved_value("plan_due_at"), DateTimeField())), default=F("plan_due_at")))


@login_required
def analytics(request):
    default_mode = "demo" if getattr(settings, "DEMO_MODE", settings.DEBUG) else "production"
    mode = request.GET.get("mode", default_mode)
    if mode not in {"demo", "production"}:
        mode = default_mode
    filters = AnalyticsFilterForm(request.GET or None)
    valid = not filters.is_bound or filters.is_valid()
    values = filters.cleaned_data if filters.is_bound and valid else {}
    as_of = values.get("as_of") or timezone.now()
    historical = bool(values.get("as_of"))
    # Authorization uses semi-joins, so each ticket occurs once without a sort.
    source = visible_tickets(request.user).filter(is_demo=mode == "demo")
    unknown_source_count = source.filter(submitted_at__isnull=True).count()
    tickets = source
    if not valid:
        tickets = tickets.none()
    if values.get("date_from"):
        tickets = tickets.filter(submitted_at__date__gte=values["date_from"])
    if values.get("date_to"):
        tickets = tickets.filter(submitted_at__date__lte=values["date_to"])
    if historical:
        tickets = _analytics_snapshot(tickets.filter(submitted_at__lte=as_of), as_of)
    else:
        tickets = tickets.annotate(metric_status=F("status"), metric_resolution=F("resolution_code"), metric_cycle=F("cycle_no"),
            metric_type=F("request_type"), metric_priority=F("effective_priority"), metric_category=F("category_id"), metric_assignee=F("assignee_id"))
    total = tickets.count()
    counts = dict(tickets.order_by().values_list("metric_status").annotate(n=Count("id")).order_by("metric_status"))
    types = dict(tickets.order_by().values_list("metric_type").annotate(n=Count("id")).order_by("metric_type"))
    priorities = dict(tickets.order_by().values_list("metric_priority").annotate(n=Count("id")).order_by("metric_priority"))
    outcomes = list(tickets.filter(metric_status="CLOSED").order_by().values("metric_resolution").annotate(n=Count("id")).order_by("-n", "metric_resolution"))
    labels = dict(Ticket.Resolution.choices)
    for item in outcomes:
        item["label"] = labels.get(item["metric_resolution"], item["metric_resolution"])
    cycle_qs = ProcessingCycle.objects.filter(ticket__in=tickets)
    if historical:
        latest = ProcessingCycle.objects.filter(ticket_id=OuterRef("ticket_id"), started_at__lte=as_of).order_by("-number")
        cycle_qs = cycle_qs.filter(number=Subquery(latest.values("number")[:1]))
        saved = Event.objects.filter(ticket_id=OuterRef("ticket_id"), created_at__lte=as_of, payload__state_snapshot__status__isnull=False).order_by("-sequence")
        cycle_qs = cycle_qs.annotate(_saved_id=Subquery(saved.values("pk")[:1]))
        for field in ("first_response_due_at", "decision_due_at"):
            saved_deadline = Subquery(saved.annotate(value=KeyTextTransform(field, KeyTextTransform("state_snapshot", "payload"))).values("value")[:1])
            cycle_qs = cycle_qs.annotate(**{"metric_" + field: Case(When(_saved_id__isnull=False, then=Cast(saved_deadline, DateTimeField())), default=F(field))})
        for field in ("first_response_breached_before_reclassify", "decision_breached_before_pause"):
            saved_flag = Subquery(saved.annotate(value=KeyTextTransform(field, KeyTextTransform("state_snapshot", "payload"))).values("value")[:1])
            alias = "_saved_" + field
            cycle_qs = cycle_qs.annotate(**{alias: saved_flag}).annotate(**{"metric_" + field:
                Case(When(**{alias: "true", "then": Value(True)}), default=Value(False), output_field=BooleanField())})
    else:
        cycle_qs = cycle_qs.filter(number=F("ticket__cycle_no")).annotate(metric_first_response_due_at=F("first_response_due_at"), metric_decision_due_at=F("decision_due_at"))
    known = cycle_qs.filter(started_at__isnull=False)
    known_count = known.count()
    answered_count, average, middle, p95 = _response_statistics(known, as_of)
    first_breached = Q(metric_first_response_breached_before_reclassify=True) if historical else Q(policy__first_response_breached_before_reclassify=True)
    due_cycles = known.filter(Q(metric_first_response_due_at__lte=as_of) | first_breached)
    due_count = due_cycles.count()
    on_time = due_cycles.filter(Q(first_response_at__lte=as_of) & Q(first_response_at__lte=F("metric_first_response_due_at"))).exclude(first_breached).count()
    category_rows = list(tickets.order_by().values("metric_category").annotate(n=Count("id")).order_by("-n", "metric_category")[:8])
    category_names = dict(Category.objects.filter(pk__in=[item["metric_category"] for item in category_rows]).values_list("pk", "name"))
    max_category = max((item["n"] for item in category_rows), default=1)
    for item in category_rows:
        item["category__name"] = category_names.get(item["metric_category"], "Неизвестная категория")
        item["width"] = max(4, round(item["n"] / max_category * 100))
    def rows(choices, counter):
        return [{"code": code, "label": label, "count": counter.get(code, 0), "width": round(counter.get(code, 0) / max(total, 1) * 100)} for code, label in choices]
    # Historical SLA uses the cycle selected at the cut; future answers/closures
    # must not make that cycle appear complete in an earlier snapshot.
    unanswered = Q(first_response_at__isnull=True) | Q(first_response_at__gt=as_of)
    open_cycles = known.exclude(closed_at__lte=as_of)
    first_overdue = Q(metric_first_response_due_at__lt=as_of)
    decision_overdue = Q(metric_decision_due_at__lt=as_of)
    if historical:
        first_overdue |= Q(metric_first_response_breached_before_reclassify=True)
        decision_overdue |= Q(metric_decision_breached_before_pause=True)
    overdue_cycles = open_cycles.filter((unanswered & first_overdue) | ((Q(decision_at__isnull=True) | Q(decision_at__gt=as_of)) & decision_overdue))
    reviewed = known.filter(decision_at__lte=as_of).count()
    return render(request, "feedback/analytics.html", {
        "active_nav": "analytics", "mode": mode, "filters": filters, "historical": historical, "as_of": as_of,
        "total": total, "closed": counts.get("CLOSED", 0),
        "closure_rate": round(counts.get("CLOSED", 0) / total * 100) if total else None,
        "implemented": tickets.filter(metric_status="CLOSED", metric_resolution="IMPLEMENTED").exclude(metric_type="PRAISE").count(),
        "overdue": tickets.filter(Q(pk__in=overdue_cycles.values("ticket_id")) | (Q(metric_status__in=["REVIEW", "IN_PROGRESS", "WAITING_INFO"], metric_decision="APPROVED") & Q(metric_plan_due__lt=as_of))).exclude(metric_status="CLOSED").count() if historical else tickets.filter(_overdue_q()).count(),
        "reviewed": reviewed, "unassigned": tickets.filter(metric_assignee__isnull=True).exclude(metric_status="CLOSED").count(),
        "historical_fallback_count": tickets.filter(_snapshot_id__isnull=True).count() if historical else 0,
        "status_rows": rows([*STATUSES, ("UNKNOWN", "Состояние не восстановлено")] if counts.get("UNKNOWN") else STATUSES, counts), "type_rows": rows(TYPES, types), "priority_rows": rows(PRIORITIES, priorities),
        "category_rows": category_rows, "outcomes": outcomes,
        "response_mean": _duration_label(average), "response_median": _duration_label(middle),
        "response_p95": _duration_label(p95) if answered_count >= 20 else "Недостаточно данных",
        "answered_count": answered_count, "known_count": known_count, "unknown_count": total - known_count,
        "unknown_source_count": unknown_source_count,
        "awaiting_count": known.filter(unanswered).exclude(closed_at__lte=as_of).count(),
        "sla_rate": round(on_time / due_count * 100) if due_count else None, "sla_due_count": due_count,
    }, status=200 if valid else 422)


def _config_audit(user, obj, action, description):
    LogEntry.objects.log_actions(user_id=user.pk, queryset=type(obj).objects.filter(pk=obj.pk), action_flag=action, change_message=description)


@login_required
def workspace_settings(request):
    if not (request.user.is_superuser or has_role(request.user, "admin")):
        raise PermissionDenied
    route_form = RoutingRuleForm(user=request.user)
    category_form = CategoryForm()
    queue_form = QueueForm(user=request.user)
    status = 200
    if request.method == "POST":
        operation = request.POST.get("operation")
        if operation == "route":
            route_form = RoutingRuleForm(request.POST, user=request.user)
            form = route_form
        elif operation == "category":
            category_form = CategoryForm(request.POST)
            form = category_form
        elif operation == "queue":
            queue_form = QueueForm(request.POST, user=request.user)
            form = queue_form
        elif operation in {"disable_route", "archive_category"}:
            model = RoutingRule if operation == "disable_route" else Category
            try:
                object_id = int(request.POST.get("object_id", ""))
            except (TypeError, ValueError):
                raise Http404("Настройка не найдена")
            obj = get_object_or_404(model, pk=object_id)
            if isinstance(obj, RoutingRule) and obj.queue.restricted and not (request.user.restricted_access or request.user.is_superuser):
                raise PermissionDenied
            with transaction.atomic():
                obj.active = False
                obj.save(update_fields=["active"])
                _config_audit(request.user, obj, CHANGE, "Деактивация из настроек приложения")
            messages.success(request, "Настройки сохранены. Исторические связи сохранены.")
            return redirect("feedback:settings")
        else:
            raise PermissionDenied
        if form.is_valid():
            with transaction.atomic():
                obj = form.save()
                _config_audit(request.user, obj, ADDITION, "Создание из настроек приложения")
            messages.success(request, "Настройки сохранены.")
            return redirect("feedback:settings")
        status = 422
    queues = Queue.objects.select_related("owner").prefetch_related("members").order_by("name")
    routes = RoutingRule.objects.select_related("category", "queue", "affected_unit")
    if not (request.user.restricted_access or request.user.is_superuser):
        queues = queues.filter(restricted=False)
        routes = routes.filter(queue__restricted=False)
    return render(request, "feedback/settings.html", {"active_nav": "settings", "route_form": route_form, "category_form": category_form, "queue_form": queue_form, "queues": queues, "routes": routes, "categories": Category.objects.order_by("name"), "business_timezone": getattr(settings, "BUSINESS_TIMEZONE", "Asia/Bishkek")}, status=status)


@login_required
def notifications(request):
    items = visible_notifications(request.user).select_related("ticket")
    page = Paginator(items, 25).get_page(request.GET.get("page"))
    return render(request, "feedback/notifications.html", {"page_obj": page, "active_nav": "notifications"})


@login_required
@require_POST
def read_notification(request, pk):
    item = get_object_or_404(visible_notifications(request.user), pk=pk)
    if item.read_at is None:
        item.read_at = timezone.now()
        item.save(update_fields=["read_at"])
    return redirect("feedback:detail", pk=item.ticket_id)


@login_required
def canonical_search(request, pk):
    ticket = get_object_or_404(visible_tickets(request.user), pk=pk)
    cycle = ticket.current_cycle
    if not can_process(request.user, ticket) or ticket.status != "REVIEW" or (cycle and cycle.policy.get("assignment_resume_status")):
        raise PermissionDenied
    query = request.GET.get("q", "").strip()[:200]
    candidates = visible_tickets(request.user).exclude(pk=ticket.pk)
    if query:
        candidates = scoped_search(candidates, query)
    page = Paginator(candidates.order_by("number"), 25).get_page(request.GET.get("page"))
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"results": [{"id": str(item.pk), "label": f"{item.number} — {item.title[:100]}"} for item in page],
            "page": page.number, "pages": page.paginator.num_pages, "has_next": page.has_next(), "count": page.paginator.count})
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "feedback/canonical_search.html", {"ticket": ticket, "page_obj": page, "q": query, "querystring": params.urlencode(), "active_nav": "list"})


@login_required
def similar_requests(request, pk):
    ticket = get_object_or_404(visible_tickets(request.user), pk=pk)
    page = Paginator(similar_tickets(visible_tickets(request.user), ticket).select_related("category"), 25).get_page(request.GET.get("page"))
    return render(request, "feedback/similar.html", {"ticket": ticket, "page_obj": page, "active_nav": "list"})


def _coordinated_tickets(user):
    if user.is_superuser:
        return visible_tickets(user)
    if not has_role(user, "coordinator", "manager"):
        return Ticket.objects.none()
    return visible_tickets(user).filter(staff_scope(user))


@login_required
def failed_deliveries(request):
    if not (request.user.is_superuser or has_role(request.user, "coordinator", "manager")):
        raise PermissionDenied
    items = Outbox.objects.filter(state="failed", ticket__in=_coordinated_tickets(request.user)).select_related("ticket", "recipient", "event")
    # Internal event existence is protected exactly like the event body.
    items = items.exclude(event__visibility="internal", ticket__author=request.user)
    page = Paginator(items.order_by("next_attempt_at", "pk"), 25).get_page(request.GET.get("page"))
    return render(request, "feedback/failed_deliveries.html", {"page_obj": page, "active_nav": "deliveries"})


@login_required
def retry_delivery(request, pk):
    from .services import retry_delivery as retry_delivery_command
    item = get_object_or_404(Outbox.objects.filter(ticket__in=_coordinated_tickets(request.user)), pk=pk)
    if item.event.visibility == "internal" and not can_view_internal(request.user, item.ticket):
        raise Http404
    form = RetryDeliveryForm(request.POST or None)
    error = None
    status = 422 if request.method == "POST" else 200
    if request.method == "POST" and form.is_valid():
        try:
            retry_delivery_command(actor=request.user, outbox_id=item.pk, reason=form.cleaned_data["reason"])
        except DomainError as exc:
            error, status = str(exc), exc.status
        else:
            messages.success(request, "Повторная доставка поставлена в очередь.")
            return redirect("feedback:failed_deliveries")
    return render(request, "feedback/retry_delivery.html", {"item": item, "form": form, "error": error, "active_nav": "deliveries"}, status=status)
