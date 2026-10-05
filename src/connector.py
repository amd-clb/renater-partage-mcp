from __future__ import annotations

import re
from email import policy
from email.header import decode_header
from email.message import Message

from .models import AttachmentInfo, ParsedMessage

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
    from email import message_from_bytes
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
