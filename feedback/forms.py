"""Server-side forms for the Russian staff feedback interface."""
from __future__ import annotations

import uuid

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Q
from django.utils import timezone

from django.conf import settings

from .attachments import ACCEPT_ATTRIBUTE, ALLOWED_LABEL
from .models import Category, OrgUnit, Queue, RoutingRule, User


TYPES = [("PROBLEM", "Проблема"), ("SUGGESTION", "Предложение"), ("PRAISE", "Положительный отзыв")]
PRIORITIES = [("LOW", "Низкий"), ("MEDIUM", "Средний"), ("HIGH", "Высокий")]
STATUSES = [("NEW", "Новое"), ("REVIEW", "На рассмотрении"), ("WAITING_INFO", "Ожидает уточнения"), ("IN_PROGRESS", "В работе"), ("CLOSED", "Закрыто")]


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="Имя пользователя", widget=forms.TextInput(attrs={"autocomplete": "username", "placeholder": "Например, author", "autofocus": True}))
    password = forms.CharField(label="Пароль", strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "placeholder": "Введите пароль"}))
    error_messages = {"invalid_login": "Не удалось войти. Проверьте имя пользователя и пароль.", "inactive": "Учётная запись отключена. Обратитесь к администратору."}


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Returns a list; content checks happen in the transactional service."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ACCEPT_ATTRIBUTE}))
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data if item]
        return [single(data, initial)] if data else []


def attachment_help() -> str:
    return (f"До {settings.ATTACHMENT_MAX_FILES} файлов, каждый до {settings.ATTACHMENT_MAX_BYTES // (1024 * 1024)} МБ: "
            f"{ALLOWED_LABEL}. Скриншот или пример часто объясняет быстрее текста.")


class CreateTicketForm(forms.Form):
    request_type = forms.ChoiceField(label="Тип обращения", choices=TYPES, initial="SUGGESTION")
    category = forms.ModelChoiceField(label="Категория", queryset=Category.objects.none(), empty_label="Выберите категорию")
    description = forms.CharField(label="Что произошло или что вы предлагаете?", min_length=10, max_length=10000, widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Опишите ситуацию, её влияние и ожидаемый результат…"}))
    proposed_solution = forms.CharField(label="Предлагаемое решение", required=False, max_length=5000, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Если у вас есть идея решения, расскажите о ней"}))
    affected_unit = forms.ModelChoiceField(label="К какому подразделению относится вопрос?", queryset=OrgUnit.objects.none(), required=False, empty_label="Не знаю / несколько подразделений")
    reported_priority = forms.ChoiceField(label="Приоритет", choices=PRIORITIES, initial="MEDIUM")
    confidentiality = forms.ChoiceField(label="Доступ к обращению", choices=[("STANDARD", "Обычное — автор и ответственные"), ("RESTRICTED", "Конфиденциальное — только сотрудники с допуском")])
    attachments = MultipleFileField(label="Материалы")
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(active=True).order_by("name")
        self.fields["affected_unit"].queryset = OrgUnit.objects.order_by("name")
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4()


class TicketFilterForm(forms.Form):
    q = forms.CharField(label="Поиск", required=False, max_length=200, widget=forms.TextInput(attrs={"placeholder": "Номер, проблема или предложение", "aria-label": "Поиск обращений"}))
    status = forms.ChoiceField(label="Статус", required=False, choices=[("", "Все статусы"), *STATUSES])
    request_type = forms.ChoiceField(label="Тип", required=False, choices=[("", "Все типы"), *TYPES])
    priority = forms.ChoiceField(label="Приоритет", required=False, choices=[("", "Все приоритеты"), *PRIORITIES])
    category = forms.ModelChoiceField(label="Категория", required=False, queryset=Category.objects.none(), empty_label="Все категории")
    unit = forms.ModelChoiceField(label="Подразделение", required=False, queryset=OrgUnit.objects.none(), empty_label="Все подразделения")
    date_from = forms.DateField(label="Подано с", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="Подано по", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    overdue = forms.BooleanField(label="Только просроченные", required=False)
    author = forms.ModelChoiceField(label="Автор", required=False, queryset=User.objects.none(), empty_label="Все авторы")
    assignee = forms.ModelChoiceField(label="Исполнитель", required=False, queryset=User.objects.none(), empty_label="Все исполнители")
    queue = forms.ModelChoiceField(label="Очередь", required=False, queryset=Queue.objects.none(), empty_label="Все очереди")

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        from .permissions import visible_tickets
        scope = visible_tickets(user)
        self.fields["category"].queryset = Category.objects.filter(pk__in=scope.values("category_id")).order_by("name")
        self.fields["unit"].queryset = OrgUnit.objects.filter(pk__in=scope.values("affected_unit_id")).order_by("name")
        for field in ("author", "assignee"):
            self.fields[field].queryset = User.objects.filter(pk__in=scope.values(field + "_id")).order_by("first_name", "username")
        self.fields["queue"].queryset = Queue.objects.filter(pk__in=scope.values("queue_id")).order_by("name")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("date_from") and cleaned.get("date_to") and cleaned["date_from"] > cleaned["date_to"]:
            raise forms.ValidationError("Начало периода должно быть раньше окончания.")
        return cleaned


class CommandForm(forms.Form):
    command = forms.ChoiceField(choices=[(x, x) for x in ("start", "assign", "approve", "decline", "acknowledge", "ask_info", "answer_info", "implement", "reopen", "withdraw", "duplicate", "no_response", "comment", "resume_info", "extend_info", "reclassify")])
    version = forms.IntegerField(min_value=1)
    message = forms.CharField(required=False, max_length=10000)
    visibility = forms.ChoiceField(required=False, choices=[("public", "Автору и ответственным"), ("internal", "Только ответственным")])
    reason = forms.CharField(required=False, max_length=5000)
    criteria = forms.CharField(required=False, max_length=5000)
    result = forms.CharField(required=False, max_length=5000)
    evidence = forms.CharField(required=False, max_length=5000)
    question = forms.CharField(required=False, max_length=5000)
    response = forms.CharField(required=False, max_length=10000)
    plan_due_at = forms.DateTimeField(required=False, input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"])
    due_at = forms.DateTimeField(required=False, input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"])
    assignee_id = forms.IntegerField(required=False, min_value=1)
    queue_id = forms.IntegerField(required=False, min_value=1)
    canonical_id = forms.UUIDField(required=False)
    category_id = forms.IntegerField(required=False, min_value=1)
    effective_priority = forms.ChoiceField(required=False, choices=PRIORITIES)


class RecurrenceFilterForm(forms.Form):
    mode = forms.ChoiceField(label="Данные", choices=[("demo", "Демонстрационные"), ("production", "Рабочие")])
    date_from = forms.DateField(label="Повторные обращения с", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="По", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        values = super().clean()
        if values.get("date_from") and values.get("date_to") and values["date_from"] > values["date_to"]:
            raise forms.ValidationError("Начало периода должно быть раньше окончания.")
        return values


class AnalyticsFilterForm(forms.Form):
    date_from = forms.DateField(label="Поступили с", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="Поступили по", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    as_of = forms.DateTimeField(label="Состояние на", required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"])

    def clean(self):
        result = super().clean()
        if result.get("date_from") and result.get("date_to") and result["date_from"] > result["date_to"]:
            raise forms.ValidationError("Начало периода должно быть раньше окончания.")
        if result.get("as_of") and result["as_of"] > timezone.now():
            self.add_error("as_of", "Дата среза не может находиться в будущем.")
        return result


class RetryDeliveryForm(forms.Form):
    reason = forms.CharField(label="Причина повторной доставки", max_length=5000, widget=forms.Textarea(attrs={"rows": 2}))


class NotificationSettingsForm(forms.ModelForm):
    email = forms.EmailField(label="Рабочая почта", required=False, max_length=254,
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "name@company.ru"}))

    class Meta:
        model = User
        fields = ["email", "notify_email", "notify_telegram", "notify_bitrix"]
        labels = {"notify_email": "Присылать на почту", "notify_telegram": "Присылать в Telegram",
                  "notify_bitrix": "Присылать в Битрикс24"}


class RoutingRuleForm(forms.ModelForm):
    request_type = forms.ChoiceField(label="Тип обращения", required=False, choices=[("", "Любой тип"), *TYPES])
    class Meta:
        model = RoutingRule
        fields = ["category", "request_type", "affected_unit", "queue", "priority", "active"]
        labels = {"category": "Категория", "request_type": "Тип обращения", "affected_unit": "Затронутое подразделение", "queue": "Очередь", "priority": "Приоритет правила", "active": "Правило активно"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["request_type"].choices = [("", "Любой тип"), *TYPES]
        self.fields["category"].queryset = Category.objects.filter(active=True).order_by("name")
        self.fields["queue"].queryset = Queue.objects.order_by("name")
        if user and not (user.restricted_access or user.is_superuser):
            self.fields["queue"].queryset = self.fields["queue"].queryset.filter(restricted=False)
        self.fields["priority"].help_text = "Большее значение имеет преимущество. Совпадающие правила одного приоритета недопустимы."

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("active") and cleaned.get("queue") and cleaned.get("priority") is not None:
            duplicate = RoutingRule.objects.filter(active=True, queue__restricted=cleaned["queue"].restricted, priority=cleaned["priority"])
            if cleaned.get("category"):
                duplicate = duplicate.filter(Q(category=cleaned["category"]) | Q(category__isnull=True))
            if cleaned.get("request_type"):
                duplicate = duplicate.filter(Q(request_type=cleaned["request_type"]) | Q(request_type=""))
            if cleaned.get("affected_unit"):
                duplicate = duplicate.filter(Q(affected_unit=cleaned["affected_unit"]) | Q(affected_unit__isnull=True))
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise forms.ValidationError("Есть пересекающееся активное правило того же приоритета. Измените приоритет или отключите прежнюю версию.")
        return cleaned


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "code"]
        labels = {"name": "Название категории", "code": "Уникальный код латиницей"}


class QueueForm(forms.ModelForm):
    class Meta:
        model = Queue
        fields = ["name", "code", "owner", "members", "restricted"]
        labels = {"name": "Название очереди", "code": "Уникальный код", "owner": "Координатор очереди", "members": "Сотрудники очереди", "restricted": "Очередь с ограниченным доступом"}

    def __init__(self, *args, user=None, **kwargs):
        self.actor = user
        super().__init__(*args, **kwargs)
        from .permissions import has_role
        allowed_ids = [person.pk for person in User.objects.filter(is_active=True) if person.is_superuser or has_role(person, "processor", "coordinator", "manager")]
        self.fields["owner"].queryset = User.objects.filter(pk__in=allowed_ids).order_by("first_name", "username")
        self.fields["owner"].required = True
        self.fields["members"].queryset = User.objects.filter(pk__in=allowed_ids).order_by("first_name", "username")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("restricted"):
            if not self.actor or not (self.actor.restricted_access or self.actor.is_superuser):
                raise forms.ValidationError("Для настройки конфиденциальной очереди нужен отдельный допуск.")
            people = list(cleaned.get("members") or []) + ([cleaned["owner"]] if cleaned.get("owner") else [])
            if any(not (p.restricted_access or p.is_superuser) for p in people):
                raise forms.ValidationError("У всех сотрудников конфиденциальной очереди должен быть допуск.")
        return cleaned
