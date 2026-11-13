"""External notification channels: e-mail, Telegram and Bitrix24.

An in-app Notification stays the source of truth. After it is stored, the worker
queues one ExternalDelivery per enabled channel and sends it outside the business
transaction with its own retries. Only the ticket number, a neutral event summary
and a link leave the system: descriptions, internal notes, decisions' texts and
attachments never do (docs/hackathon/integration.md). Errors are stored as short
codes, never as exception text, because that text may echo tokens or addresses.
"""
from __future__ import annotations

import html
import json
import secrets
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import connection, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import ExternalDelivery, InfoRequest, IntegrationState, Outbox, User
from .permissions import visible_tickets

# Events worth leaving the application for. Reclassification, delivery retries and
# every internal-visibility event stay in-app only.
EXTERNAL_KINDS = frozenset({
    "CREATED", "START", "ASSIGN", "APPROVE", "DECLINE", "ACKNOWLEDGE", "ASK_INFO", "ANSWER_INFO",
    "RESUME_INFO", "EXTEND_INFO", "INFO_REMINDER", "INFO_EXPIRED", "IMPLEMENT", "COMMENT", "REOPEN",
    "WITHDRAW", "DUPLICATE", "NO_RESPONSE", "FIRST_RESPONSE_OVERDUE", "DECISION_OVERDUE",
    "IMPLEMENTATION_OVERDUE",
})
MAX_ATTEMPTS = 6
HTTP_TIMEOUT = 10
TELEGRAM_LINK_TTL = timedelta(minutes=15)


class ChannelError(Exception):
    def __init__(self, code: str, *, permanent: bool = False):
        super().__init__(code)
        self.code = code[:120]
        self.permanent = permanent


class SkipDelivery(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code[:120]


# --- configuration -----------------------------------------------------------

def email_enabled() -> bool:
    return bool(settings.NOTIFY_EMAIL)


def telegram_enabled() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def bitrix_enabled() -> bool:
    return bool(settings.BITRIX24_WEBHOOK_URL)


def channels_for(user: User) -> list[str]:
    channels = []
    if email_enabled() and user.notify_email and user.email:
        channels.append(ExternalDelivery.Channel.EMAIL)
    if telegram_enabled() and user.notify_telegram and user.telegram_chat_id:
        channels.append(ExternalDelivery.Channel.TELEGRAM)
    if bitrix_enabled() and user.notify_bitrix and (user.bitrix_user_id or user.email):
        channels.append(ExternalDelivery.Channel.BITRIX24)
    return channels


def enqueue_external(job: Outbox) -> int:
    """Queue external copies; runs inside the in-app delivery transaction."""
    if job.event.visibility != "public" or job.event.kind not in EXTERNAL_KINDS:
        return 0
    created = 0
    for channel in channels_for(job.recipient):
        created += ExternalDelivery.objects.get_or_create(outbox=job, channel=channel)[1]
    return created


# --- message ----------------------------------------------------------------

def compose(job: Outbox) -> dict[str, str]:
    ticket, recipient = job.ticket, job.recipient
    own = recipient.pk == ticket.author_id
    if ticket.confidentiality == ticket.Confidentiality.RESTRICTED:
        # Even the number and the kind of change stay inside for restricted tickets.
        headline = "Есть обновление по обращению с ограниченным доступом"
        subject = headline
        url = settings.SITE_URL + reverse("feedback:notifications")
        lead = "Откройте систему, чтобы посмотреть подробности."
    else:
        headline = job.event.summary
        subject = f"{'Ваше обращение' if own else 'Обращение'} {ticket.number}: {headline}"
        url = settings.SITE_URL + reverse("feedback:detail", args=[ticket.pk])
        lead = f"{'Ваше обращение' if own else 'Обращение'} {ticket.number}"
    return {"subject": subject[:200], "headline": headline, "lead": lead, "url": url,
            "name": recipient.get_full_name() or recipient.username,
            "settings_url": settings.SITE_URL + reverse("feedback:notification_settings")}


# --- transports -------------------------------------------------------------

def _http_post(url: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": "modus-feedback/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChannelError("network_" + type(exc).__name__) from None
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def send_email(user: User, message: dict[str, str]) -> None:
    text = render_to_string("emails/notification.txt", message)
    body = render_to_string("emails/notification.html", message)
    mail = EmailMultiAlternatives(message["subject"], text, settings.DEFAULT_FROM_EMAIL, [user.email])
    mail.attach_alternative(body, "text/html")
    try:
        mail.send()
    except smtplib.SMTPRecipientsRefused:
        raise ChannelError("email_recipient_refused", permanent=True) from None
    except smtplib.SMTPAuthenticationError:
        raise ChannelError("email_auth_failed") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise ChannelError("email_" + type(exc).__name__) from None


def telegram_api(method: str, data: dict[str, Any]) -> Any:
    status, payload = _http_post(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", data)
    if payload.get("ok"):
        return payload.get("result")
    code = payload.get("error_code") or status
    # 400 chat not found, 403 bot blocked by the user, 401 bad token: retrying will not help.
    raise ChannelError(f"telegram_{code}", permanent=code in (400, 401, 403, 404))


def telegram_text(chat_id: str, text: str) -> None:
    telegram_api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"})


def send_telegram(user: User, message: dict[str, str]) -> None:
    text = (f"<b>{html.escape(message['lead'])}</b>\n{html.escape(message['headline'])}\n\n"
            f"Открыть: {html.escape(message['url'])}")
    telegram_text(user.telegram_chat_id, text)


_bot_username_cache: dict[str, str] = {}


def telegram_bot_username() -> str:
    if settings.TELEGRAM_BOT_USERNAME:
        return settings.TELEGRAM_BOT_USERNAME
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return ""
    if token not in _bot_username_cache:
        try:
            _bot_username_cache[token] = (telegram_api("getMe", {}) or {}).get("username", "")
        except ChannelError:
            return ""
    return _bot_username_cache[token]


def bitrix_api(method: str, data: dict[str, Any]) -> Any:
    status, payload = _http_post(f"{settings.BITRIX24_WEBHOOK_URL}/{method}.json", data)
    if payload.get("error"):
        code = str(payload["error"])[:60]
        transient = code in {"QUERY_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "OVERLOAD_LIMIT"}
        raise ChannelError(f"bitrix_{code}", permanent=not transient)
    if status >= 400:
        raise ChannelError(f"bitrix_http_{status}", permanent=status in (400, 401, 403, 404))
    return payload.get("result")


def resolve_bitrix_user(user: User) -> str:
    if user.bitrix_user_id:
        return user.bitrix_user_id
    found = bitrix_api("user.get", {"FILTER[EMAIL]": user.email, "FILTER[ACTIVE]": "true"}) or []
    if len(found) != 1:
        raise SkipDelivery("bitrix_user_not_found" if not found else "bitrix_user_ambiguous")
    bitrix_id = str(found[0].get("ID", ""))
    if not bitrix_id.isdigit():
        raise SkipDelivery("bitrix_user_not_found")
    User.objects.filter(pk=user.pk, bitrix_user_id="").update(bitrix_user_id=bitrix_id)
    return bitrix_id


def send_bitrix(user: User, message: dict[str, str], *, tag: str) -> None:
    bitrix_id = resolve_bitrix_user(user)
    text = f"[b]{message['lead']}[/b][br]{message['headline']}[br][url={message['url']}]Открыть обращение[/url]"
    # The same TAG replaces an earlier copy, so a retry after a lost response is not a duplicate.
    bitrix_api("im.notify.system.add", {"USER_ID": bitrix_id, "MESSAGE": text, "TAG": tag})


SENDERS = {"email": lambda user, msg, tag: send_email(user, msg),
           "telegram": lambda user, msg, tag: send_telegram(user, msg),
           "bitrix24": lambda user, msg, tag: send_bitrix(user, msg, tag=tag)}


# --- worker -----------------------------------------------------------------

def _still_relevant(delivery: ExternalDelivery) -> str:
    job = delivery.outbox
    recipient = User.objects.get(pk=job.recipient_id)
    if not recipient.is_active:
        return "recipient_inactive"
    if delivery.channel not in channels_for(recipient):
        return "channel_disabled"
    if not visible_tickets(recipient).filter(pk=job.ticket_id).exists():
        return "access_revoked"
    if job.event.kind == "INFO_REMINDER" and not InfoRequest.objects.filter(
            pk=job.event.payload.get("info_id"), ticket_id=job.ticket_id, answered_at__isnull=True).exists():
        return "question_answered"
    job.recipient = recipient
    return ""


def deliver_external(limit: int = 100) -> dict[str, int]:
    result = {"external_sent": 0, "external_failed": 0, "external_skipped": 0}
    for _ in range(limit):
        now = timezone.now()
        with transaction.atomic():
            candidates = ExternalDelivery.objects.filter(
                Q(state="pending", next_attempt_at__lte=now) | Q(state="processing", locked_until__lt=now)).order_by("pk")
            if connection.features.has_select_for_update_skip_locked:
                candidates = candidates.select_for_update(skip_locked=True)
            else:
                candidates = candidates.select_for_update()
            claimed = candidates.first()
            if claimed is None:
                break
            ExternalDelivery.objects.filter(pk=claimed.pk).update(
                state="processing", locked_until=now + timedelta(minutes=2), attempts=claimed.attempts + 1)
        delivery = ExternalDelivery.objects.select_related("outbox__ticket", "outbox__event").get(pk=claimed.pk)
        update: dict[str, Any] = {"locked_until": None}
        try:
            reason = _still_relevant(delivery)
            if reason:
                raise SkipDelivery(reason)
            SENDERS[delivery.channel](delivery.outbox.recipient, compose(delivery.outbox), f"modus-{delivery.pk}")
        except SkipDelivery as exc:
            update.update(state="skipped", last_error=exc.code)
            result["external_skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - any transport failure is retried with back-off
            code = exc.code if isinstance(exc, ChannelError) else type(exc).__name__
            final = (isinstance(exc, ChannelError) and exc.permanent) or delivery.attempts >= MAX_ATTEMPTS
            update.update(state="failed" if final else "pending", last_error=code[:120],
                          next_attempt_at=timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** delivery.attempts)))
            result["external_failed"] += 1
        else:
            update.update(state="sent", last_error="", sent_at=timezone.now())
            result["external_sent"] += 1
        ExternalDelivery.objects.filter(pk=delivery.pk).update(**update)
    return result


# --- Telegram account linking -------------------------------------------------

def start_telegram_link(user: User) -> str:
    token = secrets.token_urlsafe(24)
    User.objects.filter(pk=user.pk).update(telegram_link_token=token,
                                           telegram_link_expires_at=timezone.now() + TELEGRAM_LINK_TTL)
    user.telegram_link_token, user.telegram_link_expires_at = token, timezone.now() + TELEGRAM_LINK_TTL
    return telegram_link_url(user)


def telegram_link_url(user: User) -> str:
    username = telegram_bot_username()
    active = user.telegram_link_token and user.telegram_link_expires_at and user.telegram_link_expires_at > timezone.now()
    return f"https://t.me/{username}?start={user.telegram_link_token}" if username and active else ""


def _bot_reply(chat_id: str, text: str) -> None:
    try:
        telegram_text(chat_id, text)
    except ChannelError:
        pass


def handle_bot_message(chat_id: str, text: str) -> None:
    command, _, argument = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()
    if command == "/start" and argument.strip():
        with transaction.atomic():
            user = (User.objects.select_for_update()
                    .filter(telegram_link_token=argument.strip(), telegram_link_expires_at__gt=timezone.now(), is_active=True).first())
            if user:
                user.telegram_chat_id, user.telegram_link_token, user.telegram_link_expires_at = chat_id, "", None
                user.notify_telegram = True
                user.save(update_fields=["telegram_chat_id", "telegram_link_token", "telegram_link_expires_at", "notify_telegram"])
        if user:
            _bot_reply(chat_id, f"Готово! Уведомления по обращениям для аккаунта <b>{html.escape(user.get_full_name() or user.username)}</b> "
                                "будут приходить сюда.\nОтключить: /stop")
        else:
            _bot_reply(chat_id, "Ссылка недействительна или устарела. Откройте в системе «Каналы уведомлений» "
                                "и нажмите «Подключить Telegram» ещё раз.")
    elif command == "/stop":
        count = User.objects.filter(telegram_chat_id=chat_id).update(telegram_chat_id="")
        _bot_reply(chat_id, "Уведомления отключены." if count else "Этот чат не был подключён к уведомлениям.")
    else:
        _bot_reply(chat_id, "Я присылаю уведомления системы обратной связи Модус Трейд.\n"
                            "Чтобы подключиться, откройте в системе «Каналы уведомлений» → «Подключить Telegram».")


def poll_telegram(limit: int = 50) -> int:
    """Long-poll-free getUpdates pass; the worker loop already provides the cadence."""
    if not telegram_enabled():
        return 0
    state, _ = IntegrationState.objects.get_or_create(key="telegram_updates")
    offset = int(state.value.get("offset", 0))
    try:
        updates = telegram_api("getUpdates", {"offset": offset, "timeout": 0, "limit": limit,
                                              "allowed_updates": json.dumps(["message"])}) or []
    except ChannelError as exc:
        IntegrationState.objects.filter(pk=state.pk).update(value={"offset": offset, "last_error": exc.code})
        return 0
    for update in updates:
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and message.get("text"):
            handle_bot_message(str(chat["id"]), message["text"])
        # Persist after each update so a crash never replays a processed /start.
        IntegrationState.objects.filter(pk=state.pk).update(value={"offset": offset})
    return len(updates)


def send_test(user: User) -> dict[str, str]:
    """Synchronous check from the settings page; returns channel -> 'ok' or an error code."""
    message = {"subject": "Проверка уведомлений", "headline": "Это тестовое сообщение: канал настроен верно.",
               "lead": "Обратная связь Модус Трейд", "url": settings.SITE_URL + reverse("feedback:notifications"),
               "name": user.get_full_name() or user.username,
               "settings_url": settings.SITE_URL + reverse("feedback:notification_settings")}
    outcome = {}
    for channel in channels_for(user):
        try:
            SENDERS[channel](user, message, f"modus-test-{user.pk}")
            outcome[channel] = "ok"
        except (ChannelError, SkipDelivery) as exc:
            outcome[channel] = exc.code
    return outcome
