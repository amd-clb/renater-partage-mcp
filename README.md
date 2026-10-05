# renater-partage-mcp

An MCP (Model Context Protocol) stdio server that gives AI agents full
management of Renater Partage academic mailboxes (`prenom.nom@example.edu`,
`prenom.nom@example2.edu`, or any configured Renater domain) over IMAP
(read, folders, attachments) and SMTP (send).

## Setup

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set your mailbox password:

```env
RENATER_PASSWORD=your-password
```

Edit `config.yaml` with your account(s) and the domain -> host map:

```yaml
accounts:
  - email: prenom.nom@example.edu
domains:
  example.edu: mail.example.edu
  example2.edu: mail.example.edu
imap: { port: 993, ssl: true }
smtp: { port: 465, ssl: true }
```

Multiple accounts: list them under `accounts` and set
`RENATER_PASSWORD_2`, `RENATER_PASSWORD_3`, ... in `.env` matched by account
order.

## MCP client configuration

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "renater-partage": {
      "command": "uv",
      "args": ["run", "renater-partage-mcp"],
      "cwd": "/path/to/renater_partage_connector"
    }
  }
}
```

### opencode (`opencode.json`)

```json
{
  "mcp": {
    "renater-partage": {
      "type": "local",
      "command": ["uv", "run", "renater-partage-mcp"],
      "cwd": "/path/to/renater_partage_connector",
      "enabled": true
    }
  }
}
```

The `cwd` must point to this project directory so `config.yaml` and `.env`
are found. Alternatively set `RENATER_CONFIG_PATH` to the config file path.

## Tools

Every tool accepts an optional `account_email` parameter to select a specific
configured account (defaults to the first one). On failure a tool returns a
structured error object `{"error": code, "message": ..., "hint": ...}`
instead of crashing.

| Tool | Description |
| --- | --- |
| `list_folders` | List mailbox folders with message counts |
| `list_messages` | List recent messages in a folder (newest first; `limit`, `unread_only`) |
| `read_message` | Read one full message (headers, body, attachment list) |
| `search_messages` | Search a folder with an IMAP query string (e.g. `FROM alice SUBJECT budget`) |
| `download_attachment` | Download one attachment into a local directory (default `downloads/`) |
| `send_message` | Send an email with optional `cc` and local `attachments` (file paths) |
| `create_folder` | Create a folder (intermediate folders must exist; duplicate is a no-op) |
| `rename_folder` | Rename a folder |
| `delete_folder` | Delete an empty folder |
| `move_message` | Move a message to another folder; returns its new UID |
| `delete_message` | Delete a message by UID |
| `mark_read` | Mark a message as read |
| `mark_unread` | Mark a message as unread |

## Security notes

- The password lives only in `.env` (git-ignored); keep it `chmod 600`.
- `config.yaml` contains no secrets.
- Destructive operations (`delete_message`, `move_message`) require an
  explicit message UID.
- Attachments are only written inside the `dest_dir` you pass (default
  `downloads/`); filenames are sanitized before writing.

## Testing

```bash
uv run pytest            # unit tests (fakes, no network)
RENATER_LIVE_TEST=1 uv run pytest tests/test_live.py   # live check against the real server
```

## Troubleshooting

- `auth` error: check `RENATER_PASSWORD` in `.env` (it must match your Renater
  SSO password).
- `network` error: check the IMAP/SMTP host and port in `config.yaml`
  (default `mail.example.edu`, IMAP 993, SMTP 465) and your network
  connectivity.
- `folder_not_found` / `message_not_found`: list folders or messages first to
  get the exact names and current UIDs.
