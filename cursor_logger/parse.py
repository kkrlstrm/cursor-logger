# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read Cursor agent sessions out of Cursor's own SQLite store.

Cursor keeps every agent conversation (a "composer") in one SQLite file,
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb, table
cursorDiskKV:

    composerData:<composerId>            one row per session: name, model config,
                                         mode, createdAt / lastUpdatedAt, context-window
                                         use, lines changed, subagent ids, and the ordered
                                         bubble ids (fullConversationHeadersOnly)
    bubbleId:<composerId>:<bubbleId>     one row per message. type 1 = user, 2 = assistant.
                                         An assistant bubble carries text, thinking, or one
                                         tool call in toolFormerData (name, params, result,
                                         status)

This is Cursor's internal format (composerData._v 18 on Cursor 3.21), not a
documented API. Every field is read defensively and a session that no longer
parses is skipped and counted, never half-written.

The JSONL files under ~/.cursor/projects/*/agent-transcripts are NOT used as the
source: they have no timestamps, no model, no tool results and no tool-call ids,
and they cover only some sessions. Their directory names are still useful: they
say which project folder a session ran in, which the DB does not record.

Outcome rules (see README "Outcomes"):
  * toolFormerData.status is the runtime's verdict: completed / error / cancelled.
  * For shell calls, `completed` only means the command ran. The exit code decides.
    `exitCodeV2` stores 0 explicitly; the older `exitCode` field is written only when
    nonzero (protobuf omits default values; across 166 recorded shell calls it was
    never stored as 0). So a completed shell call with no exit-code field is recorded
    as exit 0 with outcome_basis='exit_code_omitted', which keeps the inference
    visible and filterable.
  * A tool bubble with no tool name is recorded as status 'error' when Cursor marked
    it so, else 'unknown'. 'unknown' never counts as success.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

DEFAULT_STATE_DB = os.path.expanduser(
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
DEFAULT_WORKSPACE_STORAGE = os.path.expanduser(
    "~/Library/Application Support/Cursor/User/workspaceStorage")
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.cursor/projects")
CURSOR_APP_PACKAGE = "/Applications/Cursor.app/Contents/Resources/app/package.json"

SHELL_TOOLS = {"run_terminal_command_v2", "run_terminal_cmd"}
# params key -> what the call acted on, first match wins
_TARGET_KEYS = ("command", "targetFile", "relativeWorkspacePath", "path",
                "targetDirectory", "globPattern", "pattern", "url", "searchTerm",
                "query")


@dataclass
class ToolCall:
    call_id: str
    seq: int
    bubble_id: str
    tool_name: Optional[str]
    arguments: Optional[str]
    output: Optional[str]
    command: Optional[str]
    target: Optional[str]
    cwd: Optional[str]
    status: str               # success | failure | rejected | aborted | error | unknown
    exit_code: Optional[int]
    outcome_basis: str        # exit_code | exit_code_omitted | tool_status | rejected | result_error | none
    error_text: Optional[str]
    model: Optional[str]
    ts: Optional[str]
    turn_seq: Optional[int]


@dataclass
class Message:
    seq: int
    bubble_id: str
    role: str                 # user | assistant
    kind: str                 # text | thinking
    text: str
    model: Optional[str]
    ts: Optional[str]
    thinking_ms: Optional[int]
    turn_seq: Optional[int]


@dataclass
class Turn:
    turn_seq: int
    request_id: Optional[str]
    model: Optional[str]
    ts: Optional[str]
    turn_duration_ms: Optional[int] = None
    context_tokens_used: Optional[int] = None
    context_token_limit: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    num_tool_calls: int = 0


@dataclass
class Session:
    session_id: str
    name: Optional[str]
    subtitle: Optional[str]
    mode: Optional[str]
    is_agentic: Optional[bool]
    model: Optional[str]            # the model picker setting; "default" means Auto
    models_seen: Optional[str]      # every model named on a request, comma-joined
    workspace: Optional[str]
    project_slug: Optional[str]
    parent_session_id: Optional[str]
    status: Optional[str]
    started_at: Optional[str]
    updated_at: Optional[str]
    num_turns: int = 0
    num_messages: int = 0
    num_tool_calls: int = 0
    context_tokens_used: Optional[int] = None
    context_token_limit: Optional[int] = None
    input_tokens: int = 0           # summed bubble tokenCount; sparse, see README
    output_tokens: int = 0
    lines_added: Optional[int] = None
    lines_removed: Optional[int] = None
    files_changed: Optional[int] = None
    cursor_version: Optional[str] = None
    signature: str = ""             # lastUpdatedAt:bubbles, for incremental ingest
    tool_calls: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    turns: list = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _iso_ms(ms) -> Optional[str]:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _loads(v, default=None):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _int(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _thinking_text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("text") or ""
    return ""


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open Cursor's DB without ever writing to it. Cursor holds the file open in
    WAL mode; a read-only URI connection reads the committed state alongside it."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def cursor_version() -> Optional[str]:
    try:
        with open(CURSOR_APP_PACKAGE) as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ workspace

def _slug_to_path(slug: str) -> Optional[str]:
    """~/.cursor/projects dir names are the path with '/' -> '-'. Dashes inside a
    folder name make that ambiguous, so rebuild it against the filesystem: extend
    one component at a time and keep a '/' split only where that directory exists."""
    parts = slug.split("-")

    def walk(i: int, prefix: str) -> Optional[str]:
        for j in range(i + 1, len(parts) + 1):
            cand = prefix + "/" + "-".join(parts[i:j])
            if not os.path.isdir(cand):
                continue
            if j == len(parts):
                return cand
            got = walk(j, cand)
            if got:
                return got
        return None

    return walk(0, "")


def workspace_index(workspace_storage: str = DEFAULT_WORKSPACE_STORAGE,
                    projects_dir: str = DEFAULT_PROJECTS_DIR) -> dict:
    """composerId -> (workspace path or None, project slug or None).

    Two independent sources, because neither covers every session:
      1. workspaceStorage/<hash>/state.vscdb lists the composers opened in that
         window, and workspace.json names its folder.
      2. ~/.cursor/projects/<slug>/agent-transcripts/<composerId>/ exists for
         sessions Cursor wrote a transcript for.
    """
    out: dict[str, tuple] = {}
    if os.path.isdir(projects_dir):
        for slug in os.listdir(projects_dir):
            tdir = os.path.join(projects_dir, slug, "agent-transcripts")
            if not os.path.isdir(tdir):
                continue
            path = _slug_to_path(slug)
            for cid in os.listdir(tdir):
                out[cid] = (path, slug)
    if os.path.isdir(workspace_storage):
        for h in os.listdir(workspace_storage):
            d = os.path.join(workspace_storage, h)
            try:
                with open(os.path.join(d, "workspace.json")) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            folder = meta.get("folder") or meta.get("workspace") or ""
            if folder.startswith("file://"):
                folder = folder[len("file://"):]
            db = os.path.join(d, "state.vscdb")
            if not os.path.exists(db):
                continue
            try:
                con = connect_readonly(db)
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
                con.close()
            except sqlite3.Error:
                continue
            data = _loads(row[0], {}) if row else {}
            for c in (data or {}).get("allComposers", []) or []:
                cid = c.get("composerId")
                if cid:
                    slug = out.get(cid, (None, None))[1]
                    out[cid] = (folder or None, slug)
    return out


# ------------------------------------------------------------------ outcome

def classify(tf: dict) -> tuple:
    """-> (status, exit_code, outcome_basis, error_text)"""
    name = tf.get("name")
    st = tf.get("status")
    result = _loads(tf.get("result"))
    extra = _loads(tf.get("additionalData"), {})
    if not isinstance(extra, dict):
        # additionalData is sometimes a python-repr string ("{'status': 'error'}")
        s = str(tf.get("additionalData") or "")
        extra = {"status": "error"} if "'status': 'error'" in s else {}

    if st == "cancelled":
        return "aborted", None, "tool_status", None
    if st == "error":
        return "failure", None, "tool_status", _error_text(tf, result) or "tool reported error"
    if st != "completed":
        if extra.get("status") == "error":
            return "error", None, "tool_status", "tool errored before it ran"
        return "unknown", None, "none", None

    if isinstance(result, dict):
        if result.get("rejected") is True:
            return "rejected", None, "rejected", "rejected by user"
        if name in SHELL_TOOLS:
            reason = str(result.get("endedReason") or "")
            code = _int(result.get("exitCodeV2"))
            if code is None:
                code = _int(result.get("exitCode"))
            if reason.endswith("ABORTED"):
                return "aborted", code, "tool_status", reason
            if code is not None:
                if code == 0:
                    return "success", 0, "exit_code", None
                return "failure", code, "exit_code", _tail(result.get("output"))
            return "success", 0, "exit_code_omitted", None
        if result.get("error"):
            return "failure", None, "result_error", _error_text(tf, result)
    return "success", None, "tool_status", None


def _error_text(tf: dict, result) -> Optional[str]:
    """toolFormerData.error holds what the model was told; result.error is the
    tool's own (sometimes nested: {"error": {"error": "rg: ..."}})."""
    e = _loads(tf.get("error"))
    if isinstance(e, dict):
        msg = e.get("modelVisibleErrorMessage") or e.get("clientVisibleErrorMessage")
        if msg:
            return str(msg).strip()[:2000]
    r = result.get("error") if isinstance(result, dict) else None
    while isinstance(r, dict):
        r = r.get("error") or r.get("message")
    return str(r).strip()[:2000] if r else None


def _tail(out, n_lines: int = 4) -> Optional[str]:
    if not isinstance(out, str):
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return "\n".join(lines[-n_lines:])[:2000] or None


def _target(params) -> Optional[str]:
    if not isinstance(params, dict):
        return None
    for k in _TARGET_KEYS:
        v = params.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# ------------------------------------------------------------------ sessions

def _bubbles(con: sqlite3.Connection, cid: str) -> dict:
    # range scan on the key index: 'bubbleId:<cid>:' .. 'bubbleId:<cid>;'
    rows = con.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
        (f"bubbleId:{cid}:", f"bubbleId:{cid};")).fetchall()
    out = {}
    for r in rows:
        b = _loads(r["value"])
        if isinstance(b, dict):
            out[r["key"].rsplit(":", 1)[1]] = b
    return out


def signature(con: sqlite3.Connection, meta: dict) -> str:
    """lastUpdatedAt + bubble count. The count is an index-only range scan, so
    checking whether a session changed never reads its bubbles."""
    cid = meta["composerId"]
    n = con.execute("SELECT count(*) FROM cursorDiskKV WHERE key >= ? AND key < ?",
                    (f"bubbleId:{cid}:", f"bubbleId:{cid};")).fetchone()[0]
    return f"{meta.get('lastUpdatedAt')}:{n}"


def parse_composer(con: sqlite3.Connection, meta: dict, where: tuple = (None, None),
                   parent: Optional[str] = None, version: Optional[str] = None
                   ) -> Optional[Session]:
    cid = meta.get("composerId")
    headers = meta.get("fullConversationHeadersOnly") or []
    if not cid or not headers:
        return None                      # an empty/draft composer: nothing happened
    bubbles = _bubbles(con, cid)
    order = [h.get("bubbleId") for h in headers if h.get("bubbleId") in bubbles]
    seen = set(order)
    order += sorted((k for k in bubbles if k not in seen),
                    key=lambda k: bubbles[k].get("createdAt") or "")

    cfg_model = (meta.get("modelConfig") or {}).get("modelName")
    s = Session(
        session_id=cid, name=meta.get("name"), subtitle=meta.get("subtitle"),
        mode=meta.get("unifiedMode") or meta.get("forceMode"),
        is_agentic=meta.get("isAgentic"), model=cfg_model, models_seen=None,
        workspace=where[0], project_slug=where[1], parent_session_id=parent,
        status=meta.get("status"), started_at=_iso_ms(meta.get("createdAt")),
        updated_at=_iso_ms(meta.get("lastUpdatedAt")),
        context_tokens_used=_int(meta.get("contextTokensUsed")),
        context_token_limit=_int(meta.get("contextTokenLimit")),
        lines_added=_int(meta.get("totalLinesAdded")),
        lines_removed=_int(meta.get("totalLinesRemoved")),
        files_changed=_int(meta.get("filesChangedCount")),
        cursor_version=version,
        signature=f"{meta.get('lastUpdatedAt')}:{len(bubbles)}",
    )
    models: list[str] = []
    model = cfg_model
    turn: Optional[Turn] = None
    mseq = tseq = 0
    last_ts = s.started_at     # a bubble with no createdAt inherits its predecessor's
    for bid in order:
        b = bubbles[bid]
        ts = (b.get("createdAt") if isinstance(b.get("createdAt"), str)
              else _iso_ms(b.get("createdAt"))) or last_ts
        last_ts = ts
        mi = (b.get("modelInfo") or {}).get("modelName")
        if mi:
            model = mi
            if mi not in models:
                models.append(mi)
        tc = b.get("tokenCount") or {}
        if b.get("type") == 1:
            cw = b.get("contextWindowStatusAtCreation") or {}
            turn = Turn(turn_seq=len(s.turns), request_id=b.get("requestId"), model=model,
                        ts=ts, context_tokens_used=_int(cw.get("tokensUsed")),
                        context_token_limit=_int(cw.get("tokenLimit")))
            s.turns.append(turn)
        if turn is not None:
            turn.input_tokens += _int(tc.get("inputTokens")) or 0
            turn.output_tokens += _int(tc.get("outputTokens")) or 0
            if _int(b.get("turnDurationMs")):
                turn.turn_duration_ms = max(turn.turn_duration_ms or 0, b["turnDurationMs"])
        s.input_tokens += _int(tc.get("inputTokens")) or 0
        s.output_tokens += _int(tc.get("outputTokens")) or 0
        role = "user" if b.get("type") == 1 else "assistant"
        tseq_now = turn.turn_seq if turn else None

        text = b.get("text")
        if isinstance(text, str) and text.strip():
            s.messages.append(Message(mseq, bid, role, "text", text, model, ts, None, tseq_now))
            mseq += 1
        think = _thinking_text(b.get("thinking"))
        if think.strip():
            s.messages.append(Message(mseq, bid, role, "thinking", think, model, ts,
                                      _int(b.get("thinkingDurationMs")), tseq_now))
            mseq += 1

        tf = b.get("toolFormerData")
        # user bubbles can carry a stub ({"additionalData": ...}) with no tool name
        # and no call id: nothing was called, so it is not a tool call
        if isinstance(tf, dict) and (tf.get("name") or tf.get("toolCallId")):
            status, code, basis, err = classify(tf)
            params = _loads(tf.get("params"))
            shell = tf.get("name") in SHELL_TOOLS
            result = _loads(tf.get("result"))
            cwd = None
            if isinstance(params, dict):
                cwd = params.get("cwd") or None
            if not cwd and isinstance(result, dict):
                cwd = result.get("resultingWorkingDirectory") or None
            s.tool_calls.append(ToolCall(
                call_id=tf.get("toolCallId") or bid, seq=tseq, bubble_id=bid,
                tool_name=tf.get("name"),
                arguments=tf.get("params") if isinstance(tf.get("params"), str) else json.dumps(params),
                output=(result.get("output") if shell and isinstance(result, dict)
                        else tf.get("result")),
                command=params.get("command") if shell and isinstance(params, dict) else None,
                target=_target(params), cwd=cwd, status=status, exit_code=code,
                outcome_basis=basis, error_text=err, model=model, ts=ts, turn_seq=tseq_now))
            tseq += 1
            if turn is not None:
                turn.num_tool_calls += 1

    s.models_seen = ",".join(models) or None
    s.num_turns = len(s.turns)
    s.num_messages = len(s.messages)
    s.num_tool_calls = len(s.tool_calls)
    return s


def iter_composers(con: sqlite3.Connection) -> Iterator[dict]:
    for r in con.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
        meta = _loads(r["value"])
        if isinstance(meta, dict) and meta.get("composerId"):
            yield meta


def parent_index(metas: list) -> dict:
    """subagent composerId -> parent composerId."""
    out = {}
    for m in metas:
        for child in (m.get("subagentComposerIds") or []) + (m.get("subComposerIds") or []):
            if isinstance(child, str):
                out[child] = m["composerId"]
    return out
