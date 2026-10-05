# Renater Partage MCP Connector — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Python MCP stdio server giving agents full IMAP/SMTP management of Renater partage mailboxes.

**Architecture:** Three layers — `config.py` (config.yaml + .env loading, domain→host resolution), `connector.py` (MailConnector doing all IMAP/SMTP via `imap-tools`/`smtplib`, plus message parsing helpers), `main.py` (thin FastMCP tool layer mapping exceptions to structured errors). MCP is only imported in `main.py`.

**Tech Stack:** Python ≥3.11, `mcp` (FastMCP), `imap-tools`, stdlib `smtplib`/`email`, `pyyaml`, `python-dotenv`, `pytest`, `ruff`. `uv` for env/deps.

**Spec:** `docs/superpowers/specs/2026-10-05-renater-partage-mcp-design.md`

## Global Constraints

- Python ≥ 3.11; package layout `src/`; entry point `renater-partage-mcp = src.main:main`.
- Dependencies: only `mcp`, `imap-tools`, `pyyaml`, `python-dotenv`; dev: `pytest`, `ruff`.
- `connector.py` must not import `mcp`; `main.py` must not import `imap_tools`/`smtplib` directly.
- One IMAP session per operation (open → act → close); one SMTP connection per send.
- Attachment writes only inside `dest_dir`; filenames sanitized (no `/`, `\`, `..`, no absolute paths, non-empty after cleaning).
- Limits: `list_messages`/`search_messages` default limit 20, hard max 100 (clamped, never an error).
- Tool errors return a dict `{error: <TypeName>, message: str, hint: str}` — never raise through the MCP layer.
- Tests must run offline (no network); live tests only under `RENATER_LIVE_TEST=1`.

## Review Focus

1. **Wrong password / expired session** → `AuthError` with hint pointing at `.env`; server stays alive.
2. **Non-ASCII subject/from (French mail)** → parsed correctly via `email.header.decode_header`, never `=?...?=` garbage.
3. **Attachment filename path traversal** (`../../etc/cron.d/evil`) → sanitized, written under `dest_dir` only.
4. **HTML-only message** → `body_text` produced by stripping tags; empty body → empty string, not crash.
5. **Requesting `uid` that no longer exists** → `MessageNotFoundError`, not a raw IMAP exception.

Each focus line gets a test added to the owning task below.

---

### Task 1: Scaffold + config module

**Files:**
- Create: `pyproject.toml`, `config.yaml`, `.env.example`, `.gitignore`, `src/__init__.py`, `src/errors.py`, `src/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `src.errors`: `ConnectorError(Exception)` with attrs `code: str`, `hint: str`; subclasses `AuthError`, `ConfigError`, `FolderNotFoundError`, `MessageNotFoundError`, `NetworkError`, `SendError` (each with a fixed `code` string: `"auth"`, `"config"`, `"folder_not_found"`, `"message_not_found"`, `"network"`, `"send"`).
  - `src.config.AccountConfig` (dataclass): `email: str`, `imap_host: str`, `smtp_host: str`, `imap_port: int`, `smtp_port: int`, `password: str`.
  - `src.config.Config`: attrs `accounts: list[AccountConfig]`, `domains: dict[str, str]`; method `get_account(email: str) -> AccountConfig` (raises `ConfigError` if unknown).
  - `src.config.load_config(config_path: Path, env_path: Path | None = None) -> Config`.

- [ ] **Step 1: Write failing tests** in `tests/test_config.py`:
  - `test_load_config_resolves_hosts`: write temp `config.yaml` with accounts `[prenom.nom@example.edu]`, domains `{example.edu: mail.example.edu, example2.edu: mail.example.edu}`, imap port 993, smtp port 465; env file `RENATER_PASSWORD=secret`. Assert `config.get_account("prenom.nom@example.edu")` has `imap_host == "mail.example.edu"`, `smtp_host == "mail.example.edu"`, `imap_port == 993`, `smtp_port == 465`, `password == "secret"`.
  - `test_account_domain_missing_from_map_raises`: account domain not in `domains` and no per-account override → `ConfigError`.
  - `test_get_account_unknown_raises`: `ConfigError`.
  - `test_per_account_host_override`: account entry with explicit `imap_host: mail.example.fr` wins over the domain map.
  - `test_password_missing_raises`: no matching env var → `ConfigError` with message naming the expected variable (`RENATER_PASSWORD`).
  - `test_second_account_password_indexed`: two accounts → second reads `RENATER_PASSWORD_2`.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (modules don't exist). First run: `uv venv && uv sync --extra dev` to create env.

- [ ] **Step 3: Implement** `pyproject.toml` (deps + `renater-partage-mcp = "src.main:main"` entry point, `[tool.pytest.ini_options] pythonpath=["."]`), `.gitignore` (`.env`, `.venv`, `__pycache__`, `downloads/`), `.env.example`, `config.yaml` (both domains from spec §6), `src/errors.py` (six exception classes per Interfaces), and `src/config.py` (parse YAML, load env via `python-dotenv` without overwriting real env, build `AccountConfig`s: host = per-account override or `domains[account domain]` else `ConfigError`; password = `RENATER_PASSWORD[_N]` by account order else `ConfigError`).

- [ ] **Step 4: Verify pass** — `uv run pytest tests/test_config.py -v` → all PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: project scaffold and config loading"` (init `git init` first if repo absent).

### Task 2: Models + message parsing + filename sanitization

**Files:**
- Create: `src/models.py`, `src/connector.py` (module-level parsing helpers only; the `MailConnector` class arrives in Task 3)
- Test: `tests/test_parsing.py`

**Interfaces:**
- Produces:
  - `src.models.FolderInfo` (dataclass): `name: str`, `message_count: int`, `flags: list[str]`.
  - `src.models.MessageSummary` (dataclass): `uid: str`, `date: str` (ISO 8601), `from_: str`, `subject: str`, `snippet: str`, `flags: list[str]`.
  - `src.models.AttachmentInfo` (dataclass): `filename: str`, `size: int`, `content_type: str`.
  - `src.models.ParsedMessage` (dataclass): `uid: str | None`, `subject: str`, `from_: str`, `to: str`, `date: str`, `body_text: str`, `body_html: str`, `attachments: list[AttachmentInfo]`.
  - `src.connector.parse_message(raw: bytes, uid: str | None = None) -> ParsedMessage`.
  - `src.connector.sanitize_filename(name: str) -> str`.

- [ ] **Step 1: Write failing tests** in `tests/test_parsing.py`:
  - `test_parse_simple_text_message`: raw RFC822 with `Subject: Hello`, `From: a@b.fr`, text body → `subject == "Hello"`, `body_text` contains body, `body_html == ""`, `attachments == []`.
  - `test_parse_non_ascii_subject`: header `Subject: =?utf-8?Q?Caf=C3=A9_?= OK` → `subject == "Café OK"`.
  - `test_parse_multipart_with_attachment`: multipart/mixed with quoted-printable text part + base64 `application/pdf` part named `rapport.pdf` (small byte payload) → one `AttachmentInfo` with `filename == "rapport.pdf"`, matching size, `content_type == "application/pdf"`; text part in `body_text`.
  - `test_parse_html_only_message`: single `text/html` part → `body_html` non-empty, `body_text` contains the visible text without tags and is non-empty.
  - `test_parse_empty_body`: no body parts → `body_text == ""`, no exception.
  - `test_sanitize_filename_rejects_traversal`: `sanitize_filename("../../etc/cron.d/evil")` returns non-empty, contains no `/`, `\`, or `..`.
  - `test_sanitize_filename_keeps_unicode_and_ext`: `"café-2026.pdf"` unchanged.

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_parsing.py -v` → FAIL (`src.models` / `src.connector` missing).

- [ ] **Step 3: Implement** `src/models.py` (four dataclasses). In `src/connector.py`: `sanitize_filename` (strip directory components, drop null bytes, collapse `..`, fall back to `"attachment"`, cap at 150 chars, preserve one dot-extension); `parse_message` using stdlib `email.message_from_bytes` + `email.header.decode_header` on subject/from/to; walk parts: text/plain → `body_text` (first wins), text/html → `body_html` (first wins); non-inline parts → attachments via `get_payload(decode=True)`; if `body_text` empty and `body_html` present, strip tags with `re.sub(r"<[^>]+>", " ", html)` and collapse whitespace.

- [ ] **Step 4: Verify pass** — `uv run pytest tests/test_parsing.py -v` → all PASS. Also run `uv run pytest -q` (Task 1 tests still green).

- [ ] **Step 5: Commit** — `git commit -m "feat: message models, RFC822 parsing, filename sanitization"`.

### Task 3: MailConnector — read operations (IMAP)

**Files:**
- Modify: `src/connector.py` (add `MailConnector` class)
- Test: `tests/test_connector.py`

**Interfaces:**
- Consumes: `AccountConfig` (Task 1), `FolderInfo`/`MessageSummary`/`ParsedMessage`, `parse_message` (Task 2), all `src.errors`.
- Produces (all methods open/close their own IMAP session via `imap_tools.MailBox(host, port, ssl=True, login=..., password=...)`):
  - `MailConnector(account: AccountConfig)`
  - `list_folders() -> list[FolderInfo]`
  - `list_messages(folder: str = "INBOX", query: str | None = None, unread_only: bool = False, limit: int = 20) -> list[MessageSummary]`
  - `search_messages(folder: str, query: str, limit: int = 20) -> list[MessageSummary]`
  - `read_message(folder: str, uid: str, include_body: bool = True, include_attachments: bool = True) -> ParsedMessage`
  - `download_attachment(folder: str, uid: str, filename: str, dest_dir: Path = Path("downloads")) -> Path`
  - Folder not found → `FolderNotFoundError`; IMAP auth failure (imap_tools `imaplib.IMAP4.error` containing `AUTHENTICATIONFAILED`) → `AuthError`; connection failures → `NetworkError`.

- [ ] **Step 1: Write failing tests** in `tests/test_connector.py`. Use a fake `MailBox` class (records login args, yields canned folders/messages) injected by monkeypatching `imap_tools.MailBox` inside `src.connector` namespace. Fixtures: fake folder `INBOX` (10 msgs) + `Sent`; fake messages with `uid`, `date`, `from`, `subject`, `flags`, `raw`.
  - `test_list_folders_returns_counts`: two folders, correct names/counts/flags.
  - `test_list_messages_limit_clamped_to_100`: call with `limit=500` → fake MailBox receives an effective cap of 100 (assert on number of messages returned: 100, or on fetch args).
  - `test_list_messages_unread_only_filters`: `unread_only=True` with 3 unseen of 10 → 3 summaries, flags include `\Seen`-absent.
  - `test_list_messages_missing_folder_raises`: folder `"Nope"` not in fake mailbox → `FolderNotFoundError`.
  - `test_read_message_returns_parsed`: canned raw (multipart + attachment) → `ParsedMessage` with `uid` set, attachment present.
  - `test_read_message_unknown_uid_raises`: `MessageNotFoundError`.
  - `test_auth_failure_raises_auth_error`: fake MailBox raises `imaplib.IMAP4.error("AUTHENTICATIONFAILED")` → `AuthError` with hint mentioning `.env`.
  - `test_download_attachment_writes_under_dest_dir`: fake message with base64 pdf attachment; call with filename `"../../evil.pdf"`, `dest_dir=tmp_path` → returned path is `tmp_path`-rooted, filename sanitized, content matches.
  - `test_download_attachment_unknown_filename_raises`: no such part → `MessageNotFoundError`.

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_connector.py -v` → FAIL.

- [ ] **Step 3: Implement** `MailConnector.__init__`, `_connect() -> MailBox` (wraps connection errors in `NetworkError`, auth errors in `AuthError`), and the five read methods. `list_messages`: build criteria (`UNSEEN` if unread_only; `query` string if given), fetch `UID BODY.PEEK[]` for latest N (clamp 1..100, newest-first via reversed), map to `MessageSummary` (snippet = first 200 chars of decoded body). `read_message`: fetch raw by UID; if absent → `MessageNotFoundError`; call `parse_message`; drop body/attachments per flags. `download_attachment`: parse, find part by exact filename, `sanitize_filename`, `dest_dir.mkdir(parents=True)`, assert resolved path stays under `dest_dir`, write bytes.

- [ ] **Step 4: Verify pass** — `uv run pytest -q` → all green.

- [ ] **Step 5: Commit** — `git commit -m "feat: IMAP read operations in MailConnector"`.

### Task 4: MailConnector — send + management operations

**Files:**
- Modify: `src/connector.py`
- Test: `tests/test_connector.py` (extend)

**Interfaces:**
- Produces (same class/session rules as Task 3; send uses `smtplib.SMTP_SSL(host, port)` with `login`/`sendmail`, message built with `email.mime`):
  - `send_message(to: str, subject: str, body: str, cc: list[str] | None = None, attachments: list[Path] | None = None) -> None` — `SendError` on SMTP failure; attachment files must exist (`ConfigError` if not, message naming the path); filenames attached = sanitized basename.
  - `create_folder(name: str) -> None` — `FolderNotFoundError` if a parent path segment (for `a/b`) doesn't exist; duplicate → no-op success.
  - `rename_folder(old: str, new: str) -> None`
  - `delete_folder(name: str) -> None` — non-empty folder or missing folder → `FolderNotFoundError` (message explains non-empty).
  - `move_message(folder: str, uid: str, dest_folder: str) -> str` — returns new uid; missing dest → `FolderNotFoundError`; missing uid → `MessageNotFoundError`.
  - `delete_message(folder: str, uid: str) -> None` — `\Seen`-less or seen both fine; missing uid → `MessageNotFoundError`.
  - `mark_read(folder: str, uid: str) -> None`, `mark_unread(folder: str, uid: str) -> None` — missing uid → `MessageNotFoundError`.

- [ ] **Step 1: Write failing tests** (extend `tests/test_connector.py`, reuse fake MailBox; add fake `SMTP_SSL` via monkeypatch in `src.connector`):
  - `test_send_message_simple`: captured `sendmail` call has From/To/Subject/`MIME-Version`; body present; returns None.
  - `test_send_message_with_cc_and_attachment`: cc in headers; one file attachment part with correct filename + bytes; attachment path `tmp_path/f.txt`.
  - `test_send_message_missing_attachment_raises`: nonexistent path → `ConfigError`.
  - `test_send_smtp_failure_raises_send_error`: fake SMTP raises `smtplib.SMTPException` → `SendError`.
  - `test_create_nested_folder_creates_parent`: fake mailbox without `A/B` but with `A` → `CREATE A/B` issued (assert on fake).
  - `test_delete_non_empty_folder_raises`: `FolderNotFoundError`.
  - `test_move_message_returns_new_uid`: fake UIDPLUS `UID MOVE` returns uid `"999"` → result `"999"`; dest folder missing → `FolderNotFoundError`.
  - `test_delete_message_missing_uid_raises`: `MessageNotFoundError`.
  - `test_mark_read_uses_set_flag`: fake records `\Seen` set command; `mark_unread` clears it.

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_connector.py -v` → new tests FAIL.

- [ ] **Step 3: Implement** the nine methods per signatures. Send: build `MIMEMultipart` (mixed only if attachments), text/plain UTF-8 body, `Subject` via `Header`; send from `account.email`. Management ops via `imap_tools` (`create_folder`, `rename_folder`, `delete_folder`, `uid.move`, `uid.store` with `FLAGS.SILENT`).

- [ ] **Step 4: Verify pass** — `uv run pytest -q` → all green.

- [ ] **Step 5: Commit** — `git commit -m "feat: SMTP send and folder/message management"`.

### Task 5: MCP tool layer (`main.py`)

**Files:**
- Create: `src/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_config`, `Config`, `MailConnector`, all exceptions.
- Produces:
  - `create_server() -> FastMCP` (named `"renater-partage"`); `main()` runs `server.run(transport="stdio")` (entry point).
  - All 13 tools from spec §7, each: load config (cached at server init; `config.yaml` next to CWD, overridable via env `RENATER_CONFIG_PATH`), build/hold a `MailConnector` for the first account (multi-account: tool param `account_email: str | None = None` on every tool, defaulting to first configured account), execute, and on `ConnectorError` return `{"error": exc.code, "message": str(exc), "hint": exc.hint}`.
  - Tool return shapes: `list_folders` → `list[dict]`; `list_messages`/`search_messages` → `list[dict]` (MessageSummary as dict); `read_message` → dict with headers/body/attachments; `download_attachment` → `{"path": str, "size": int}`; `send_message` → `{"sent": true, "to": [...], "subject": ...}`; management ops → `{"ok": true, ...}`.

- [ ] **Step 1: Write failing tests** in `tests/test_main.py` (build server with `create_server()`, call tools via `await server.call_tool(name, args)` from the mcp SDK test utilities; monkeypatch `load_config` to a temp config, and monkeypatch `MailConnector` with a stub returning canned objects):
  - `test_tool_list_folders_returns_dicts`.
  - `test_tool_error_returns_structured_dict`: stub raises `AuthError` → result content JSON contains `error == "auth"` and a non-empty `hint`; the call does not raise.
  - `test_all_thirteen_tools_registered`: the 13 tool names exist.
  - `test_account_email_param_selects_account`: `account_email="second@..."` → connector built for that account.
  - `test_download_attachment_returns_path_and_size`.

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_main.py -v` → FAIL.

- [ ] **Step 3: Implement** `src/main.py`: `@mcp.tool()` async functions (one per tool, thin: validate args — e.g. `limit` clamp hint, non-empty `query` for search — delegate to `MailConnector`, serialize dataclasses with `dataclasses.asdict`, catch `ConnectorError` into the structured dict). Shared helper `_connector(account_email)`. Config loaded once lazily; `ConfigError` at load time also returns the structured dict.

- [ ] **Step 4: Verify pass** — `uv run pytest -q` → all green; `uv run ruff check .` clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: MCP server exposing 13 mail tools"`.

### Task 6: Live-test hook, README, final verification

**Files:**
- Create: `tests/test_live.py`, `README.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write `tests/test_live.py`** — module-level `pytest.mark.skipif(os.environ.get("RENATER_LIVE_TEST") != "1", reason="set RENATER_LIVE_TEST=1")`; one test: `load_config()` real config → `MailConnector.list_folders()` returns a list containing `INBOX`.

- [ ] **Step 2: Write `README.md`** — purpose; setup (`uv sync`, copy `.env.example` → `.env` + `chmod 600`, edit `config.yaml` with your domains/hosts from spec §6); MCP client snippets: Claude Desktop `mcpServers` entry (`"command": "uv", "args": ["run", "renater-partage-mcp"]` with `cwd`) and opencode `opencode.json` mcp entry; tool reference table (13 tools); security notes (password only in `.env`, 0600, git-ignored; delete ops require explicit uid; attachments only under `downloads/`); troubleshooting (auth → check `.env`; SSL → check host/port in `config.yaml`).

- [ ] **Step 3: Final verification** — `uv run pytest -q` (live test shows SKIPPED), `uv run ruff check .` clean, `uv run renater-partage-mcp --help`-style smoke: run the entry point under timeout to confirm it starts without import errors (`timeout 3 uv run renater-partage-mcp; echo "exit=$?"` — expect clean start/timeout, no traceback).

- [ ] **Step 4: Commit** — `git commit -m "docs: README, live test hook, final verification"`.
