from __future__ import annotations

import imaplib
import mimetypes
import re
from contextlib import contextmanager
from email import encoders, message_from_bytes
from email.header import Header, decode_header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from smtplib import SMTP_SSL, SMTPException

from imap_tools import MailBox
from imap_tools.errors import (
    MailboxFolderCreateError,
    MailboxFolderDeleteError,
    MailboxFolderRenameError,
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
    SendError,
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

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> None:
        cc = list(cc or [])
        files = [Path(path) for path in (attachments or [])]
        for path in files:
            if not path.is_file():
                raise ConfigError(f"attachment file not found: {path}")
        if files:
            message = MIMEMultipart("mixed")
            message.attach(MIMEText(body, "plain", "utf-8"))
            for path in files:
                message.attach(self._build_attachment(path))
        else:
            message = MIMEText(body, "plain", "utf-8")
        message["From"] = self.account.email
        message["To"] = to
        if cc:
            message["Cc"] = ", ".join(cc)
        try:
            subject.encode("ascii")
            message["Subject"] = subject
        except UnicodeEncodeError:
            message["Subject"] = Header(subject, "utf-8")
        try:
            with SMTP_SSL(self.account.smtp_host, self.account.smtp_port) as server:
                server.login(self.account.email, self.account.password)
                server.sendmail(self.account.email, [to, *cc], message.as_string())
        except SMTPException as exc:
            raise SendError(f"SMTP error while sending message: {exc}") from exc

    @staticmethod
    def _build_attachment(path: Path) -> MIMEBase:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        major, _, minor = content_type.partition("/")
        part = MIMEBase(major, minor or "octet-stream")
        part.set_payload(path.read_bytes())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", "attachment", filename=sanitize_filename(path.name)
        )
        return part

    def create_folder(self, name: str) -> None:
        with self._session() as box:
            if "/" in name:
                parent = name.rsplit("/", 1)[0]
                if not box.folder.exists(parent):
                    raise FolderNotFoundError(f"parent folder not found: {parent}")
            if not box.folder.exists(name):
                try:
                    box.folder.create(name)
                except MailboxFolderCreateError:
                    pass

    def rename_folder(self, old_name: str, new_name: str) -> None:
        with self._session() as box:
            if not box.folder.exists(old_name):
                raise FolderNotFoundError(f"folder not found: {old_name}")
            try:
                box.folder.rename(old_name, new_name)
            except MailboxFolderRenameError as exc:
                raise FolderNotFoundError(f"cannot rename folder {old_name}: {exc}") from exc

    def delete_folder(self, name: str) -> None:
        with self._session() as box:
            if not box.folder.exists(name):
                raise FolderNotFoundError(f"folder not found: {name}")
            try:
                box.folder.delete(name)
            except MailboxFolderDeleteError as exc:
                raise FolderNotFoundError(
                    f"cannot delete folder {name} (not empty or in use): {exc}"
                ) from exc

    def move_message(self, folder: str, uid: str, destination_folder: str) -> str:
        with self._session() as box:
            self._select(box, folder)
            if not self._uid_exists(box, uid):
                raise MessageNotFoundError(f"message not found: uid {uid} in folder {folder}")
            if not box.folder.exists(destination_folder):
                raise FolderNotFoundError(f"folder not found: {destination_folder}")
            result = box.move(uid, destination_folder)
            return self._new_uid_from_move(result, uid)

    def delete_message(self, folder: str, uid: str) -> None:
        with self._session() as box:
            self._select(box, folder)
            if not self._uid_exists(box, uid):
                raise MessageNotFoundError(f"message not found: uid {uid} in folder {folder}")
            box.delete(uid)

    def mark_read(self, folder: str, uid: str) -> None:
        self._set_seen(folder, uid, seen=True)

    def mark_unread(self, folder: str, uid: str) -> None:
        self._set_seen(folder, uid, seen=False)

    def _set_seen(self, folder: str, uid: str, *, seen: bool) -> None:
        with self._session() as box:
            self._select(box, folder)
            if not self._uid_exists(box, uid):
                raise MessageNotFoundError(f"message not found: uid {uid} in folder {folder}")
            box.flag(uid, "\\Seen", seen)

    @staticmethod
    def _uid_exists(box: MailBox, uid: str) -> bool:
        return bool(list(box.fetch(uid_list=uid, mark_seen=False)))

    @staticmethod
    def _new_uid_from_move(move_result, original_uid: str) -> str:
        if move_result:
            try:
                line = move_result[0][0][1][0]
                parts = line.split()
                if len(parts) >= 2:
                    return parts[-1].decode("utf-8", "replace")
            except (IndexError, TypeError, AttributeError):
                pass
        return original_uid


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
