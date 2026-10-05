"""MCP stdio server exposing the Renater partage mail tools."""
from __future__ import annotations

import dataclasses
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import Config, load_config
from .connector import MailConnector
from .errors import ConfigError, ConnectorError


class ConfirmationStore:
    """In-memory store for one-shot write-action confirmation tokens."""

    def __init__(self, ttl: float = 300.0):
        self._pending: dict[str, tuple[float, dict]] = {}
        self._ttl = ttl

    def create(self, details: dict) -> str:
        token = str(uuid.uuid4())
        self._pending[token] = (time.monotonic(), details)
        self._cleanup()
        return token

    def validate(self, token: str) -> dict | None:
        entry = self._pending.pop(token, None)
        if entry is None:
            return None
        created_at, details = entry
        if time.monotonic() - created_at > self._ttl:
            return None
        return details

    def _cleanup(self) -> None:
        now = time.monotonic()
        expired = [t for t, (ts, _) in self._pending.items() if now - ts > self._ttl]
        for t in expired:
            del self._pending[t]


def _error_dict(exc: ConnectorError) -> dict:
    return {"error": exc.code, "message": str(exc), "hint": exc.hint}


def _invalid_token() -> dict:
    return {
        "error": "invalid_token",
        "message": "Confirmation token is invalid or expired.",
        "hint": "Call the tool again without confirm_token to obtain a fresh token.",
    }


def create_server() -> MCPServer:
    server = MCPServer(name="renater-partage")
    state: dict[str, Any] = {"config": None, "config_error": None, "connectors": {}}
    confirm_store = ConfirmationStore()

    override = os.environ.get("RENATER_CONFIG_PATH")
    config_path = Path(override) if override else Path("config.yaml")
    try:
        state["config"] = load_config(config_path)
    except ConnectorError as exc:
        state["config_error"] = exc

    readonly = state["config"].readonly if state["config"] is not None else True

    def get_config() -> Config:
        if state["config_error"] is not None:
            raise state["config_error"]
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

    if not readonly:

        @server.tool(description="Send an email. Requires user confirmation before execution.")
        async def send_message(
            to: str,
            subject: str,
            body: str,
            cc: list[str] | None = None,
            attachments: list[str] | None = None,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {
                "to": to,
                "subject": subject,
                "body": body,
                "cc": list(cc or []),
                "attachments": list(attachments or []),
                "account_email": account_email,
            }
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "send_message",
                    "description": f"Send email to {to} (subject: {subject})",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.send_message(
                    validated["to"],
                    validated["subject"],
                    validated["body"],
                    cc=validated["cc"],
                    attachments=[Path(p) for p in validated["attachments"]],
                )
                return {
                    "sent": True,
                    "to": [validated["to"], *validated["cc"]],
                    "subject": validated["subject"],
                }

            return run(action, validated["account_email"])

        @server.tool(description="Create a folder. Requires user confirmation before execution.")
        async def create_folder(
            name: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {"name": name, "account_email": account_email}
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "create_folder",
                    "description": f"Create folder '{name}'",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.create_folder(validated["name"])
                return {"ok": True, "name": validated["name"]}

            return run(action, validated["account_email"])

        @server.tool(description="Rename a folder. Requires user confirmation before execution.")
        async def rename_folder(
            old_name: str,
            new_name: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {
                "old_name": old_name,
                "new_name": new_name,
                "account_email": account_email,
            }
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "rename_folder",
                    "description": f"Rename folder '{old_name}' to '{new_name}'",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.rename_folder(validated["old_name"], validated["new_name"])
                return {
                    "ok": True,
                    "old_name": validated["old_name"],
                    "new_name": validated["new_name"],
                }

            return run(action, validated["account_email"])

        @server.tool(description="Delete an empty folder. Requires user confirmation before execution.")
        async def delete_folder(
            name: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {"name": name, "account_email": account_email}
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "delete_folder",
                    "description": f"Delete folder '{name}'",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.delete_folder(validated["name"])
                return {"ok": True, "name": validated["name"]}

            return run(action, validated["account_email"])

        @server.tool(description="Move a message to another folder. Requires user confirmation before execution.")
        async def move_message(
            folder: str,
            uid: str,
            destination_folder: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {
                "folder": folder,
                "uid": uid,
                "destination_folder": destination_folder,
                "account_email": account_email,
            }
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "move_message",
                    "description": f"Move message (UID {uid}) from '{folder}' to '{destination_folder}'",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                new_uid = c.move_message(
                    validated["folder"], validated["uid"], validated["destination_folder"]
                )
                return {"ok": True, "new_uid": new_uid}

            return run(action, validated["account_email"])

        @server.tool(description="Delete a message. Requires user confirmation before execution.")
        async def delete_message(
            folder: str,
            uid: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {"folder": folder, "uid": uid, "account_email": account_email}
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "delete_message",
                    "description": f"Delete message (UID {uid}) from '{folder}'",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.delete_message(validated["folder"], validated["uid"])
                return {"ok": True, "uid": validated["uid"]}

            return run(action, validated["account_email"])

        @server.tool(description="Mark a message as read. Requires user confirmation before execution.")
        async def mark_read(
            folder: str,
            uid: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {"folder": folder, "uid": uid, "account_email": account_email}
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "mark_read",
                    "description": f"Mark message (UID {uid}) in '{folder}' as read",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.mark_read(validated["folder"], validated["uid"])
                return {"ok": True, "uid": validated["uid"]}

            return run(action, validated["account_email"])

        @server.tool(description="Mark a message as unread. Requires user confirmation before execution.")
        async def mark_unread(
            folder: str,
            uid: str,
            confirm_token: str | None = None,
            account_email: str | None = None,
        ) -> dict:
            details = {"folder": folder, "uid": uid, "account_email": account_email}
            if confirm_token is None:
                token = confirm_store.create(details)
                return {
                    "needs_confirmation": True,
                    "token": token,
                    "action": "mark_unread",
                    "description": f"Mark message (UID {uid}) in '{folder}' as unread",
                    "hint": "Ask the user to approve. If approved, call again with confirm_token.",
                }
            validated = confirm_store.validate(confirm_token)
            if validated is None:
                return _invalid_token()

            def action(c: MailConnector) -> dict:
                c.mark_unread(validated["folder"], validated["uid"])
                return {"ok": True, "uid": validated["uid"]}

            return run(action, validated["account_email"])

    return server


def main() -> None:
    create_server().run(transport="stdio")
