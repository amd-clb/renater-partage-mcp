from __future__ import annotations

import imaplib
import re
from contextlib import contextmanager
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from pathlib import Path

from imap_tools import MailBox
from imap_tools.errors import (
    MailboxFolderSelectError,
    MailboxLoginError,
    MailboxLogoutError,
)

from .config import AccountConfig
from .errors import (
    AuthError,
    ConfigError,
    FolderNotFoundError,
    MessageNotFoundError,
    NetworkError,
)
from .models import AttachmentInfo, FolderInfo, MessageSummary, ParsedMessage

_TAG_RE = re.compile(r"<[^>]+>")


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for piece, charset in decode_header(value):
        if isinstance(piece, bytes):
            parts.append(piece.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(piece)
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def _decode_payload_bytes(data: bytes | None, charset: str | None) -> str:
    if not data:
        return ""
    return data.decode(charset or "utf-8", errors="replace")


def _is_attachment(part: Message) -> bool:
    disposition = (part.get("Content-Disposition") or "").lower()
    if "attachment" in disposition:
        return True
    content_type = part.get_content_type()
    if content_type.startswith("text/"):
        return False
    # Non-text leaf parts (pdf, images, etc.) are treated as attachments.
    return not part.is_multipart()


def parse_message(raw: bytes, uid: str | None = None) -> ParsedMessage:
    msg = _from_bytes(raw)

    subject = _decode_header(msg.get("Subject"))
    from_ = _decode_header(msg.get("From"))
    to = _decode_header(msg.get("To"))
    date = msg.get("Date") or ""

    body_text = ""
    body_html = ""
    attachments: list[AttachmentInfo] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if _is_attachment(part):
                payload = part.get_payload(decode=True)
                if payload is not None:
                    filename = _decode_header(part.get_filename()) or "attachment"
                    attachments.append(
                        AttachmentInfo(
                            filename=filename,
                            size=len(payload),
                            content_type=content_type,
                        )
                    )
            elif content_type == "text/plain" and not body_text:
                body_text = _text_of(part)
            elif content_type == "text/html" and not body_html:
                body_html = _text_of(part, keep_html=True)
    else:
        content_type = msg.get_content_type()
        if content_type == "text/html":
            body_html = _text_of(msg, keep_html=True)
        elif content_type == "text/plain":
            body_text = _text_of(msg)

    if not body_text and body_html:
        body_text = _TAG_RE.sub(" ", body_html)
        body_text = re.sub(r"\s+", " ", body_text).strip()

    return ParsedMessage(
        uid=uid,
        subject=subject,
        from_=from_,
        to=to,
        date=date,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )


def _from_bytes(raw: bytes):
    from email import policy as _policy

    return message_from_bytes(raw, policy=_policy.default)


def _text_of(part, keep_html: bool = False) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        # Fall back to non-decoded payload (already-str cases).
        payload_bytes = part.get_payload()
        if isinstance(payload_bytes, str):
            return payload_bytes
        return ""
    charset = part.get_content_charset()
    return _decode_payload_bytes(payload, charset)


class MailConnector:
    def __init__(self, account: AccountConfig):
        self.account = account

    def _connect(self) -> MailBox:
        try:
            box = MailBox(self.account.imap_host, self.account.imap_port)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise NetworkError(
                f"cannot connect to IMAP server {self.account.imap_host}:{self.account.imap_port}: {exc}"
            ) from exc
        try:
            box.login(self.account.email, self.account.password)
        except MailboxLoginError as exc:
            raise AuthError(f"IMAP authentication failed for {self.account.email}") from exc
        except imaplib.IMAP4.error as exc:
            if "AUTHENTICATIONFAILED" in str(exc).upper():
                raise AuthError(f"IMAP authentication failed for {self.account.email}") from exc
            raise NetworkError(f"IMAP connection error: {exc}") from exc
        return box

    @contextmanager
    def _session(self):
        box = self._connect()
        try:
            yield box
        finally:
            try:
                box.logout()
            except (imaplib.IMAP4.error, OSError, MailboxLogoutError):
                pass

    def _select(self, box: MailBox, folder: str) -> None:
        try:
            box.folder.set(folder)
        except MailboxFolderSelectError as exc:
            raise FolderNotFoundError(f"folder not found: {folder}") from exc

    @staticmethod
    def _summary(message) -> MessageSummary:
        try:
            date = message.date.isoformat()
        except (TypeError, ValueError):
            date = getattr(message, "date_str", "") or ""
        return MessageSummary(
            uid=str(message.uid),
            date=date,
            from_=message.from_ or "",
            subject=message.subject or "",
            snippet=(message.text or "")[:200],
            flags=list(message.flags or ()),
        )

    def list_folders(self) -> list[FolderInfo]:
        with self._session() as box:
            result = []
            for folder in box.folder.list():
                status = box.folder.status(folder.name) or {}
                result.append(
                    FolderInfo(
                        name=folder.name,
                        message_count=int(status.get("MESSAGES", 0)),
                        flags=list(folder.flags or ()),
                    )
                )
            return result

    def list_messages(
        self,
        folder: str = "INBOX",
        query: str | None = None,
        unread_only: bool = False,
        limit: int = 20,
    ) -> list[MessageSummary]:
        limit = max(1, min(int(limit), 100))
        criteria = query if query else ("UNSEEN" if unread_only else "ALL")
        with self._session() as box:
            self._select(box, folder)
            messages = list(box.fetch(criteria, limit=limit, mark_seen=False, reverse=True))
            return [self._summary(m) for m in messages]

    def search_messages(self, folder: str, query: str, limit: int = 20) -> list[MessageSummary]:
        return self.list_messages(folder=folder, query=query, limit=limit)

    def read_message(
        self,
        folder: str,
        uid: str,
        include_body: bool = True,
        include_attachments: bool = True,
    ) -> ParsedMessage:
        with self._session() as box:
            self._select(box, folder)
            messages = list(box.fetch(uid_list=uid, mark_seen=False))
            if not messages:
                raise MessageNotFoundError(f"message not found: uid {uid} in folder {folder}")
            message = messages[0]
            try:
                date = message.date.isoformat()
            except (TypeError, ValueError):
                date = getattr(message, "date_str", "") or ""
            attachments = (
                [
                    AttachmentInfo(
                        filename=att.filename or "attachment",
                        size=int(att.size),
                        content_type=att.content_type or "application/octet-stream",
                    )
                    for att in message.attachments
                ]
                if include_attachments
                else []
            )
            return ParsedMessage(
                uid=str(message.uid),
                subject=message.subject or "",
                from_=message.from_ or "",
                to=message.to or "",
                date=date,
                body_text=(message.text or "") if include_body else "",
                body_html=(message.html or "") if include_body else "",
                attachments=attachments,
            )

    def download_attachment(
        self,
        folder: str,
        uid: str,
        filename: str,
        dest_dir: Path = Path("downloads"),
    ) -> Path:
        with self._session() as box:
            self._select(box, folder)
            messages = list(box.fetch(uid_list=uid, mark_seen=False))
            if not messages:
                raise MessageNotFoundError(f"message not found: uid {uid} in folder {folder}")
            message = messages[0]
            attachment = next(
                (att for att in message.attachments if att.filename == filename), None
            )
            if attachment is None:
                raise MessageNotFoundError(f"attachment not found: {filename}")
            safe_name = sanitize_filename(filename)
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            target = (dest / safe_name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ConfigError(f"refusing to write outside destination directory: {target}")
            target.write_bytes(attachment.payload)
            return target


def sanitize_filename(name: str) -> str:
    # Drop directory components from both path conventions.
    name = name.replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    # Remove null bytes and collapse any remaining "..".
    name = name.replace("\x00", "")
    while ".." in name:
        name = name.replace("..", ".")
    name = name.strip(" .")
    if not name:
        name = "attachment"
    # Cap length while preserving one dot-extension.
    if len(name) > 150:
        stem, _, ext = name.rpartition(".")
        if stem and ext and len(ext) <= 20:
            name = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            name = name[:150]
    return name
