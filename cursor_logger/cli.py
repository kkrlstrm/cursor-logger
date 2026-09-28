# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""cursor-logger CLI: ingest Cursor agent sessions and inspect the telemetry.

    python3 -m cursor_logger ingest [--watch] [--force] [--verbose]
    python3 -m cursor_logger sessions [--limit N] [--days N]
    python3 -m cursor_logger inspect <session-id-prefix>
    python3 -m cursor_logger stats
    python3 -m cursor_logger failures [--list]

DB target: --db, or $CURSOR_LOGGER_DB, else NEON_TELEMETRY_URL from the repo .env,
else ~/.cursor-logger/cursor.db (SQLite).
"""
from __future__ import annotations

import argparse
import sys

from .ingest import ingest_once, watch
from .parse import DEFAULT_STATE_DB
from .store import open_store


def _n(v):
    return f"{v:,}" if isinstance(v, int) else (v or "-")


def cmd_ingest(a):
    if a.watch:
        watch(db=a.db, state_db=a.state_db, interval=a.interval)
        return
    s = ingest_once(db=a.db, state_db=a.state_db, force=a.force, verbose=a.verbose)
    print(f"composers {s['composers']}  ingested {s['ingested']}  skipped {s['skipped']}  "
          f"empty {s['empty']}  failed {s['failed']}")
    if s["failed"]:
        return 1


def cmd_sessions(a):
    store = open_store(a.db)
    where, params = "", []
    if a.days:
        where = "WHERE started_at >= datetime('now', ?)"
        params.append(f"-{int(a.days)} days")
    rows = store.query(
        f"""SELECT session_id, name, model, models_seen, mode, num_turns, num_tool_calls,
                   started_at, workspace, parent_session_id
            FROM {{P}}sessions {where} ORDER BY started_at DESC LIMIT ?""",
        (*params, a.limit))
    if not rows:
        print("no sessions ingested yet — run:  python3 -m cursor_logger ingest")
        return
    print(f"{'session':9} {'model':14} {'turns':>5} {'calls':>5}  {'started':19}  name")
    for r in rows:
        model = r["models_seen"] or r["model"] or "-"
        sub = " (subagent)" if r["parent_session_id"] else ""
        print(f"{r['session_id'][:8]:9} {model:14.14} {_n(r['num_turns']):>5} "
              f"{_n(r['num_tool_calls']):>5}  {str(r['started_at'] or '')[:19]:19}  "
              f"{(r['name'] or '')[:50]}{sub}")
    store.close()


def cmd_inspect(a):
    store = open_store(a.db)
    rows = store.query("SELECT * FROM {P}sessions WHERE session_id LIKE ? LIMIT 2",
                       (a.session + "%",))
    if len(rows) != 1:
        print(f"{'no' if not rows else 'more than one'} session matching {a.session!r}")
        return 1
    s = rows[0]
    print(f"session   {s['session_id']}  {s['name'] or ''}")
    print(f"model     setting={s['model']}  seen={s['models_seen']}  mode={s['mode']}")
    print(f"workspace {s['workspace'] or '-'}  (project dir {s['project_slug'] or '-'})")
    if s["parent_session_id"]:
        print(f"parent    {s['parent_session_id']}")
    print(f"time      {s['started_at']} -> {s['updated_at']}")
    print(f"context   {_n(s['context_tokens_used'])} / {_n(s['context_token_limit'])} tokens")
    print(f"turns     {s['num_turns']}   messages {s['num_messages']}   "
          f"tool_calls {s['num_tool_calls']}   +{_n(s['lines_added'])}/-{_n(s['lines_removed'])} lines")
    calls = store.query(
        "SELECT seq, tool_name, status, exit_code, outcome_basis, target FROM {P}tool_calls "
        "WHERE session_id=? ORDER BY seq", (s["session_id"],))
    if calls:
        print("\ntool calls:")
        for c in calls:
            code = "" if c["exit_code"] is None else f"exit {c['exit_code']}"
            tgt = (c["target"] or "").replace("\n", " ")
            print(f"  {c['seq']:>3} {(c['tool_name'] or '?'):24.24} {c['status']:8.8} "
                  f"{code:8.8} {tgt[:80]}")
    store.close()


def cmd_stats(a):
    store = open_store(a.db)
    print("by model (the model named on each request; 'default' = Auto):")
    for r in store.query(
        """SELECT COALESCE(model, '-') AS model, COUNT(*) AS n,
                  COUNT(DISTINCT session_id) AS sessions
           FROM {P}tool_calls GROUP BY 1 ORDER BY n DESC"""):
        print(f"  {r['model']:20.20} {r['sessions']:>4} sessions {r['n']:>7} calls")
    print("\ntop tools:")
    for r in store.query(
        """SELECT tool_name, COUNT(*) AS n,
                  SUM(CASE WHEN status IN ('failure','rejected') THEN 1 ELSE 0 END) AS bad,
                  SUM(CASE WHEN status IN ('unknown','error') THEN 1 ELSE 0 END) AS unk
           FROM {P}tool_calls GROUP BY tool_name ORDER BY n DESC LIMIT 15"""):
        print(f"  {(r['tool_name'] or '?'):26.26} {r['n']:>6}  {r['bad'] or 0} bad  "
              f"{r['unk'] or 0} unsettled")
    print("\noutcomes (status / basis):")
    for r in store.query(
        """SELECT status, outcome_basis, COUNT(*) AS n FROM {P}tool_calls
           GROUP BY status, outcome_basis ORDER BY n DESC"""):
        print(f"  {r['status']:10.10} {r['outcome_basis']:18.18} {r['n']:>6}")
    store.close()


def cmd_failures(a):
    store = open_store(a.db)
    total = store.query("SELECT COUNT(*) AS c FROM {P}v_failures")[0]["c"]
    if not total:
        print("no failures recorded")
        return
    print(f"{total} failures\n\nby tool + outcome:")
    for r in store.query(
        """SELECT tool_name, outcome, COUNT(*) AS n FROM {P}v_failures
           GROUP BY tool_name, outcome ORDER BY n DESC LIMIT 20"""):
        print(f"  {(r['tool_name'] or '?'):26.26} {r['outcome']:9.9} {r['n']:>5}")
    if a.list:
        print("\nmost recent:")
        for r in store.query(
            """SELECT ts, session_id, tool_name, outcome, target, error_text
               FROM {P}v_failures ORDER BY ts DESC LIMIT ?""", (a.limit,)):
            head = (r["error_text"] or r["target"] or "").replace("\n", " ")
            print(f"  {str(r['ts'] or '')[:19]}  {r['session_id'][:8]}  "
                  f"{(r['tool_name'] or '?'):22.22} {r['outcome']:9.9} {head[:80]}")
    store.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="cursor-logger",
                                description="Cursor agent telemetry from Cursor's local store")
    p.add_argument("--db", help="sqlite path or postgresql:// URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="load new/changed Cursor sessions")
    pi.add_argument("--state-db", default=DEFAULT_STATE_DB)
    pi.add_argument("--watch", action="store_true", help="poll continuously")
    pi.add_argument("--interval", type=float, default=30.0)
    pi.add_argument("--force", action="store_true", help="re-parse every session")
    pi.add_argument("--verbose", action="store_true")
    pi.set_defaults(func=cmd_ingest)

    ps = sub.add_parser("sessions", help="list recent sessions")
    ps.add_argument("--limit", type=int, default=30)
    ps.add_argument("--days", type=int)
    ps.set_defaults(func=cmd_sessions)

    px = sub.add_parser("inspect", help="one session in detail")
    px.add_argument("session", help="session id or unique prefix")
    px.set_defaults(func=cmd_inspect)

    pt = sub.add_parser("stats", help="model + tool + outcome aggregates")
    pt.set_defaults(func=cmd_stats)

    pf = sub.add_parser("failures", help="what went wrong")
    pf.add_argument("--list", action="store_true")
    pf.add_argument("--limit", type=int, default=20)
    pf.set_defaults(func=cmd_failures)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
