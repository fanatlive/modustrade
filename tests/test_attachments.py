"""Attachments: validation, storage outside public URLs and access checks on download."""
import shutil
import tempfile
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from feedback.models import Attachment, Ticket
from feedback.services import DomainError, execute_command
from tests import test_workflow as workflow

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%test\n"


def upload(name, content, kind="application/octet-stream"):
    return SimpleUploadedFile(name, content, content_type=kind)


class AttachmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        workflow.WorkflowTests.setUpTestData.__func__(cls)
    make_ticket = workflow.WorkflowTests.make_ticket
    command = workflow.WorkflowTests.command

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.override = override_settings(ATTACHMENTS_ROOT=self.root, DEBUG=True, DEMO_MODE=False)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.root, ignore_errors=True)

    def submit(self, files, key=None, **extra):
        self.client.force_login(self.author)
        data = {'request_type': 'PROBLEM', 'category': self.category.pk, 'description': 'Ошибка в отчёте закупок, пример во вложении.',
                'reported_priority': 'MEDIUM', 'confidentiality': 'STANDARD', 'idempotency_key': key or str(uuid4()),
                'attachments': files, **extra}
        return self.client.post('/requests/new/', data)

    def comment(self, user, ticket, files, visibility='public'):
        ticket.refresh_from_db()
        self.client.force_login(user)
        return self.client.post(f'/requests/{ticket.pk}/command/', {'command': 'comment', 'version': ticket.version,
            'message': 'Добавляю материалы', 'visibility': visibility, 'attachments': files})

    def test_submission_stores_files_and_author_downloads(self):
        response = self.submit([upload('экран ошибки.png', PNG), upload('пример.pdf', PDF)])
        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get()
        files = list(ticket.attachments.order_by('original_name'))
        self.assertEqual([f.original_name for f in files], ['пример.pdf', 'экран ошибки.png'])
        self.assertNotIn('экран', files[1].file.name)  # random on-disk name
        page = self.client.get(f'/requests/{ticket.pk}/')
        self.assertContains(page, 'экран ошибки.png')
        download = self.client.get(f'/attachments/{files[0].pk}/')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b''.join(download.streaming_content), PDF)
        self.assertIn('attachment;', download['Content-Disposition'])
        self.assertIn('sandbox', download['Content-Security-Policy'])
        self.assertEqual(download['X-Content-Type-Options'], 'nosniff')
        inline = self.client.get(f'/attachments/{files[1].pk}/?inline=1')
        self.assertIn('inline', inline['Content-Disposition'])
        self.assertIn('attachment;', self.client.get(f'/attachments/{files[0].pk}/?inline=1')['Content-Disposition'])

    def test_invalid_files_reject_whole_submission(self):
        cases = [
            [upload('скрин.png', b'<script>alert(1)</script>')],          # content does not match extension
            [upload('page.html', b'<html></html>')],                       # type not allowed
            [upload('logo.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>')],
            [upload('empty.pdf', b'')],
            [upload(f'{i}.pdf', PDF) for i in range(4)],                   # more than three
        ]
        for files in cases:
            with self.subTest(files=[f.name for f in files]):
                response = self.submit(files)
                self.assertEqual(response.status_code, 422)
        with override_settings(ATTACHMENT_MAX_BYTES=10):
            self.assertEqual(self.submit([upload('big.pdf', PDF)]).status_code, 422)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_replayed_submission_does_not_duplicate_files(self):
        key = str(uuid4())
        self.submit([upload('a.pdf', PDF)], key=key)
        self.submit([upload('a.pdf', PDF)], key=key)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_download_requires_access_to_ticket(self):
        self.submit([upload('a.pdf', PDF)])
        attachment = Attachment.objects.get()
        self.client.logout()
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 404)
        self.client.force_login(self.processor)
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 200)

    def test_restricted_ticket_files_need_clearance(self):
        self.submit([upload('secret.pdf', PDF)], confidentiality='RESTRICTED')
        attachment = Attachment.objects.get()
        self.client.force_login(self.processor)
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 404)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 200)

    def test_internal_note_files_hidden_from_author(self):
        ticket = self.make_ticket()
        self.command(ticket, 'start')
        self.assertEqual(self.comment(self.processor, ticket, [upload('внутренний.pdf', PDF)], 'internal').status_code, 302)
        attachment = Attachment.objects.get()
        self.assertEqual(attachment.visibility, 'internal')
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(f'/requests/{ticket.pk}/'), 'внутренний.pdf')
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 404)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(f'/attachments/{attachment.pk}/').status_code, 200)

    def test_author_message_with_file_reaches_handler(self):
        ticket = self.make_ticket()
        self.assertEqual(self.comment(self.author, ticket, [upload('уточнение.png', PNG)]).status_code, 302)
        self.client.force_login(self.processor)
        self.assertContains(self.client.get(f'/requests/{ticket.pk}/'), 'уточнение.png')
        self.assertEqual(ticket.events.order_by('-sequence').first().payload.get('attachments'), 1)

    def test_files_only_accepted_with_messages(self):
        ticket = self.make_ticket()
        with self.assertRaises(DomainError):
            execute_command(actor=self.processor, ticket_id=ticket.pk, command='start', expected_version=ticket.version,
                            data={'files': [upload('a.pdf', PDF)]})
        with self.assertRaises(PermissionDenied):
            execute_command(actor=self.other, ticket_id=ticket.pk, command='comment', expected_version=ticket.version,
                            data={'message': 'Чужое', 'files': [upload('a.pdf', PDF)]})
        self.assertEqual(Attachment.objects.count(), 0)
