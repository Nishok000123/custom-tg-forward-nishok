# Security Audit — custom-tg-forward-nishok

Date: 2026-11-27 · Branch: `refactor/per-forward-filters` · Base commit: `682b9d4`

This was a manual review. No "Cloudflare security audit" tool was available. It covers secrets handling, input handling, network exposure, and how the bot fails under errors.

Severity: **High** = account/data takeover or easy remote abuse · **Medium** = abuse needing a logged-in user or specific conditions · **Low** = hardening.

---

## 0. Already remediated

| Item | Status |
|---|---|
| Bot token + 2 MongoDB URIs (with passwords) committed in old HF Space git history | Token revoked, DB passwords rotated, Telegram sessions terminated, 2FA on. Space made private. GitHub repo has a single clean orphan commit. |
| `__pycache__` / `.session` / `.env` could be committed | `.gitignore` added. |
| Public web dashboard | Off by default (`ENABLE_DASHBOARD` unset on orkestr). |

> The old history still exists on the HF Space (owner `rakesh009`, the account is locked so force-push isn't possible) and on the local branch `backup-before-clean`. Treat every credential that was ever in that history as burned. Don't reuse any of them.

---

## 1. High

### H1 — Session strings and bot tokens are stored in plaintext in MongoDB
`plugins/test.py` (`CLIENT.add_session`, `CLIENT.add_bot`) → `db.bots` stores `session` / `token` as-is. A pyrogram session string gives **full control of the user's Telegram account**. Anyone with DB read access (a leaked URI, a backup, or a hosting operator) can take over every connected account.
**Fix:** encrypt at rest with a key held only in env (`SESSION_KEY`, Fernet/AES-GCM). Decrypt only when starting a client. Rotate the key = re-login.

### H2 — User-supplied `db_uri` (SSRF + credential echo)
`plugins/settings.py:305-339` accepts an arbitrary MongoDB URI from any bot user and stores it. `plugins/utils.py:63` passes it on to the duplicate checker. The bot server will then connect to **any host/port the user names**, including internal services on the host network. The URI (with its password) is also echoed back in a callback alert (`settings.py:336`).
**Fix:** drop per-user `db_uri`. Do duplicate detection in the bot's own DB (a hash of `file_unique_id` per task). Never display stored credentials.

### H3 — User-controlled `str.format` on captions
`plugins/regix.py:360` `caption.format(filename=…, size=…, caption=…)`. `plugins/settings.py:242` "validates" the template with the same call. Python format strings allow attribute/index traversal (`{caption.__class__.__mro__}` etc.) and raise `KeyError`/`IndexError` on unknown fields. The result is info leakage of object internals, and a forward that crashes on every message.
**Fix:** use a safe substitution that only replaces the literal tokens `{filename}`, `{size}`, `{caption}` (plain `str.replace`). Unknown braces stay as text.

---

## 2. Medium

### M1 — Dashboard token compared with `==`
`main.py:52` `return token == API_TOKEN` is not constant-time, so it is timing-attackable. It only matters when `ENABLE_DASHBOARD=1`.
**Fix:** `hmac.compare_digest`. Refuse to start the dashboard if `API_TOKEN` is empty or short (< 32 chars).

### M2 — Userbot client writes `USERBOT.session` to disk, shared name
`plugins/test.py` `CLIENT.client` creates `Client("USERBOT", session_string=…)` without `in_memory=True`. Every user's session is written to the **same** file in the working dir. This causes cross-user state bleed and leaves a credential on disk/in the image.
**Fix:** `in_memory=True` and a unique name per client.

### M3 — Error text from Telegram/Mongo sent to users verbatim
Several handlers do `await m.edit(e)` / `f"<code>{e}</code>"` (e.g. `regix.py` pub_ start failure). This can expose internal hostnames, URIs or stack details, and an unescaped `<` breaks HTML parse mode.
**Fix:** log the full error. Show the user a short, HTML-escaped message.

### M4 — No authorization on long-running resources
Any Telegram user can `/start` the bot, add a session, and run forwards, live watchers and schedules. On a shared host that means unbounded CPU, bandwidth and download disk use (restricted-content download path writes to temp).
**Fix:** optional allow-list env (`ALLOWED_USERS`). Per-user caps on live watchers/schedules. Keep the existing 3-concurrent-batch cap.

### M5 — Live watcher keyed by `(from_id, to_id)`, not per user
`plugins/regix.py` `_live_watcher` / `_start_live_watcher` use `temp.LIVE_WATCHERS[(from_id, to_id)]`. User B can't create the same pair, and stopping by pair can affect another user's watcher.
**Fix:** key by task id (fixed by the per-forward task refactor).

---

## 3. Low / hardening

- **L1** `database.py:7` `mongodb_version()` uses a synchronous `MongoClient`, which blocks the event loop.
- **L2** `Translation.START_TXT.format(user.first_name)` puts the user's display name into HTML unescaped, so a name with `<` breaks rendering. Escape with `html.escape`.
- **L3** `add_bot` / `add_session` continue after the client fails to start and then use an undefined variable (`NameError`). The token/session is never validated and the user gets a confusing error.
- **L4** Catch-all private message handlers (`regix.live_guided_flow`, `connections.schedule_flow`, `save_restricted.save_flow`) and two `/cancel` handlers compete. Input meant for one flow can be consumed by another. That is a logic-integrity risk, not a direct exploit.
- **L5** Dependencies are not pinned to hashes. Pin versions in `requirements.txt` and run `pip-audit` in CI.
- **L6** Download path: temp files are cleaned in `finally`, good. But there is no size cap before download, so a huge file can fill the disk. Respect the task's size filter **before** downloading.

---

## 4. Fix status on this branch

| ID | Status |
|---|---|
| H3 safe caption | **fixed**: `plugins/engine.py` `render_caption` (plain replace, filename HTML-escaped). Used by batch and live. |
| M1 constant-time token | **fixed**: `main.py` `check_auth` uses `hmac.compare_digest`, refuses tokens < 32 chars |
| M2 in-memory userbot | **fixed**: `plugins/test.py` `CLIENT.client` uses `in_memory=True` with a per-user name |
| M3 raw errors to users | **partial**: batch start + add_bot/add_session now escape and truncate. Other handlers still open. |
| L3 add_bot/add_session crash | **fixed**: returns on start failure, temp client stopped |
| L6 size before download | **partial**: messages rejected by the task's size filter are never downloaded. No hard cap without a filter. |
| M5 live key per user | **open**: still `(from_id, to_id)`. Planned with the task-id refactor (Phase 3). |
| H1 sessions/tokens at rest | **fixed**: `secret_box.py` (Fernet, `enc:v1:` prefix). Encrypted on write in `database.py` (`add_bot`, `save_connection`), decrypted only in `plugins/test.py` `CLIENT.client`. `db.migrate_secrets()` encrypts old plaintext on startup. Key = `SESSION_KEY` env; without it a key derived from API_ID/API_HASH is used (weaker: set `SESSION_KEY`). |
| H2, M4, L1, L2, L4, L5 | **open** |
