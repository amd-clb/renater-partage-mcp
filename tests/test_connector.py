"""Tests for MailConnector read operations against a fake imap_tools.MailBox."""
import imaplib
from datetime import UTC, datetime, timedelta

import pytest
from imap_tools.errors import MailboxFolderSelectError

from src import connector
from src.config import AccountConfig
from src.errors import AuthError, FolderNotFoundError, MessageNotFoundError


def folder_select_error(detail: str) -> MailboxFolderSelectError:
    return MailboxFolderSelectError(("NO", [detail.encode()]), "OK")


def account() -> AccountConfig:
    return AccountConfig(
        email="prenom.nom@example.edu",
        imap_host="mail.example.edu",
        smtp_host="mail.example.edu",
        imap_port=993,
        smtp_port=465,
        password="secret",
    )


class FakeAttachment:
    def __init__(self, filename: str, payload: bytes, content_type: str):
        self.filename = filename
        self.payload = payload
        self.content_type = content_type
        self.size = len(payload)


class FakeMessage:
    def __init__(self, uid, subject, from_, to, date, flags, text, html="", attachments=None):
        self.uid = uid
        self.subject = subject
        self.from_ = from_
        self.to = to
        self.date = date
        self.flags = tuple(flags)
        self.text = text
        self.html = html
        self.attachments = attachments or []


class FakeFolder:
    def __init__(self, name, flags=()):
        self.name = name
        self.flags = tuple(flags)
        self.delim = "/"


class FakeFolderManager:
    def __init__(self, state):
        self._state = state

    def list(self, folder="", search_args="*", subscribed_only=False):
        return [FakeFolder(name) for name in self._state["folders"]]

    def status(self, folder=None, options=None):
        name = folder or self._state["current"]
        return {"MESSAGES": len(self._state["folders"][name])}

    def set(self, folder, readonly=False):
        if folder not in self._state["folders"]:
            raise folder_select_error(f"NO folder not found: {folder}")
        self._state["current"] = folder
        return ("OK",)

    def exists(self, folder):
        return folder in self._state["folders"]

    def create(self, folder):
        if "/" in folder:
            parent = folder.rsplit("/", 1)[0]
            if parent not in self._state["folders"]:
                raise folder_select_error(f"NO folder not found: {parent}")
        self._state["folders"].setdefault(folder, [])
        return ("OK",)

    def rename(self, old_name, new_name):
        if old_name not in self._state["folders"]:
            raise folder_select_error(f"NO folder not found: {old_name}")
        self._state["folders"][new_name] = self._state["folders"].pop(old_name)
        return ("OK",)

    def delete(self, folder):
        if folder not in self._state["folders"]:
            raise folder_select_error(f"NO folder not found: {folder}")
        if self._state["folders"][folder]:
            raise folder_select_error(f"NO folder not empty: {folder}")
        del self._state["folders"][folder]
        return ("OK",)


def make_fake_mailbox(monkeypatch, folders, fail_auth=False):
    state = {"folders": folders, "current": "INBOX", "fail_auth": fail_auth, "instances": []}

    class FakeMailBox:
        def __init__(self, host="", port=993, *args, **kwargs):
            self.host = host
            self.port = port
            self.folder = FakeFolderManager(state)
            self.logged_in = None
            self.logged_out = False
            self.fetch_calls = []
            state["instances"].append(self)

        def login(self, username, password, initial_folder="INBOX"):
            if state["fail_auth"]:
                raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            self.logged_in = (username, password)
            self.folder.set(initial_folder)
            return self

        def fetch(self, criteria="ALL", charset="US-ASCII", *, limit=None, mark_seen=True,
                  reverse=False, headers_only=False, uid_list=None, **kwargs):
            self.fetch_calls.append({
                "criteria": criteria, "limit": limit, "mark_seen": mark_seen,
                "reverse": reverse, "uid_list": uid_list,
            })
            msgs = list(state["folders"][state["current"]])
            if uid_list is not None:
                wanted = {str(u) for u in (uid_list if isinstance(uid_list, (list, tuple)) else [uid_list])}
                msgs = [m for m in msgs if str(m.uid) in wanted]
            elif criteria == "UNSEEN":
                msgs = [m for m in msgs if "\\Seen" not in m.flags]
            if reverse:
                msgs = sorted(msgs, key=lambda m: m.date, reverse=True)
            if isinstance(limit, int):
                msgs = msgs[:limit]
            return iter(msgs)

        def logout(self):
            self.logged_out = True
            return ("BYE",)

    monkeypatch.setattr(connector, "MailBox", FakeMailBox)
    return state


def inbox_with_10():
    msgs = []
    for i in range(1, 11):
        seen = i <= 7
        msgs.append(FakeMessage(
            uid=str(i),
            subject=f"Msg {i}",
            from_="sender{i}@example.fr",
            to="prenom.nom@example.edu",
            date=datetime(2026, 10, 1, 8, i, tzinfo=UTC),
            flags=("\\Seen",) if seen else (),
            text=f"body of message {i} " + "x" * 300,
        ))
    return {"INBOX": msgs, "Sent": [
        FakeMessage("100", "Sent one", "me@example.edu", "x@y.fr",
                    datetime(2026, 9, 1, 12, 0, tzinfo=UTC), ("\\Seen",), "sent body"),
        FakeMessage("101", "Sent two", "me@example.edu", "x@y.fr",
                    datetime(2026, 9, 2, 12, 0, tzinfo=UTC), (), "sent body 2"),
    ]}


def test_list_folders_returns_counts(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    connector.MailConnector(account()).list_folders()
    result = connector.MailConnector(account()).list_folders()
    by_name = {f.name: f for f in result}
    assert set(by_name) == {"INBOX", "Sent"}
    assert by_name["INBOX"].message_count == 10
    assert by_name["Sent"].message_count == 2


def test_list_messages_limit_clamped_to_100(monkeypatch):
    folders = {"INBOX": [
        FakeMessage(str(i), f"S{i}", "a@b.fr", "c@d.fr",
                    datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i), (), "t")
        for i in range(150)
    ]}
    state = make_fake_mailbox(monkeypatch, folders)
    result = connector.MailConnector(account()).list_messages("INBOX", limit=500)
    assert len(result) == 100
    assert state["instances"][0].fetch_calls[0]["limit"] == 100


def test_list_messages_unread_only_filters(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    result = connector.MailConnector(account()).list_messages("INBOX", unread_only=True)
    assert len(result) == 3
    assert all("\\Seen" not in m.flags for m in result)
    assert {m.uid for m in result} == {"8", "9", "10"}


def test_list_messages_missing_folder_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(FolderNotFoundError):
        connector.MailConnector(account()).list_messages("Nope")


def test_read_message_returns_parsed(monkeypatch):
    pdf = b"%PDF-1.4 data"
    folders = {"INBOX": [
        FakeMessage(
            "42", "Rapport", "a@b.fr", "c@d.fr",
            datetime(2026, 10, 2, 9, 0, tzinfo=UTC), (),
            text="voir PJ",
            attachments=[FakeAttachment("rapport.pdf", pdf, "application/pdf")],
        ),
    ]}
    make_fake_mailbox(monkeypatch, folders)
    parsed = connector.MailConnector(account()).read_message("INBOX", "42")
    assert parsed.uid == "42"
    assert parsed.subject == "Rapport"
    assert parsed.body_text == "voir PJ"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "rapport.pdf"
    assert parsed.attachments[0].size == len(pdf)


def test_read_message_unknown_uid_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).read_message("INBOX", "999")


def test_auth_failure_raises_auth_error(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10(), fail_auth=True)
    with pytest.raises(AuthError) as excinfo:
        connector.MailConnector(account()).list_folders()
    assert ".env" in excinfo.value.hint


def _attachment_folders(tmp_path):
    pdf = b"%PDF-1.4 evil"
    return {"INBOX": [
        FakeMessage(
            "7", "Doc", "a@b.fr", "c@d.fr",
            datetime(2026, 10, 3, 9, 0, tzinfo=UTC), (),
            text="fichier",
            attachments=[FakeAttachment("../../evil.pdf", pdf, "application/pdf")],
        ),
    ]}


def test_download_attachment_writes_under_dest_dir(monkeypatch, tmp_path):
    make_fake_mailbox(monkeypatch, _attachment_folders(tmp_path))
    result = connector.MailConnector(account()).download_attachment(
        "INBOX", "7", "../../evil.pdf", dest_dir=tmp_path)
    assert result.parent == tmp_path
    assert result.name == "evil.pdf"
    assert result.read_bytes() == b"%PDF-1.4 evil"


def test_download_attachment_unknown_filename_raises(monkeypatch, tmp_path):
    make_fake_mailbox(monkeypatch, _attachment_folders(tmp_path))
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).download_attachment(
            "INBOX", "7", "nope.pdf", dest_dir=tmp_path)
