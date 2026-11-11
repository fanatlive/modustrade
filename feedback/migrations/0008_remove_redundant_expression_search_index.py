from django.db import migrations


def remove_old(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP INDEX IF EXISTS ticket_text_search_gin')


def restore_old(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute("""CREATE INDEX IF NOT EXISTS ticket_text_search_gin ON feedback_ticket USING GIN
            ((to_tsvector('russian'::regconfig, COALESCE(description, '')) ||
              to_tsvector('russian'::regconfig, COALESCE(proposed_solution, ''))))""")


class Migration(migrations.Migration):
    dependencies = [('feedback', '0007_generated_search_document')]
    operations = [migrations.RunPython(remove_old, restore_old)]
