# Keybase platform plugin

A Hermes Agent gateway adapter for [Keybase](https://keybase.io) — end-to-end
encrypted DMs, team channels, and attachments — driven through the local
`keybase` CLI talking to a locally logged-in Keybase service.

Keybase is a **singleton service identity**, not a token-based account. The
local CLI talks to one running Keybase daemon, which owns one Keybase account.
This shapes the whole deployment model below.

## Architecture

```text
                 local Keybase service (one daemon, one account)
                 bound to one Unix service identity
┌─────────────────────────┐
│  keybase chat api-listen │  (subprocess, JSONL event stream)
│  keybase chat send       │
└────────────┬─────────────┘
             │ CLI (--home KEYBASE_HOME)
┌────────────▼─────────────┐
│  KeybaseAdapter          │  (Python, in gateway)
│  - parses api-listen JSON│
│  - dedupes own sends     │
│  - builds SessionSource  │
└────────────┬─────────────┘
             │ MessageEvent (source.profile stamped by gateway)
┌────────────▼─────────────┐
│  GatewayRunner          │
│  ProfileRoutedSessionStore
│   profiles/<name>/sessions/
│   profiles/<name>/state.db
└──────────────────────────┘
```

- **Inbound**: the adapter spawns `keybase chat api-listen` and parses its
  JSONL stream. Each message is deduplicated against recently-sent ids
  (echo-back suppression) and dispatched as a `MessageEvent`. Reconnect and
  health monitoring are supervised.
- **Outbound**: `send` / `send_typing` / attachment download go through the
  `keybase` CLI with a global `--home KEYBASE_HOME` flag prepended to every
  subcommand.
- **Profile binding**: under gateway multiplexing the adapter is started as a
  *secondary-profile* adapter. The gateway's profile message handler stamps
  `event.source.profile` and runs the turn inside that profile's
  `_profile_runtime_scope`, so `get_secret(...)` reads that profile's `.env`
  and sessions are owned by that profile (see below).

## Installation

This is a **user plugin** — it is not part of the core gateway tree.

Place the plugin under your Hermes home:

```text
$HERMES_HOME/plugins/platforms/keybase/__init__.py
```

The gateway discovers it automatically at startup (next to the bundled
`plugins/platforms/*` adapters). No core-code change is required.

## Configuration

Keybase configuration lives in the **profile's** `.env` (multiplex-aware via
`get_secret`):

| Variable               | Purpose                                                      |
|------------------------|--------------------------------------------------------------|
| `KEYBASE_HOME`         | Path passed as `--home` to the `keybase` CLI. Use a writable path such as `$HERMES_HOME/keybase-home`; do **not** point at a root-owned home. |
| `KEYBASE_HOME_CHANNEL` | Default conversation(s) cleared on `/reset` / `/clear` when no explicit chat id is given. Comma-separated (e.g. `dharbigt,kosima`). |
| `KEYBASE_ALLOWED_TEAMS`| Team-channel allowlist. Unset disables team channels; `*` allows all; otherwise an explicit list. |

The adapter resolves the `keybase` binary from `keybase_bin` (config extra) or
PATH. Register the platform under `gateway.platforms.keybase` in the profile's
`config.yaml`.

### Single-instance contract

A Keybase daemon owns one account, so **only one Hermes gateway process may
own that Keybase identity at a time**. The adapter takes a platform lock
(`keybase-account`, scoped to the profile) so two profiles cannot consume the
same credential concurrently. This prevents *accidental* double-use; it does
not create per-profile Keybase account isolation — that is not possible with a
singleton Keybase identity. Run Keybase and the gateway under the same non-root
Unix account; never run an internet-facing gateway as root merely to
accommodate a messaging client.

## Session ownership (per-profile)

Each profile owns its own session state on disk:

```text
profiles/<name>/sessions/sessions.json   # session index, key agent:<profile>:...
profiles/<name>/state.db                 # SQLite mirror the dashboard reads
```

Keybase sessions are keyed `agent:<profile>:keybase:...` and are written to
the **active profile's** store, so they appear under that profile in the
dashboard — not under `default`. The `ProfileRoutedSessionStore` migrates any
legacy global `agent:<profile>:` rows into the correct profile dir on startup
(idempotent; re-runs every start).

## Operational notes

- **Restart recovery**: the adapter re-attaches to the Keybase service on
  gateway restart and resumes `api-listen`; in-flight conversations continue
  from the persisted session.
- **`/reset` and `/clear`**: `/clear` (and `/reset`) clears the local Hermes
  session and, where supported, deletes the visible Keybase chat history for
  the current conversation (via `clear_chat_history`).
- **Attachments**: downloaded through `keybase chat download` into a temp dir,
  then re-cached through the standard media helpers; oversized attachments are
  skipped.
- **Duplicate suppression**: messages the adapter itself just sent are
  suppressed from re-dispatch (recent-sent-id set, bounded to 50 entries).

## Security

Keybase provides end-to-end encryption and public-key identity verification —
real strengths. A secure transport does **not** by itself make agent tool
access safe: keep Hermes allowlists / DM pairing enabled for the Keybase
platform as for any other channel.
