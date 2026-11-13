"""The Pavlodar synthetic set goes through real commands and loads idempotently."""
from collections import Counter
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from feedback.models import Ticket, User


@override_settings(DEBUG=True, DEMO_MODE=True)
class PavlodarDemoTests(TestCase):
    def test_twenty_cards_with_real_history_and_no_duplicates(self):
        call_command('load_demo', stdout=StringIO())
        call_command('load_pavlodar_demo', stdout=StringIO())
        cards = Ticket.objects.filter(source_record_id__startswith='PVL-')
        self.assertEqual(cards.count(), 20)
        self.assertTrue(all(card.is_demo for card in cards))
        self.assertEqual(Counter(cards.values_list('status', flat=True)),
                         Counter({'REVIEW': 6, 'NEW': 5, 'CLOSED': 4, 'IN_PROGRESS': 3, 'WAITING_INFO': 2}))
        implemented = cards.filter(resolution_code='IMPLEMENTED')
        self.assertEqual(implemented.count(), 3)
        self.assertTrue(all(card.events.filter(kind='IMPLEMENT').exists() for card in implemented))
        self.assertEqual(cards.filter(resolution_code='DECLINED').count(), 1)
        self.assertEqual(User.objects.get(username='pvl_sklad').org_unit.name, 'Филиал Павлодар')

        out = StringIO()
        call_command('load_pavlodar_demo', stdout=out)
        self.assertIn('создано 0, уже было 20', out.getvalue())
        self.assertEqual(Ticket.objects.filter(source_record_id__startswith='PVL-').count(), 20)
