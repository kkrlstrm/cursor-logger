# cursor-logger

Telemetry for **Cursor's agent**, the sibling of [`cc-logger`](https://github.com/kkrlstrm/cc-logger)
(Claude Code) and [`codex-logger`](https://github.com/kkrlstrm/codex-logger) (Codex). It records
every Cursor agent session, turn, message, tool call, outcome and model into the same warehouse,
as `cursor_*` tables. [RunTune](https://github.com/kkrlstrm/runtune) reads those tables as its
`cursor` source; RunTune can also read Cursor's store itself with `runtune record cursor`.

## Where the data comes from

Cursor keeps every agent conversation in one SQLite file:
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`, table `cursorDiskKV`.

| key | what it holds |
|---|---|
| `composerData:<id>` | one session: name, model setting, mode, created/updated times, context-window use, lines changed, subagent ids, ordered message ids |
| `bubbleId:<id>:<bubble>` | one message. `type` 1 = user, 2 = assistant. Assistant bubbles carry text, thinking, or one tool call (`toolFormerData`: name, params, result, status, error) |

cursor-logger opens that file **read-only** and installs nothing into Cursor. It backfills
every session already on disk.

This is Cursor's internal format (`composerData._v` 18 on Cursor 3.21.18), not a
documented API. A session that stops parsing after a Cursor update is skipped, reported
on stderr, counted as `failed`, and retried on the next run. It is never half-written.

### Sources considered and not used

- **`~/.cursor/projects/*/agent-transcripts/*.jsonl`**: no timestamps, no model, no tool
  results, no call ids, and only 20 of 31 sessions. The directory names are still read,
  because they record which project folder a session ran in, which the DB does not.
- **Cursor hooks** (`~/.cursor/hooks.json`): this build has `sessionStart`, `preToolUse`,
  `postToolUse`, `postToolUseFailure`, `subagentStart/Stop`, `afterAgentResponse` and more.
  They see only sessions from install onward and need a running receiver. They are the
  fallback if Cursor ever stops writing tool results to the DB.
- **`~/.cursor/ai-tracking/ai-code-tracking.db`**: hashes of AI-written lines per file and
  model (for Cursor's "% AI code" metric). Not agent activity.

## Outcomes

Every tool call gets a `status` and the `outcome_basis` it rests on:

| status | when |
|---|---|
| `success` | the tool completed; for a shell call, exit code 0 |
| `failure` | Cursor marked the tool `error`, the result carries an error, or a shell exit code was nonzero |
| `rejected` | the user declined the call |
| `aborted` | cancelled, or a shell command ended `ABORTED` |
| `error` / `unknown` | no result recorded. Never counted as success |

**The exit-code inference.** For shell calls, `completed` only means the command ran.
`exitCodeV2` stores 0 explicitly, but the older `exitCode` field is written only when
nonzero (protobuf omits default values). Across 166 recorded shell calls it was never
stored as 0. So a completed shell call with no exit-code field is recorded as exit 0 with
`outcome_basis = 'exit_code_omitted'`: 130 of 166 shell calls on this machine. Filter on
that column if you want only calls whose exit code Cursor stated.

Error text comes from `toolFormerData.error.modelVisibleErrorMessage` (what the model was
told), else the tool's own `result.error`, else the last lines of shell output.

## What it cannot see

- **Billed tokens.** Cursor keeps no per-request usage locally. `tokenCount` is filled on
  about 1% of messages. `context_tokens_used` is how full the context window was, not what
  was billed. Real usage is on the Cursor dashboard only.
- **The real model under Auto.** `model = 'default'` means Auto picked it, and the store
  does not say which model it picked.
- **Workspace for every session.** 25 of 31 sessions resolve to a folder. The rest ran in
  windows whose workspace record Cursor has since dropped.

## Install and run

```bash
cd cursor-logger
python3 -m cursor_logger ingest --verbose     # load every session on disk
python3 -m cursor_logger sessions             # recent sessions
python3 -m cursor_logger inspect <id-prefix>  # one session in detail
python3 -m cursor_logger stats                # models, tools, outcomes
python3 -m cursor_logger failures --list      # what went wrong
```

DB target, first match wins: `--db`, `$CURSOR_LOGGER_DB`, `NEON_TELEMETRY_URL` from the
enclosing repo's `.env`, then `~/.cursor-logger/cursor.db`. Postgres needs
`pip install 'psycopg[binary]'`; run it with the framework `python3`, not `/usr/bin/python3`
(which has no psycopg, so the write would fail on every run).

A run with nothing new costs one index-only count per session (~0.04s for 232 composers).

## Storage

`sessions`, `tool_calls`, `messages`, `turns`, `ingest_state` and the `v_failures` view
(prefixed `cursor_` in Postgres). See [`cursor_logger/store.py`](cursor_logger/store.py).

A Cursor session is **not append-only**: restoring a checkpoint deletes the messages after
it. A changed session is therefore replaced in one transaction (its rows deleted, the new
parse inserted), not upserted, so rewound tool calls do not stay in the data.

Every text field is redacted before it is written (`redact.py`, copied from codex-logger).
Cursor's own `state.vscdb` is not redacted; this tool only reads it.

## Schedule (launchd)

```bash
cp launchd/com.kaikarlstrom.cursor-logger.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kaikarlstrom.cursor-logger.plist
launchctl start com.kaikarlstrom.cursor-logger
```

Every 5 minutes. Logs: `~/Library/Logs/cursor-logger.{out,err}.log`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
