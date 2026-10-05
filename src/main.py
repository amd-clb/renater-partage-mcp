"""MCP stdio server exposing the Renater partage mail tools."""
from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import Config, load_config
from .connector import MailConnector
from .errors import ConfigError, ConnectorError


def _error_dict(exc: ConnectorError) -> dict:
    return {"error": exc.code, "message": str(exc), "hint": exc.hint}


def create_server() -> MCPServer:
    server = MCPServer(name="renater-partage")
    state: dict[str, Any] = {"config": None, "connectors": {}}

    def get_config() -> Config:
        if state["config"] is None:
            override = os.environ.get("RENATER_CONFIG_PATH")
            config_path = Path(override) if override else Path("config.yaml")
            state["config"] = load_config(config_path)
        return state["config"]

    def get_connector(account_email: str | None) -> MailConnector:
        config = get_config()
        if account_email is None:
            account = config.accounts[0]
        else:
            account = config.get_account(account_email)
        if account.email not in state["connectors"]:
            state["connectors"][account.email] = MailConnector(account)
        return state["connectors"][account.email]

    def run(action: Callable[[MailConnector], Any], account_email: str | None) -> Any:
        try:
            return action(get_connector(account_email))
        except ConnectorError as exc:
            return _error_dict(exc)

    def as_dicts(items) -> list[dict]:
        return [dataclasses.asdict(item) for item in items]

    @server.tool(description="List mailbox folders with message counts.")
    async def list_folders(account_email: str | None = None) -> list[dict] | dict:
        return run(lambda c: as_dicts(c.list_folders()), account_email)

    @server.tool(description="List recent messages in a folder (newest first).")
    async def list_messages(
        folder: str = "INBOX",
        limit: int = 20,
        unread_only: bool = False,
        account_email: str | None = None,
    ) -> list[dict] | dict:
        return run(
            lambda c: as_dicts(
                c.list_messages(folder=folder, unread_only=unread_only, limit=limit)
            ),
            account_email,
        )

    @server.tool(description="Read one full message (headers, body, attachments).")
    async def read_message(
        folder: str,
        uid: str,
        include_body: bool = True,
        include_attachments: bool = True,
        account_email: str | None = None,
    ) -> dict:
        def action(c: MailConnector) -> dict:
            parsed = c.read_message(
                folder, uid, include_body=include_body, include_attachments=include_attachments
            )
            return dataclasses.asdict(parsed)

        return run(action, account_email)

    @server.tool(description="Search messages in a folder with an IMAP query string.")
    async def search_messages(
        folder: str,
        query: str,
        limit: int = 20,
        account_email: str | None = None,
    ) -> list[dict] | dict:
        def action(c: MailConnector) -> list[dict] | dict:
            if not query.strip():
                raise ConfigError(
                    "search query must not be empty",
                    hint="Use an IMAP search string, e.g. 'FROM alice SUBJECT budget'.",
                )
            return as_dicts(c.search_messages(folder, query, limit=limit))

        return run(action, account_email)

    @server.tool(description="Download one attachment into a local directory.")
    async def download_attachment(
        folder: str,
        uid: str,
        filename: str,
        dest_dir: str = "downloads",
        account_email: str | None = None,
    ) -> dict:
        def action(c: MailConnector) -> dict:
            path = c.download_attachment(folder, uid, filename, dest_dir=Path(dest_dir))
            return {"path": str(path), "size": int(path.stat().st_size)}

        return run(action, account_email)

    @server.tool(description="Send an email with optional cc recipients and attachments.")
    async def send_message(
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        attachments: list[str] | None = None,
        account_email: str | None = None,
    ) -> dict:
        cc = list(cc or [])
        files = [Path(p) for p in (attachments or [])]

        def action(c: MailConnector) -> dict:
            c.send_message(to, subject, body, cc=cc, attachments=files)
            return {"sent": True, "to": [to, *cc], "subject": subject}

        return run(action, account_email)

    @server.tool(description="Create a folder; intermediate folders must exist.")
    async def create_folder(name: str, account_email: str | None = None) -> dict:
        def action(c: MailConnector) -> dict:
            c.create_folder(name)
            return {"ok": True, "name": name}

        return run(action, account_email)

    @server.tool(description="Rename a folder.")
    async def rename_folder(
        old_name: str, new_name: str, account_email: str | None = None
    ) -> dict:
        def action(c: MailConnector) -> dict:
            c.rename_folder(old_name, new_name)
            return {"ok": True, "old_name": old_name, "new_name": new_name}

        return run(action, account_email)

    @server.tool(description="Delete an empty folder.")
    async def delete_folder(name: str, account_email: str | None = None) -> dict:
        def action(c: MailConnector) -> dict:
            c.delete_folder(name)
            return {"ok": True, "name": name}

        return run(action, account_email)

    @server.tool(description="Move a message to another folder; returns its new UID.")
    async def move_message(
        folder: str, uid: str, destination_folder: str, account_email: str | None = None
    ) -> dict:
        def action(c: MailConnector) -> dict:
            new_uid = c.move_message(folder, uid, destination_folder)
            return {"ok": True, "new_uid": new_uid}

        return run(action, account_email)

    @server.tool(description="Delete a message from a folder.")
    async def delete_message(folder: str, uid: str, account_email: str | None = None) -> dict:
        def action(c: MailConnector) -> dict:
            c.delete_message(folder, uid)
            return {"ok": True, "uid": uid}

        return run(action, account_email)

    @server.tool(description="Mark a message as read.")
    async def mark_read(folder: str, uid: str, account_email: str | None = None) -> dict:
        def action(c: MailConnector) -> dict:
            c.mark_read(folder, uid)
            return {"ok": True, "uid": uid}

        return run(action, account_email)

    @server.tool(description="Mark a message as unread.")
    async def mark_unread(folder: str, uid: str, account_email: str | None = None) -> dict:
        def action(c: MailConnector) -> dict:
            c.mark_unread(folder, uid)
            return {"ok": True, "uid": uid}

        return run(action, account_email)

    return server


def main() -> None:
    create_server().run(transport="stdio")
