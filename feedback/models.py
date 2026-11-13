"""Transactional domain model; source snapshots and runtime events remain distinct."""
from __future__ import annotations

import uuid
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .search_fields import TicketSearchDocument


def ticket_number() -> str:
    # 28 UUID hex characters fit the existing public-number column.
    return f"MT-{uuid.uuid4().hex[:28].upper()}"


class OrgUnit(models.Model):
    name = models.CharField(max_length=160)
    code = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        AUTHOR = "author", "Сотрудник"
        PROCESSOR = "processor", "Исполнитель"
        COORDINATOR = "coordinator", "Координатор"
        MANAGER = "manager", "Руководитель"
        ADMIN = "admin", "Администратор"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AUTHOR)
    extra_roles = models.JSONField(default=list, blank=True)
    org_unit = models.ForeignKey(OrgUnit, null=True, blank=True, on_delete=models.SET_NULL)
    restricted_access = models.BooleanField(default=False)
    # External notification channels. In-app notifications are always kept.
    notify_email = models.BooleanField("Уведомления на почту", default=True)
    notify_telegram = models.BooleanField("Уведомления в Telegram", default=True)
    notify_bitrix = models.BooleanField("Уведомления в Битрикс24", default=True)
    telegram_chat_id = models.CharField("Telegram chat id", max_length=32, blank=True, default="")
    telegram_link_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    telegram_link_expires_at = models.DateTimeField(null=True, blank=True)
    bitrix_user_id = models.CharField("ID пользователя в Битрикс24", max_length=32, blank=True, default="",
                                      help_text="Если пусто, определяется по e-mail при первой отправке.")

    def clean(self):
        super().clean()
        if not isinstance(self.extra_roles, list) or any(not isinstance(role, str) or role not in self.Role.values for role in self.extra_roles):
            raise ValidationError({"extra_roles":"Дополнительные роли должны быть списком допустимых кодов ролей."})

    @property
    def role_label(self):
        return self.get_role_display()

    def __str__(self):
        return self.get_full_name() or self.username


class Category(models.Model):
    name = models.CharField(max_length=160)
    code = models.SlugField(unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Queue(models.Model):
    name = models.CharField(max_length=160)
    code = models.SlugField(unique=True)
    restricted = models.BooleanField(default=False)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_queues")
    members = models.ManyToManyField(User, related_name="queues", blank=True)

    def __str__(self):
        return self.name


class RoutingRule(models.Model):
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT)
    request_type = models.CharField(max_length=20, blank=True)
    affected_unit = models.ForeignKey(OrgUnit, null=True, blank=True, on_delete=models.PROTECT)
    queue = models.ForeignKey(Queue, on_delete=models.PROTECT)
    priority = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    supersedes = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="successor")

    class Meta:
        ordering = ["-priority", "pk"]

    def clean(self):
        super().clean()
        if self.request_type not in ("", "PROBLEM", "SUGGESTION", "PRAISE"):
            raise ValidationError({"request_type": "Недопустимый тип обращения."})
        if not self.active or not self.queue_id:
            return
        competing = RoutingRule.objects.filter(active=True,priority=self.priority,queue__restricted=self.queue.restricted).exclude(pk=self.pk)
        if self.category_id:
            competing = competing.filter(Q(category_id=self.category_id) | Q(category__isnull=True))
        if self.request_type:
            competing = competing.filter(Q(request_type=self.request_type) | Q(request_type=""))
        if self.affected_unit_id:
            competing = competing.filter(Q(affected_unit_id=self.affected_unit_id) | Q(affected_unit__isnull=True))
        if competing.exists():
            raise ValidationError("Правило пересекается с действующим правилом того же приоритета.")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous:
                immutable = ("category_id", "request_type", "affected_unit_id", "queue_id", "priority", "version", "supersedes_id")
                if any(getattr(previous, field) != getattr(self, field) for field in immutable) or (not previous.active and self.active):
                    raise ValidationError("Условия маршрута неизменяемы. Создайте следующую версию правила.")
        return super().save(*args, **kwargs)


class Ticket(models.Model):
    class Type(models.TextChoices):
        PROBLEM = "PROBLEM", "Проблема"
        SUGGESTION = "SUGGESTION", "Предложение"
        PRAISE = "PRAISE", "Положительный отзыв"

    class Priority(models.TextChoices):
        LOW = "LOW", "Низкий"
        MEDIUM = "MEDIUM", "Средний"
        HIGH = "HIGH", "Высокий"

    class Status(models.TextChoices):
        NEW = "NEW", "Новое"
        REVIEW = "REVIEW", "На рассмотрении"
        WAITING_INFO = "WAITING_INFO", "Ожидает уточнения"
        IN_PROGRESS = "IN_PROGRESS", "В работе"
        CLOSED = "CLOSED", "Закрыто"

    class Confidentiality(models.TextChoices):
        STANDARD = "STANDARD", "Обычное"
        RESTRICTED = "RESTRICTED", "Конфиденциальное"

    class Resolution(models.TextChoices):
        IMPLEMENTED = "IMPLEMENTED", "Реализовано"
        DECLINED = "DECLINED", "Мотивированный отказ"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Принято к сведению"
        DUPLICATE = "DUPLICATE", "Дубликат"
        WITHDRAWN = "WITHDRAWN", "Отозвано автором"
        NO_RESPONSE = "NO_RESPONSE", "Недостаточно сведений"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=32, unique=True, default=ticket_number)
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="authored_tickets")
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    affected_unit = models.ForeignKey(OrgUnit, null=True, blank=True, on_delete=models.PROTECT)
    author_unit_name = models.CharField(max_length=160, blank=True)
    queue = models.ForeignKey(Queue, on_delete=models.PROTECT, related_name="tickets")
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_tickets")
    request_type = models.CharField(max_length=20, choices=Type.choices)
    description = models.TextField(max_length=10000)
    proposed_solution = models.TextField(max_length=5000, blank=True)
    search_document = models.GeneratedField(expression=TicketSearchDocument("description", "proposed_solution"),
                                           output_field=SearchVectorField(), db_persist=True)
    reported_priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    effective_priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    confidentiality = models.CharField(max_length=12, choices=Confidentiality.choices, default=Confidentiality.STANDARD)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    decision = models.CharField(max_length=20, blank=True, default="")
    criteria = models.TextField(blank=True)
    plan_due_at = models.DateTimeField(null=True, blank=True)
    resolution_code = models.CharField(max_length=20, choices=Resolution.choices, blank=True, default="")
    resolution_summary = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    cycle_no = models.PositiveIntegerField(default=1)
    is_demo = models.BooleanField(default=False)
    source_record_id = models.CharField(max_length=120, null=True, blank=True, unique=True)
    source_data = models.JSONField(default=dict, blank=True)
    quality_flags = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["author", "-created_at"]), models.Index(fields=["queue", "status"]), models.Index(fields=["assignee", "status"]), models.Index(fields=["category", "status"]), models.Index(fields=["plan_due_at"], condition=Q(status__in=["IN_PROGRESS", "WAITING_INFO", "REVIEW"], decision="APPROVED"), name="ticket_open_plan_due"), GinIndex(fields=["search_document"], name="ticket_search_document_gin")]
        constraints = [models.CheckConstraint(condition=(Q(status="CLOSED") & ~Q(resolution_code="") & ~Q(resolution_summary="") & Q(closed_at__isnull=False)) | (~Q(status="CLOSED") & Q(resolution_code="") & Q(closed_at__isnull=True)), name="ticket_closure_consistent"), models.CheckConstraint(condition=Q(version__gte=1) & Q(cycle_no__gte=1), name="ticket_positive_version")]

    @property
    def title(self):
        text = self.description.strip().split("\n")[0]
        return text[:107] + "…" if len(text) > 110 else text

    @property
    def body(self):
        """Description without its first line when that line is already shown as the title."""
        first, _, rest = self.description.strip().partition("\n")
        return rest.strip() if rest.strip() and len(first) <= 110 else self.description

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def type_label(self):
        return self.get_request_type_display()

    @property
    def priority_label(self):
        return self.get_effective_priority_display()

    @property
    def current_cycle(self):
        prefetched = getattr(self, "prefetched_cycles", None)
        if prefetched is None:
            prefetched = getattr(self, "_prefetched_objects_cache", {}).get("cycles")
        if prefetched is not None:
            return next((cycle for cycle in prefetched if cycle.number == self.cycle_no), None)
        return self.cycles.filter(number=self.cycle_no).first()

    @property
    def is_overdue(self):
        if self.status == self.Status.CLOSED:
            return False
        cycle = self.current_cycle
        now = timezone.now()
        return bool((cycle and ((cycle.first_response_due_at and not cycle.first_response_at and (now > cycle.first_response_due_at or cycle.policy.get("first_response_breached_before_reclassify"))) or (cycle.decision_due_at and not cycle.decision_at and (now > cycle.decision_due_at or cycle.policy.get("decision_breached_before_pause"))))) or (self.plan_due_at and self.decision == "APPROVED" and self.status in (self.Status.IN_PROGRESS,self.Status.WAITING_INFO,self.Status.REVIEW) and now > self.plan_due_at))

    def __str__(self):
        return self.number


class ProcessingCycle(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="cycles")
    number = models.PositiveIntegerField(default=1)
    started_at = models.DateTimeField(null=True, blank=True)
    first_response_due_at = models.DateTimeField(null=True, blank=True)
    decision_due_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    decision_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    resolution_code = models.CharField(max_length=20, blank=True)
    resolution_summary = models.TextField(blank=True)
    policy = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket", "number"], name="unique_processing_cycle")]
        indexes = [models.Index(fields=["first_response_due_at"], condition=Q(first_response_at__isnull=True), name="cycle_response_due"), models.Index(fields=["decision_due_at"], condition=Q(decision_at__isnull=True), name="cycle_decision_due")]


class Decision(models.Model):
    class Code(models.TextChoices):
        APPROVED = "APPROVED", "Одобрено"
        DECLINED = "DECLINED", "Отказ"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Принято к сведению"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="decisions")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    code = models.CharField(max_length=20, choices=Code.choices)
    reason = models.TextField()
    criteria = models.TextField(blank=True)
    cycle_no = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)


class InfoRequest(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="info_requests")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    question = models.TextField()
    due_at = models.DateTimeField()
    resume_status = models.CharField(max_length=20)
    response = models.TextField(blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    expired_notified = models.BooleanField(default=False)
    reminder_delivered_at = models.DateTimeField(null=True, blank=True)
    reminder_generation = models.PositiveIntegerField(default=1)
    # Cleared transactionally once the current generation is enqueued.
    reminder_due_at = models.DateTimeField(null=True, blank=True)
    pause_until = models.DateTimeField(null=True, blank=True)
    paused_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket"], condition=Q(answered_at__isnull=True), name="one_active_info_request")]
        indexes = [models.Index(fields=["due_at"], condition=Q(answered_at__isnull=True), name="active_info_due"), models.Index(fields=["reminder_due_at"], condition=Q(answered_at__isnull=True, reminder_due_at__isnull=False), name="info_reminder_due")]


class Comment(models.Model):
    class Visibility(models.TextChoices):
        PUBLIC = "public", "Сообщение автору"
        INTERNAL = "internal", "Внутренняя заметка"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.PROTECT)
    message = models.TextField(max_length=10000)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "pk"]


class Event(models.Model):
    Visibility = Comment.Visibility
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=50)
    summary = models.TextField()
    visibility = models.CharField(max_length=10, choices=Comment.Visibility.choices, default="public")
    payload = models.JSONField(default=dict)
    sequence = models.PositiveIntegerField()
    origin = models.CharField(max_length=12, default="runtime")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["ticket", "sequence"], name="unique_ticket_event_sequence")]


class Outbox(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    recipient = models.ForeignKey(User, on_delete=models.CASCADE)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    message = models.TextField()
    state = models.CharField(max_length=12, default="pending", choices=[(s, s) for s in ["pending", "processing", "sent", "failed"]])
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "next_attempt_at"])]
        constraints = [models.UniqueConstraint(fields=["event", "recipient"], name="unique_event_delivery")]


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    outbox = models.OneToOneField(Outbox, on_delete=models.CASCADE)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def display_message(self):
        """The ticket number is already shown next to the message; drop the repeated prefix."""
        prefix = f"{self.ticket.number}: "
        return self.message[len(prefix):] if self.message.startswith(prefix) else self.message


class ExternalDelivery(models.Model):
    """One outbound copy of an in-app notification per external channel."""
    class Channel(models.TextChoices):
        EMAIL = "email", "Почта"
        TELEGRAM = "telegram", "Telegram"
        BITRIX24 = "bitrix24", "Битрикс24"

    outbox = models.ForeignKey(Outbox, on_delete=models.CASCADE, related_name="external_deliveries")
    channel = models.CharField(max_length=12, choices=Channel.choices)
    state = models.CharField(max_length=12, default="pending", choices=[(s, s) for s in ["pending", "processing", "sent", "failed", "skipped"]])
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "next_attempt_at"])]
        constraints = [models.UniqueConstraint(fields=["outbox", "channel"], name="unique_external_delivery")]


class IntegrationState(models.Model):
    """Small persistent cursors for integrations (e.g. Telegram update offset)."""
    key = models.CharField(max_length=80, unique=True)
    value = models.JSONField(default=dict, blank=True)


class IdempotencyRecord(models.Model):
    actor = models.ForeignKey(User, on_delete=models.CASCADE)
    key = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "key"], name="unique_actor_idempotency_key")]


class ImportBatch(models.Model):
    checksum = models.CharField(max_length=64, unique=True)
    source_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)
    summary = models.JSONField(default=dict)


class ImportRecord(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="records")
    source_id = models.CharField(max_length=120)
    raw = models.JSONField(default=dict)
    issues = models.JSONField(default=list)
    ticket = models.ForeignKey(Ticket, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "source_id"], name="unique_import_source_id")]


class TicketRelation(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="duplicate_relations")
    canonical = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="duplicates")
    active = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(ticket=models.F("canonical")), name="duplicate_not_self"), models.UniqueConstraint(fields=["ticket"], condition=Q(active=True), name="one_active_duplicate_relation")]


Status = Ticket.Status
Priority = Ticket.Priority
RequestType = Ticket.Type


class RateLimitBucket(models.Model):
    """Shared database counters, usable across web worker processes."""
    key = models.CharField(max_length=120,unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()


class WorkerHeartbeat(models.Model):
    key = models.CharField(max_length=80, unique=True, default="default")
    last_seen = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=500, blank=True)
    last_counts = models.JSONField(default=dict, blank=True)


def attachment_storage():
    # Callable so tests and deployments can point ATTACHMENTS_ROOT elsewhere.
    from django.conf import settings
    from django.core.files.storage import FileSystemStorage
    return FileSystemStorage(location=settings.ATTACHMENTS_ROOT, base_url=None)


def attachment_path(instance, filename):
    # Random on-disk names: the original name is metadata only, never a path.
    return f"{instance.ticket_id.hex[:2]}/{uuid.uuid4().hex}"


class Attachment(models.Model):
    """A file added at submission or to a conversation message.

    Visibility follows its message: internal files are never shown to the author.
    Access is always checked through the ticket; there is no public URL.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    comment = models.ForeignKey(Comment, null=True, blank=True, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    visibility = models.CharField(max_length=10, choices=Comment.Visibility.choices, default=Comment.Visibility.PUBLIC)
    file = models.FileField(upload_to=attachment_path, storage=attachment_storage, max_length=200)
    original_name = models.CharField(max_length=160)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "original_name"]

    @property
    def is_image(self) -> bool:
        return self.content_type in {"image/png", "image/jpeg", "image/webp"}
