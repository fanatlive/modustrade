from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from feedback import channels
from feedback.models import User


class Command(BaseCommand):
    help = "Показывает состояние каналов почты, Telegram и Битрикс24; с --user отправляет тестовое сообщение."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Имя пользователя, которому отправить тестовое сообщение.")

    def handle(self, *args, **options):
        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.stdout.write(f"Почта: {'включена' if channels.email_enabled() else 'выключена'} (backend: {backend}"
                          f"{', папка ' + str(settings.EMAIL_FILE_PATH) if backend == 'filebased' else ''})")
        if channels.telegram_enabled():
            username = channels.telegram_bot_username()
            self.stdout.write(f"Telegram: бот @{username}" if username else "Telegram: токен задан, но бот не отвечает")
        else:
            self.stdout.write("Telegram: не настроен (TELEGRAM_BOT_TOKEN)")
        if channels.bitrix_enabled():
            try:
                channels.bitrix_api("profile", {})
                self.stdout.write("Битрикс24: вебхук отвечает")
            except channels.ChannelError as exc:
                self.stdout.write(f"Битрикс24: ошибка {exc.code}")
        else:
            self.stdout.write("Битрикс24: не настроен (BITRIX24_WEBHOOK_URL)")
        self.stdout.write(f"Ссылки в сообщениях ведут на {settings.SITE_URL}")
        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError("Пользователь не найден.")
            outcome = channels.send_test(user)
            if not outcome:
                self.stdout.write("У пользователя нет подключённых каналов (нужны e-mail или привязка Telegram).")
            for channel, code in outcome.items():
                self.stdout.write(f"  {channel}: {'отправлено' if code == 'ok' else code}")
