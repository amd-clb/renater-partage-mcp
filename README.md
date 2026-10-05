# renater-partage-mcp

An MCP (Model Context Protocol) stdio server that gives AI agents full
management of Renater Partage academic mailboxes (`prenom.nom@example.edu`,
`prenom.nom@example2.edu`, or any configured Renater domain) over IMAP
(read, folders, attachments) and SMTP (send).

> **Warning — this connector can send mail.** With the default
> `readonly: false`, an AI agent can **send, delete, move and flag mail on your
> behalf**, authenticated with your Renater SSO password. Every write tool
> (send, delete, move, folder management, flag) requires a **user
> confirmation token** before it executes — see
> [Confirmation for write tools](#confirmation-for-write-tools). If you only
> need to read mail, set `readonly: true` in `config.yaml` — see
> [Read-only mode](#read-only-mode).

## Setup

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
chmod 600 .env
cp config.yaml.example config.yaml
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

### opencode

The project includes an `opencode.json` that auto-registers the MCP server
when you open this directory in opencode. If you need to configure it
globally instead:

```json
{
  "mcp": {
    "renater-partage": {
      "type": "local",
      "command": ["uv", "run", "renater-partage-mcp"],
      "cwd": "/path/to/renater-partage-mcp",
      "enabled": true
    }
  }
}
```

The `cwd` must point to the project directory so `config.yaml` and `.env`
are found. Alternatively set `RENATER_CONFIG_PATH` to the config file path.

## Tools

Every tool accepts an optional `account_email` parameter to select a specific
configured account (defaults to the first one). On failure a tool returns a
structured error object `{"error": code, "message": ..., "hint": ...}`
instead of crashing.

| Tool | Description | Confirmation |
| --- | --- | --- |
| `list_folders` | List mailbox folders with message counts | No |
| `list_messages` | List recent messages in a folder (newest first; `limit`, `unread_only`) | No |
| `read_message` | Read one full message (headers, body, attachment list) | No |
| `search_messages` | Search a folder with an IMAP query string (e.g. `FROM alice SUBJECT budget`) | No |
| `download_attachment` | Download one attachment into a local directory (default `downloads/`) | No |
| `send_message` | Send an email with optional `cc` and local `attachments` (file paths) | Yes |
| `create_folder` | Create a folder (intermediate folders must exist; duplicate is a no-op) | Yes |
| `rename_folder` | Rename a folder | Yes |
| `delete_folder` | Delete an empty folder | Yes |
| `move_message` | Move a message to another folder; returns its new UID | Yes |
| `delete_message` | Delete a message by UID | Yes |
| `mark_read` | Mark a message as read | Yes |
| `mark_unread` | Mark a message as unread | Yes |

## Confirmation for write tools

Every write tool (send, delete, move, folder management, flag) uses a
**two-step confirmation** flow. The AI agent cannot execute a write action
without explicit user approval.

1. The agent calls the tool with the action parameters.
2. The server returns `{"needs_confirmation": true, "token": "<uuid>",
   "description": "...", "hint": "..."}` and does **not** execute the action.
3. The agent presents the action to the user and asks for approval.
4. If approved, the agent calls the same tool again with `confirm_token` set
   to the token from step 2. The server then executes the action.

Tokens are single-use and expire after 5 minutes. A tool called with an
invalid or expired token returns an `invalid_token` error.

Read tools (`list_folders`, `list_messages`, `read_message`,
`search_messages`, `download_attachment`) execute immediately without
confirmation.

## Read-only mode

IMAP/SMTP on Renater partage authenticate with the password only (no 2FA —
that gate exists only on the webmail SSO). To shrink the risk of a leaked
password, you can expose only the read tools:

```yaml
# config.yaml
readonly: true
```

With `readonly: true` the server registers only `list_folders`,
`list_messages`, `read_message`, `search_messages` and
`download_attachment`. The send/folder/message-management tools are not
registered at all, so an AI agent (or a leaked password) cannot send, delete,
move or flag mail. If the config file cannot be read at all, the server also
fails safe and exposes only the read tools.

Restart the MCP server after changing this flag.

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
