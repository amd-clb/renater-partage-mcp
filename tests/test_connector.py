"""Tests for MailConnector read + send + management against fakes."""
import imaplib
import smtplib
from datetime import UTC, datetime, timedelta
from email import message_from_string

import pytest
from imap_tools.errors import (
    MailboxFolderCreateError,
    MailboxFolderDeleteError,
    MailboxFolderSelectError,
)

from src import connector
from src.config import AccountConfig
from src.errors import (
    AuthError,
    ConfigError,
    FolderNotFoundError,
    MessageNotFoundError,
    SendError,
)


def folder_select_error(detail: str) -> MailboxFolderSelectError:
    return MailboxFolderSelectError(("NO", [detail.encode()]), "OK")


def folder_delete_error(detail: str) -> MailboxFolderDeleteError:
    return MailboxFolderDeleteError(("NO", [detail.encode()]), "OK")


def make_fake_smtp(monkeypatch, fail=False):
    state = {"fail": fail, "sent": [], "login": None}

    class FakeSMTPSSL:
        def __init__(self, host, port, *args, **kwargs):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            state["login"] = (user, password)

        def sendmail(self, sender, recipients, message):
            if state["fail"]:
                raise smtplib.SMTPException("550 5.7.1 relay denied")
            state["sent"].append((sender, list(recipients), message))

    monkeypatch.setattr(connector, "SMTP_SSL", FakeSMTPSSL)
    return state


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
        if folder in self._state["folders"]:
            raise MailboxFolderCreateError(("NO", [f"mailbox exists: {folder}".encode()]), "OK")
        if "/" in folder:
            parent = folder.rsplit("/", 1)[0]
            if parent not in self._state["folders"]:
                raise folder_select_error(f"NO folder not found: {parent}")
        self._state["folders"][folder] = []
        return ("OK",)

    def rename(self, old_name, new_name):
        if old_name not in self._state["folders"]:
            raise folder_select_error(f"NO folder not found: {old_name}")
        self._state["folders"][new_name] = self._state["folders"].pop(old_name)
        return ("OK",)

    def delete(self, folder):
        if folder not in self._state["folders"]:
            raise folder_delete_error(f"NO folder not found: {folder}")
        if self._state["folders"][folder]:
            raise folder_delete_error(f"NO folder not empty: {folder}")
        del self._state["folders"][folder]
        return ("OK",)


def make_fake_mailbox(monkeypatch, folders, fail_auth=False):
    state = {
        "folders": folders,
        "current": "INBOX",
        "fail_auth": fail_auth,
        "instances": [],
        "flag_calls": [],
        "move_calls": [],
        "next_move_uid": 999,
    }

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

        def move(self, uid_list, destination_folder, chunks=None):
            uids = {str(u) for u in (uid_list if isinstance(uid_list, (list, tuple)) else [uid_list])}
            src = state["folders"][state["current"]]
            lines = []
            for m in list(src):
                if str(m.uid) in uids:
                    old = str(m.uid)
                    src.remove(m)
                    new_uid = str(state["next_move_uid"])
                    state["next_move_uid"] += 1
                    m.uid = new_uid
                    state["folders"].setdefault(destination_folder, []).append(m)
                    lines.append(f"{old} {new_uid}".encode())
            state["move_calls"].append((sorted(uids), destination_folder))
            return [(("OK", lines), ("_MOVE",))] if lines else None

        def delete(self, uid_list, chunks=None):
            uids = {str(u) for u in (uid_list if isinstance(uid_list, (list, tuple)) else [uid_list])}
            src = state["folders"][state["current"]]
            state["folders"][state["current"]] = [m for m in src if str(m.uid) not in uids]
            return [(("OK", None), ("OK", None))]

        def flag(self, uid_list, flag_set, value, chunks=None):
            uids = {str(u) for u in (uid_list if isinstance(uid_list, (list, tuple)) else [uid_list])}
            for m in state["folders"][state["current"]]:
                if str(m.uid) in uids:
                    if value:
                        if flag_set not in m.flags:
                            m.flags = m.flags + (flag_set,)
                    else:
                        m.flags = tuple(f for f in m.flags if f != flag_set)
            state["flag_calls"].append((sorted(uids), flag_set, value))
            return [(("OK", None),)]

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


# --- Task 4: send + management ------------------------------------------


def test_send_message_simple(monkeypatch):
    state = make_fake_smtp(monkeypatch)
    result = connector.MailConnector(account()).send_message(
        "dest@example.fr", "Bonjour", "Corps du message")
    assert result is None
    assert state["login"] == ("prenom.nom@example.edu", "secret")
    sender, recipients, raw = state["sent"][0]
    assert sender == "prenom.nom@example.edu"
    assert recipients == ["dest@example.fr"]
    msg = message_from_string(raw)
    assert msg["From"] == "prenom.nom@example.edu"
    assert msg["To"] == "dest@example.fr"
    assert msg["Subject"] == "Bonjour"
    assert msg["MIME-Version"] == "1.0"
    assert "Corps du message" in (msg.get_payload(decode=True) or b"").decode()


def test_send_message_with_cc_and_attachment(monkeypatch, tmp_path):
    state = make_fake_smtp(monkeypatch)
    att_file = tmp_path / "f.txt"
    att_file.write_bytes(b"contenu du fichier")
    connector.MailConnector(account()).send_message(
        "dest@example.fr", "Doc", "Voir PJ",
        cc=["cc1@example.fr", "cc2@example.fr"],
        attachments=[att_file],
    )
    _, recipients, raw = state["sent"][0]
    assert recipients == ["dest@example.fr", "cc1@example.fr", "cc2@example.fr"]
    msg = message_from_string(raw)
    assert msg["Cc"] == "cc1@example.fr, cc2@example.fr"
    att_parts = [
        p for p in msg.walk()
        if p.get("Content-Disposition", "").startswith("attachment")
    ]
    assert len(att_parts) == 1
    assert att_parts[0].get_filename() == "f.txt"
    assert att_parts[0].get_payload(decode=True) == b"contenu du fichier"
    assert "Voir PJ" in (msg.get_payload(0).get_payload(decode=True) or b"").decode()


def test_send_message_missing_attachment_raises(monkeypatch, tmp_path):
    make_fake_smtp(monkeypatch)
    with pytest.raises(ConfigError, match=str(tmp_path / "nope.pdf")):
        connector.MailConnector(account()).send_message(
            "dest@example.fr", "S", "B", attachments=[tmp_path / "nope.pdf"])


def test_send_smtp_failure_raises_send_error(monkeypatch):
    make_fake_smtp(monkeypatch, fail=True)
    with pytest.raises(SendError):
        connector.MailConnector(account()).send_message("dest@example.fr", "S", "B")


def test_create_nested_folder_creates_parent(monkeypatch):
    folders = inbox_with_10()
    folders["A"] = []
    state = make_fake_mailbox(monkeypatch, folders)
    connector.MailConnector(account()).create_folder("A/B")
    assert "A/B" in state["folders"]


def test_create_folder_missing_parent_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(FolderNotFoundError):
        connector.MailConnector(account()).create_folder("Nope/B")


def test_delete_non_empty_folder_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(FolderNotFoundError, match="not empty|non-vide|empty"):
        connector.MailConnector(account()).delete_folder("INBOX")


def test_move_message_returns_new_uid(monkeypatch):
    state = make_fake_mailbox(monkeypatch, inbox_with_10())
    new_uid = connector.MailConnector(account()).move_message("INBOX", "3", "Sent")
    assert new_uid == "999"
    assert any(m.uid == "999" for m in state["folders"]["Sent"])
    assert not any(m.uid == "3" for m in state["folders"]["INBOX"])


def test_move_message_missing_dest_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(FolderNotFoundError):
        connector.MailConnector(account()).move_message("INBOX", "3", "Nope")


def test_move_message_missing_uid_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).move_message("INBOX", "999", "Sent")


def test_delete_message_missing_uid_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).delete_message("INBOX", "999")


def test_delete_message_removes_message(monkeypatch):
    state = make_fake_mailbox(monkeypatch, inbox_with_10())
    connector.MailConnector(account()).delete_message("INBOX", "5")
    assert not any(m.uid == "5" for m in state["folders"]["INBOX"])


def test_mark_read_uses_set_flag(monkeypatch):
    state = make_fake_mailbox(monkeypatch, inbox_with_10())
    mc = connector.MailConnector(account())
    mc.mark_read("INBOX", "8")
    assert (["8"], "\\Seen", True) in state["flag_calls"]
    msg8 = next(m for m in state["folders"]["INBOX"] if m.uid == "8")
    assert "\\Seen" in msg8.flags
    mc.mark_unread("INBOX", "8")
    assert (["8"], "\\Seen", False) in state["flag_calls"]
    assert "\\Seen" not in msg8.flags


def test_mark_read_missing_uid_raises(monkeypatch):
    make_fake_mailbox(monkeypatch, inbox_with_10())
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).mark_read("INBOX", "999")
    with pytest.raises(MessageNotFoundError):
        connector.MailConnector(account()).mark_unread("INBOX", "999")
