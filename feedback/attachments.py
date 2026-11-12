"""Upload validation and storage for request attachments.

Files are checked by size, extension and leading bytes (the declared browser
type is ignored), stored under random names outside any public URL, and served
only through an access-checked view. Callers run inside the business transaction.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Iterable

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from .models import Attachment, Comment, Ticket, User

OOXML = "application/vnd.openxmlformats-officedocument."
# extension -> (served content type, accepted leading-byte signatures; None = text check)
ALLOWED: dict[str, tuple[str, tuple[bytes, ...] | None]] = {
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "webp": ("image/webp", (b"RIFF",)),
    "pdf": ("application/pdf", (b"%PDF-",)),
    "docx": (OOXML + "wordprocessingml.document", (b"PK\x03\x04",)),
    "xlsx": (OOXML + "spreadsheetml.sheet", (b"PK\x03\x04",)),
    "pptx": (OOXML + "presentationml.presentation", (b"PK\x03\x04",)),
    "doc": ("application/msword", (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)),
    "xls": ("application/vnd.ms-excel", (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)),
    "txt": ("text/plain; charset=utf-8", None),
    "csv": ("text/csv; charset=utf-8", None),
}
ACCEPT_ATTRIBUTE = ",".join("." + ext for ext in ALLOWED)
ALLOWED_LABEL = "PNG, JPG, WEBP, PDF, DOCX, XLSX, PPTX, DOC, XLS, TXT, CSV"


class AttachmentError(ValueError):
    pass


@dataclass
class CheckedUpload:
    upload: UploadedFile
    name: str
    content_type: str
    size: int
    sha256: str


def _clean_name(raw: str) -> str:
    name = PurePath(str(raw).replace("\\", "/")).name
    name = "".join(ch for ch in unicodedata.normalize("NFC", name) if unicodedata.category(ch)[0] != "C").strip(" .")
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        name = (stem[:110] + "…." + ext) if stem else name[:120]
    return name or "файл"


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character may be cut at the sampling boundary.
        return exc.start >= len(head) - 3
    return True


def check_uploads(files: Iterable[Any] | None) -> list[CheckedUpload]:
    files = [item for item in (files or []) if item]
    if not files:
        return []
    if len(files) > settings.ATTACHMENT_MAX_FILES:
        raise AttachmentError(f"Можно приложить не более {settings.ATTACHMENT_MAX_FILES} файлов.")
    limit_mb = settings.ATTACHMENT_MAX_BYTES // (1024 * 1024)
    checked = []
    for upload in files:
        name = _clean_name(getattr(upload, "name", ""))
        ext = name.rpartition(".")[2].casefold() if "." in name else ""
        if ext not in ALLOWED:
            raise AttachmentError(f"Файл «{name}»: такой тип не поддерживается. Допустимы {ALLOWED_LABEL}.")
        size = int(getattr(upload, "size", 0) or 0)
        if size == 0:
            raise AttachmentError(f"Файл «{name}» пустой.")
        if size > settings.ATTACHMENT_MAX_BYTES:
            raise AttachmentError(f"Файл «{name}» больше {limit_mb} МБ.")
        content_type, signatures = ALLOWED[ext]
        upload.seek(0)
        head = upload.read(4096)
        if signatures is None:
            valid = _looks_like_text(head)
        else:
            valid = any(head.startswith(signature) for signature in signatures)
            if ext == "webp":
                valid = valid and head[8:12] == b"WEBP"
        if not valid:
            raise AttachmentError(f"Файл «{name}»: содержимое не соответствует расширению .{ext}.")
        digest = hashlib.sha256()
        upload.seek(0)
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        checked.append(CheckedUpload(upload, name, content_type, size, digest.hexdigest()))
    return checked


def store_uploads(ticket: Ticket, actor: User, uploads: list[CheckedUpload], *, visibility: str = "public",
                  comment: Comment | None = None) -> list[Attachment]:
    stored = []
    for item in uploads:
        attachment = Attachment(ticket=ticket, comment=comment, uploaded_by=actor, visibility=visibility,
                                original_name=item.name, content_type=item.content_type, size=item.size, sha256=item.sha256)
        attachment.file.save(item.name, item.upload, save=False)
        attachment.save()
        stored.append(attachment)
    return stored
