# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage backends for parsed Cursor sessions.

Default: SQLite at ~/.cursor-logger/cursor.db. With a postgresql:// URL (or
NEON_TELEMETRY_URL in the repo .env) it writes cursor_* tables into the same
telemetry warehouse as cc-logger and codex-logger. Every session row carries
source='cursor'.

A Cursor session is NOT append-only: restoring a checkpoint deletes the bubbles
after it. So a changed session is replaced (delete its rows, insert the new
parse) inside one transaction, rather than upserted. An upsert would keep the
rewound messages and tool calls as if they still happened.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from .parse import Session
from .redact import redact

DEFAULT_DB = os.path.expanduser("~/.cursor-logger/cursor.db")
TRUNCATE_LEN = 200_000  # same guard as codex-logger: one runaway output can't bloat a row

# {P} = table prefix, {TS} = timestamp type, {BIG} = 64-bit int type
SCHEMA = """
CREATE TABLE IF NOT EXISTS {P}sessions (
    session_id          TEXT PRIMARY KEY,
    source              TEXT DEFAULT 'cursor',
    name                TEXT,
    subtitle            TEXT,
    mode                TEXT,
    is_agentic          BOOLEAN,
    model               TEXT,
    models_seen         TEXT,
    workspace           TEXT,
    project_slug        TEXT,
    parent_session_id   TEXT,
    status              TEXT,
    started_at          {TS},
    updated_at          {TS},
    num_turns           INTEGER,
    num_messages        INTEGER,
    num_tool_calls      INTEGER,
    context_tokens_used {BIG},
    context_token_limit {BIG},
    input_tokens        {BIG},
    output_tokens       {BIG},
    lines_added         INTEGER,
    lines_removed       INTEGER,
    files_changed       INTEGER,
    cursor_version      TEXT,
    ingested_at         {TS}
);
CREATE TABLE IF NOT EXISTS {P}tool_calls (
    session_id    TEXT,
    call_id       TEXT,
    seq           INTEGER,
    bubble_id     TEXT,
    turn_seq      INTEGER,
    tool_name     TEXT,
    arguments     TEXT,
    output        TEXT,
    command       TEXT,
    target        TEXT,
    cwd           TEXT,
    status        TEXT,
    exit_code     INTEGER,
    outcome_basis TEXT,
    error_text    TEXT,
    model         TEXT,
    ts            {TS},
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS {P}messages (
    session_id  TEXT,
    seq         INTEGER,
    bubble_id   TEXT,
    turn_seq    INTEGER,
    role        TEXT,
    kind        TEXT,
    text        TEXT,
    model       TEXT,
    thinking_ms INTEGER,
    ts          {TS},
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS {P}turns (
    session_id          TEXT,
    turn_seq            INTEGER,
    request_id          TEXT,
    model               TEXT,
    turn_duration_ms    {BIG},
    context_tokens_used {BIG},
    context_token_limit {BIG},
    input_tokens        {BIG},
    output_tokens       {BIG},
    num_tool_calls      INTEGER,
    ts                  {TS},
    PRIMARY KEY (session_id, turn_seq)
);
CREATE TABLE IF NOT EXISTS {P}ingest_state (
    session_id  TEXT PRIMARY KEY,
    signature   TEXT,
    updated_at  {TS}
);
CREATE INDEX IF NOT EXISTS idx_{P}tool_calls_status ON {P}tool_calls(status);
CREATE INDEX IF NOT EXISTS idx_{P}sessions_started ON {P}sessions(started_at);
"""

FAILURE_VIEW = """
{DROP}
CREATE VIEW {P}v_failures AS
    SELECT t.ts, t.session_id, t.model, COALESCE(t.cwd, s.workspace) AS cwd,
           t.tool_name, t.status AS outcome, t.exit_code, t.outcome_basis,
           COALESCE(t.command, t.target) AS target, t.error_text
      FROM {P}tool_calls t JOIN {P}sessions s USING (session_id)
     WHERE t.status IN ('failure', 'rejected', 'aborted', 'error');
"""

_CHILD_TABLES = ("tool_calls", "messages", "turns")


def _scrub(v: Optional[str]) -> Optional[str]:
    """Redact secrets, then clip: redaction first, so truncation cannot cut a
    pattern in half and let the tail through."""
    if v is None:
        return None
    v = redact(str(v))
    if len(v) > TRUNCATE_LEN:
        return v[:TRUNCATE_LEN] + f"\n...[truncated {len(v) - TRUNCATE_LEN} chars]"
    return v


_clip = _scrub


class BaseStore:
    prefix = ""
    ph = "?"
    now = "datetime('now')"

    def _sql(self, sql: str) -> str:
        return sql.replace("{P}", self.prefix).replace("?", self.ph).replace("{NOW}", self.now)

    def _exec(self, sql, params=()):
        raise NotImplementedError

    def _many(self, sql, rows):
        raise NotImplementedError

    def signatures(self) -> dict:
        """Every ingested session's signature, in one query: over a remote DSN a
        per-session lookup costs a round trip each, which dominated an idle run."""
        rows = self.query(self._sql("SELECT session_id, signature FROM {P}ingest_state"), raw=True)
        return {r["session_id"]: r["signature"] for r in rows}

    def mark_ingested(self, session_id: str, signature: str):
        self._exec(self._sql(
            "INSERT INTO {P}ingest_state(session_id, signature, updated_at) VALUES(?,?,{NOW}) "
            "ON CONFLICT(session_id) DO UPDATE SET signature=excluded.signature, "
            "updated_at=excluded.updated_at"), (session_id, signature))

    def write_session(self, s: Session):
        for t in _CHILD_TABLES:
            self._exec(self._sql(f"DELETE FROM {{P}}{t} WHERE session_id=?"), (s.session_id,))
        self._exec(self._sql("DELETE FROM {P}sessions WHERE session_id=?"), (s.session_id,))
        self._exec(self._sql(
            """INSERT INTO {P}sessions(session_id, source, name, subtitle, mode, is_agentic,
                model, models_seen, workspace, project_slug, parent_session_id, status,
                started_at, updated_at, num_turns, num_messages, num_tool_calls,
                context_tokens_used, context_token_limit, input_tokens, output_tokens,
                lines_added, lines_removed, files_changed, cursor_version, ingested_at)
               VALUES(?,'cursor',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,{NOW})"""),
            (s.session_id, _clip(s.name), _clip(s.subtitle), s.mode, s.is_agentic, s.model,
             s.models_seen, s.workspace, s.project_slug, s.parent_session_id, s.status,
             s.started_at, s.updated_at, s.num_turns, s.num_messages, s.num_tool_calls,
             s.context_tokens_used, s.context_token_limit, s.input_tokens, s.output_tokens,
             s.lines_added, s.lines_removed, s.files_changed, s.cursor_version))
        self._many(self._sql(
                """INSERT INTO {P}tool_calls(session_id, call_id, seq, bubble_id, turn_seq,
                    tool_name, arguments, output, command, target, cwd, status, exit_code,
                    outcome_basis, error_text, model, ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id, call_id) DO NOTHING"""),
            [(s.session_id, c.call_id, c.seq, c.bubble_id, c.turn_seq, c.tool_name,
              _clip(c.arguments), _clip(c.output), _clip(c.command), _clip(c.target),
              c.cwd, c.status, c.exit_code, c.outcome_basis, _clip(c.error_text),
              c.model, c.ts) for c in s.tool_calls])
        self._many(self._sql(
                """INSERT INTO {P}messages(session_id, seq, bubble_id, turn_seq, role, kind,
                    text, model, thinking_ms, ts) VALUES(?,?,?,?,?,?,?,?,?,?)"""),
            [(s.session_id, m.seq, m.bubble_id, m.turn_seq, m.role, m.kind, _clip(m.text),
              m.model, m.thinking_ms, m.ts) for m in s.messages])
        self._many(self._sql(
                """INSERT INTO {P}turns(session_id, turn_seq, request_id, model,
                    turn_duration_ms, context_tokens_used, context_token_limit,
                    input_tokens, output_tokens, num_tool_calls, ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)"""),
            [(s.session_id, t.turn_seq, t.request_id, t.model, t.turn_duration_ms,
              t.context_tokens_used, t.context_token_limit, t.input_tokens,
              t.output_tokens, t.num_tool_calls, t.ts) for t in s.turns])


class SQLiteStore(BaseStore):
    def __init__(self, path: str = DEFAULT_DB):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.format(P="", TS="TEXT", BIG="INTEGER"))
        self.conn.executescript(FAILURE_VIEW.format(P="", DROP="DROP VIEW IF EXISTS v_failures;"))
        self.conn.commit()

    def _exec(self, sql, params=()):
        self.conn.execute(sql, params)

    def _many(self, sql, rows):
        self.conn.executemany(sql, rows)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def query(self, sql: str, params=(), raw: bool = False):
        return [dict(r) for r in self.conn.execute(sql.replace("{P}", ""), params).fetchall()]

    def close(self):
        self.conn.close()


def _telemetry_dsn_from_env_file() -> Optional[str]:
    """NEON_TELEMETRY_URL from the enclosing repo's .env — the one place that
    DSN lives (same fallback codex-logger uses, so the launchd plist can ship
    without a secret in it)."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        text = env_path.read_text()
    except OSError:
        return None
    m = re.search(r"^NEON_TELEMETRY_URL\s*=\s*['\"]?([^'\"\n]+)", text, re.M)
    return m.group(1).strip() if m else None


def open_store(db: Optional[str] = None):
    """sqlite by default; postgres for a URL."""
    db = (db or os.environ.get("CURSOR_LOGGER_DB")
          or _telemetry_dsn_from_env_file() or DEFAULT_DB)
    if db.startswith("postgres://") or db.startswith("postgresql://"):
        from .store_pg import PostgresStore  # lazy: psycopg only needed here
        return PostgresStore(db)
    return SQLiteStore(db)
