"""Tests for the MCP server tool layer."""
import asyncio
import json
from pathlib import Path
from typing import ClassVar

from src import main
from src.config import AccountConfig, Config
from src.errors import AuthError
from src.models import FolderInfo

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
        return None


def install(monkeypatch) -> main.MCPServer:
    StubConnector.built = []
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


def test_download_attachment_returns_path_and_size(monkeypatch, tmp_path):
    server = install(monkeypatch)
    data = call_tool(
        server,
        "download_attachment",
        {"folder": "INBOX", "uid": "7", "filename": "a.pdf", "dest_dir": str(tmp_path)},
    )
    assert data["path"] == str(tmp_path / "a.pdf")
    assert data["size"] == 5
