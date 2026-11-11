"""Seed a real system category independently from demo data and preserve old links."""
from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def initialize_system_data(apps, schema_editor):
    alias = schema_editor.connection.alias
    category = apps.get_model("feedback", "Category")
    category.objects.using(alias).get_or_create(
        code="unknown", defaults={"name": "Не знаю", "active": True},
    )
    # Old releases kept the duplicate link after reopening a card. Preserve the
    # relation as history, while excluding it from the active duplicate graph.
    relation = apps.get_model("feedback", "TicketRelation")
    relation.objects.using(alias).filter(active=True).exclude(
        Q(ticket__status="CLOSED", ticket__resolution_code="DUPLICATE")
    ).update(active=False, archived_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("feedback", "0003_workerheartbeat_inforequest_reminder_delivered_at_and_more")]
    operations = [migrations.RunPython(initialize_system_data, migrations.RunPython.noop)]
