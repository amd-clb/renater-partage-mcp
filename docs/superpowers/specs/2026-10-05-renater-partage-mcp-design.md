# Renater Partage Mail Connector (MCP) — Design Spec

Date: 2026-10-05
Status: approved (design confirmed in chat)

## 1. Purpose

A Gmail-style connector for Renater "partage" academic mailboxes
(e.g. `prenom.nom@example.edu`, `prenom.nom@example2.edu`). It exposes
full mailbox management to AI agents (Claude Desktop, opencode, any MCP
client) as an MCP stdio server, backed by direct IMAP/SMTP access to the
Renater partage platform.

## 2. Background / Probing Results

- Webmail (`https://mail.example.edu/mail`) sits behind SAML SSO
  (`sso.example.edu`) — not automatable headlessly.
- Renater partage mail infrastructure supports direct protocol access:
  - IMAP4rev1 on `mail.example.edu:993` (TLS) — confirmed open.
  - SMTP submission on `mail.example.edu:465` (TLS, Postfix mtaauth)
    — confirmed open.
  - `mail.example.edu` is a CNAME to
    `*.partage.renater.fr`; both `example.edu` and `example2.edu`
    MX records point to Renater relay (`mx*.relay.renater.fr`).
- Credentials: username = full Renater address, password = user's SSO
  password.

Conclusion: connector talks IMAP + SMTP, not the web UI.

## 3. Decisions (from user)

| Decision | Choice |
|---|---|
| Connector type | Agent/AI connector (MCP server) |
| Domain coverage | Configurable list (config file) |
| Mail scope | Full management (read, send, folders, move/delete) |
| Runtime | Python (MCP stdio server) |
| Credentials | `.env` file (git-ignored, chmod 600) |

## 4. Stack

- Python ≥ 3.11
- `mcp` SDK (FastMCP) — MCP server framework
- `imap-tools` — IMAP client (sessions, UIDs, folders, attachments)
- stdlib `smtplib` + `email` — SMTP send / MIME parsing
- `pyyaml`, `python-dotenv` — configuration
- `pytest` — tests; `ruff` — lint
- Packaging: `pyproject.toml`, console entry point
  `renater-partage-mcp = src.main:main`, run via `uv run` / venv

## 5. Layout

```
renater_partage_connector/
├── pyproject.toml
├── config.yaml
├── .env.example
├── .gitignore
├── README.md
├── src/
│   ├── main.py        # FastMCP server; thin tool layer, arg validation
│   ├── config.py      # load config.yaml + .env; domain→host resolution
│   ├── connector.py   # MailConnector: IMAP/SMTP operations (no MCP imports)
│   ├── models.py      # dataclasses: FolderInfo, MessageSummary, Attachment
│   └── errors.py      # AuthError, FolderNotFoundError, NetworkError, ...
└── tests/
    ├── test_config.py
    ├── test_connector.py   # mocked imap-tools / smtplib
    ├── test_parsing.py     # raw RFC822 fixtures (multipart, 8-bit, attach)
    └── test_live.py        # opt-in, skipped unless RENATER_LIVE_TEST=1
```

Layering rule: `main.py` never touches IMAP/SMTP directly;
`connector.py` never imports MCP. Tools validate args, call the connector,
map exceptions to structured error results.

## 6. Configuration

`config.yaml`:

```yaml
accounts:
  - email: prenom.nom@example.edu
domains:                       # configurable: domain → partage host
  example.edu: mail.example.edu
  example2.edu: mail.example.edu
imap: { port: 993, ssl: true }
smtp: { port: 465, ssl: true }
```

- `.env`: `RENATER_PASSWORD=...` (git-ignored, chmod 600). Multiple
  accounts: `RENATER_PASSWORD_2`, `RENATER_PASSWORD_3`, ... matched by
  account order.
- `config.py` validates: non-empty accounts, each account domain present in
  `domains` (or explicit `imap_host`/`smtp_host` override per account),
  password resolvable.

## 7. Tools (13)

Read:
- `list_folders()` — names, message counts, flags
- `list_messages(folder, query?, unread_only?, limit=20, max 100)`
- `read_message(folder, uid, include_body=true, include_attachments=true)`
- `search_messages(query, folder, limit)` — IMAP SEARCH syntax
- `download_attachment(folder, uid, filename, dest_dir=downloads/)`

Write:
- `send_message(to, subject, body, cc?, attachments?)`

Manage:
- `create_folder(name)` / `rename_folder(old, new)` / `delete_folder(name)`
- `move_message(folder, uid, dest_folder)`
- `delete_message(folder, uid)`
- `mark_read(folder, uid)` / `mark_unread(folder, uid)`

Returns are structured (JSON-serializable): folder names + counts; message
summaries (uid, date, from, subject, snippet, flags); read messages get full
headers, text body (HTML→text fallback), attachment list; sends get a
confirmation result.

## 8. Safety Rules

- One IMAP session per tool call (open → operate → close); reconnect on
  stale/expired session.
- Attachment download: sanitize filenames (no path separators, no `..`),
  never write outside `dest_dir`; default `./downloads`; report saved path +
  size.
- Delete operations require an explicit `uid` in a specific folder — no
  bulk/wildcard deletes exposed to the agent.

## 9. Error Handling

Typed exceptions in `errors.py`: `AuthError`, `FolderNotFoundError`,
`MessageNotFoundError`, `NetworkError`, `ConfigError`, `SendError`.
`main.py` catches and returns `{error: <type>, message, hint}` — e.g.
`AuthError` hints at checking `.env`. The server never crashes on a tool
error.

## 10. Testing

- `pytest` coverage: config loading/validation/domain resolution; filename
  sanitization; message + attachment parsing from raw RFC822 fixtures
  (multipart/mixed, quoted-printable, base64, unicode subjects); every tool
  against mocked `imap-tools`/`smtplib` (happy path + each error type).
- `test_live.py` runs real IMAP ops only when `RENATER_LIVE_TEST=1` is set;
  skipped otherwise.
- Verification commands: `uv sync`, `uv run pytest`, `uv run ruff check`.

## 11. Documentation

README: purpose, probing findings summary, setup (venv/uv, `.env` with
chmod 600, `config.yaml`), MCP client config snippets for Claude Desktop
(`mcpServers` → `uv run renater-partage-mcp`) and opencode (`opencode.json`
mcp section), tool reference, security notes.
