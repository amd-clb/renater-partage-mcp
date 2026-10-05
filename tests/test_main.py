"""Tests for the MCP server tool layer."""
import asyncio
import json
from pathlib import Path
from typing import ClassVar

from src import main
from src.config import AccountConfig, Config
from src.errors import AuthError
from src.models import FolderInfo

READ_ONLY_TOOLS = {
    "list_folders",
    "list_messages",
    "read_message",
    "search_messages",
    "download_attachment",
}

EXPECTED_TOOLS = {
    "list_folders",
    "list_messages",
    "read_message",
    "search_messages",
    "download_attachment",
    "send_message",
    "create_folder",
    "rename_folder",
    "delete_folder",
    "move_message",
    "delete_message",
    "mark_read",
    "mark_unread",
}


def make_config() -> Config:
    accounts = [
        AccountConfig(
            email="first@example.fr",
            imap_host="imap.example.fr",
            smtp_host="smtp.example.fr",
            imap_port=993,
            smtp_port=465,
            password="pw1",
        ),
        AccountConfig(
            email="second@example.fr",
            imap_host="imap2.example.fr",
            smtp_host="smtp2.example.fr",
            imap_port=993,
            smtp_port=465,
            password="pw2",
        ),
    ]
    return Config(accounts, {"example.fr": "imap.example.fr"})


class StubConnector:
    built: ClassVar[list[str]] = []
    write_calls: ClassVar[list[str]] = []

    def __init__(self, account: AccountConfig):
        self.account = account
        type(self).built.append(account.email)

    def list_folders(self) -> list[FolderInfo]:
        return [FolderInfo(name="INBOX", message_count=2, flags=["\\HasChildren"])]

    def download_attachment(self, folder, uid, filename, dest_dir=Path("downloads")) -> Path:
        path = Path(dest_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"12345")
        return path

    def send_message(self, to, subject, body, cc=None, attachments=None) -> None:
        type(self).write_calls.append("send_message")

    def create_folder(self, name) -> None:
        type(self).write_calls.append("create_folder")

    def rename_folder(self, old_name, new_name) -> None:
        type(self).write_calls.append("rename_folder")

    def delete_folder(self, name) -> None:
        type(self).write_calls.append("delete_folder")

    def move_message(self, folder, uid, destination_folder) -> str:
        type(self).write_calls.append("move_message")
        return "999"

    def delete_message(self, folder, uid) -> None:
        type(self).write_calls.append("delete_message")

    def mark_read(self, folder, uid) -> None:
        type(self).write_calls.append("mark_read")

    def mark_unread(self, folder, uid) -> None:
        type(self).write_calls.append("mark_unread")


def install(monkeypatch) -> main.MCPServer:
    StubConnector.built = []
    StubConnector.write_calls = []
    monkeypatch.setattr(main, "load_config", lambda *args, **kwargs: make_config())
    monkeypatch.setattr(main, "MailConnector", StubConnector)
    return main.create_server()


def call_tool(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    if result.structured_content is not None:
        return result.structured_content["result"]
    return json.loads(result.content[0].text)


def test_tool_list_folders_returns_dicts(monkeypatch):
    server = install(monkeypatch)
    data = call_tool(server, "list_folders", {})
    assert data == [{"name": "INBOX", "message_count": 2, "flags": ["\\HasChildren"]}]
    assert StubConnector.built == ["first@example.fr"]


def test_tool_error_returns_structured_dict(monkeypatch):
    class Failing(StubConnector):
        def list_folders(self) -> list[FolderInfo]:
            raise AuthError("bad password")

    StubConnector.built = []
    monkeypatch.setattr(main, "load_config", lambda *args, **kwargs: make_config())
    monkeypatch.setattr(main, "MailConnector", Failing)
    server = main.create_server()
    data = call_tool(server, "list_folders", {})
    assert data["error"] == "auth"
    assert data["message"]
    assert data["hint"]


def test_all_thirteen_tools_registered(monkeypatch):
    server = install(monkeypatch)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == EXPECTED_TOOLS


def test_account_email_param_selects_account(monkeypatch):
    server = install(monkeypatch)
    call_tool(server, "list_folders", {"account_email": "second@example.fr"})
    assert StubConnector.built == ["second@example.fr"]


def test_readonly_mode_registers_only_read_tools(monkeypatch):
    def readonly_config() -> Config:
        config = make_config()
        config.readonly = True
        return config

    StubConnector.built = []
    monkeypatch.setattr(main, "load_config", lambda *args, **kwargs: readonly_config())
    monkeypatch.setattr(main, "MailConnector", StubConnector)
    server = main.create_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == READ_ONLY_TOOLS


def test_broken_config_fails_safe_to_read_only(monkeypatch):
    def broken_config(*args, **kwargs) -> Config:
        raise AuthError("nope")

    StubConnector.built = []
    monkeypatch.setattr(main, "load_config", broken_config)
    monkeypatch.setattr(main, "MailConnector", StubConnector)
    server = main.create_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == READ_ONLY_TOOLS
    data = call_tool(server, "list_folders", {})
    assert data["error"] == "auth"


def test_download_attachment_returns_path_and_size(monkeypatch, tmp_path):
    server = install(monkeypatch)
    data = call_tool(
        server,
        "download_attachment",
        {"folder": "INBOX", "uid": "7", "filename": "a.pdf", "dest_dir": str(tmp_path)},
    )
    assert data["path"] == str(tmp_path / "a.pdf")
    assert data["size"] == 5


# --- Confirmation flow tests ----------------------------------------------


def test_send_message_requires_confirmation(monkeypatch):
    server = install(monkeypatch)
    data = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi"},
    )
    assert data["needs_confirmation"] is True
    assert "token" in data
    assert data["action"] == "send_message"
    assert data["description"]
    assert StubConnector.write_calls == []


def test_send_message_executes_with_valid_token(monkeypatch):
    server = install(monkeypatch)
    first = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi"},
    )
    token = first["token"]
    second = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi", "confirm_token": token},
    )
    assert second.get("sent") is True
    assert StubConnector.write_calls == ["send_message"]


def test_send_message_rejects_invalid_token(monkeypatch):
    server = install(monkeypatch)
    data = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi",
         "confirm_token": "fake-token-123"},
    )
    assert data["error"] == "invalid_token"
    assert StubConnector.write_calls == []


def test_token_is_single_use(monkeypatch):
    server = install(monkeypatch)
    first = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi"},
    )
    token = first["token"]
    call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi", "confirm_token": token},
    )
    assert StubConnector.write_calls == ["send_message"]
    second = call_tool(
        server, "send_message",
        {"to": "dest@example.fr", "subject": "Hello", "body": "Hi", "confirm_token": token},
    )
    assert second["error"] == "invalid_token"
    assert len(StubConnector.write_calls) == 1


def test_all_write_tools_require_confirmation(monkeypatch):
    server = install(monkeypatch)
    write_tools = {
        "send_message": {"to": "a@b.fr", "subject": "S", "body": "B"},
        "create_folder": {"name": "NewFolder"},
        "rename_folder": {"old_name": "A", "new_name": "B"},
        "delete_folder": {"name": "A"},
        "move_message": {"folder": "INBOX", "uid": "1", "destination_folder": "Sent"},
        "delete_message": {"folder": "INBOX", "uid": "1"},
        "mark_read": {"folder": "INBOX", "uid": "1"},
        "mark_unread": {"folder": "INBOX", "uid": "1"},
    }
    for name, args in write_tools.items():
        data = call_tool(server, name, args)
        assert data.get("needs_confirmation") is True, f"{name} should require confirmation"
        assert "token" in data, f"{name} should return a token"
    assert StubConnector.write_calls == []


def test_readonly_mode_blocks_write_tools_even_with_confirmation(monkeypatch):
    def readonly_config() -> Config:
        config = make_config()
        config.readonly = True
        return config

    StubConnector.built = []
    StubConnector.write_calls = []
    monkeypatch.setattr(main, "load_config", lambda *args, **kwargs: readonly_config())
    monkeypatch.setattr(main, "MailConnector", StubConnector)
    server = main.create_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "send_message" not in names
    assert "delete_message" not in names


def test_confirmation_store_expiry(monkeypatch):
    from src.main import ConfirmationStore

    store = ConfirmationStore(ttl=0.001)
    token = store.create({"key": "value"})
    import time as _time
    _time.sleep(0.01)
    assert store.validate(token) is None
