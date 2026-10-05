from dataclasses import dataclass, field


@dataclass
class FolderInfo:
    name: str
    message_count: int
    flags: list[str] = field(default_factory=list)


@dataclass
class MessageSummary:
    uid: str
    date: str
    from_: str
    subject: str
    snippet: str
    flags: list[str] = field(default_factory=list)


@dataclass
class AttachmentInfo:
    filename: str
    size: int
    content_type: str


@dataclass
class ParsedMessage:
    uid: str | None
    subject: str
    from_: str
    to: str
    date: str
    body_text: str
    body_html: str
    attachments: list[AttachmentInfo] = field(default_factory=list)
