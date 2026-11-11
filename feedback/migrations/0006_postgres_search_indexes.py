"""PostgreSQL-only physical indexes; SQLite remains a small local demo backend."""
from django.db import migrations


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    schema_editor.execute("""CREATE INDEX ticket_text_search_gin ON feedback_ticket USING GIN
        ((to_tsvector('russian'::regconfig, COALESCE(description, '')) ||
          to_tsvector('russian'::regconfig, COALESCE(proposed_solution, ''))))""")
    schema_editor.execute('CREATE INDEX ticket_number_trgm ON feedback_ticket USING GIN (UPPER(number) gin_trgm_ops)')
    schema_editor.execute('CREATE INDEX ticket_source_id_trgm ON feedback_ticket USING GIN (UPPER(source_record_id) gin_trgm_ops)')


def remove_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        for name in ('ticket_text_search_gin', 'ticket_number_trgm', 'ticket_source_id_trgm'):
            schema_editor.execute('DROP INDEX IF EXISTS '+name)
    # pg_trgm may be used elsewhere; never drop the shared extension.


class Migration(migrations.Migration):
    dependencies = [('feedback', '0005_info_reminder_schedule')]
    operations = [migrations.RunPython(create_indexes, remove_indexes)]
