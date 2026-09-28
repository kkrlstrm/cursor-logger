# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read Cursor's state.vscdb and write changed sessions to the store.

Incremental per session, not per file: the whole Cursor store is one SQLite file
that changes whenever any window does anything, so a file-level size/mtime skip
(what codex-logger uses) would re-read everything every run. Each composer's
lastUpdatedAt + bubble count is its signature; an unchanged session costs one
row read of composerData and nothing else.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Optional

from .parse import (DEFAULT_STATE_DB, connect_readonly, cursor_version, iter_composers,
                    parent_index, parse_composer, signature, workspace_index)
from .store import open_store


def ingest_once(db: Optional[str] = None, state_db: str = DEFAULT_STATE_DB,
                force: bool = False, verbose: bool = False) -> dict:
    stats = {"composers": 0, "ingested": 0, "skipped": 0, "empty": 0, "failed": 0}
    if not os.path.exists(state_db):
        raise SystemExit(f"cursor-logger: no Cursor store at {state_db}")
    src = connect_readonly(state_db)
    store = open_store(db)
    try:
        metas = list(iter_composers(src))
        parents = parent_index(metas)
        where = workspace_index()
        version = cursor_version()
        known = {} if force else store.signatures()
        for meta in metas:
            stats["composers"] += 1
            cid = meta["composerId"]
            if not meta.get("fullConversationHeadersOnly"):
                stats["empty"] += 1
                continue
            if not force and known.get(cid) == signature(src, meta):
                stats["skipped"] += 1
                continue
            try:
                s = parse_composer(src, meta, where.get(cid, (None, None)),
                                   parents.get(cid), version)
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
                # Cursor's format is internal; one unparseable session must not stop
                # the rest, and must not be marked ingested (so it is retried).
                stats["failed"] += 1
                print(f"cursor-logger: {cid[:8]} did not parse: {exc!r}", file=sys.stderr)
                continue
            if s is None:
                stats["empty"] += 1
                continue
            try:
                store.write_session(s)
                store.mark_ingested(cid, s.signature)
                store.commit()
            except Exception:
                store.rollback()
                raise
            stats["ingested"] += 1
            if verbose:
                print(f"  ingested {cid[:8]}  {s.num_tool_calls:>4} calls  "
                      f"{s.num_messages:>4} msgs  {(s.model or '-'):14.14} {s.name or ''}")
    finally:
        store.close()
        src.close()
    return stats


def watch(db: Optional[str] = None, state_db: str = DEFAULT_STATE_DB,
          interval: float = 30.0):
    print(f"watching {state_db} every {interval:.0f}s (Ctrl-C to stop)")
    while True:
        s = ingest_once(db=db, state_db=state_db, verbose=True)
        if s["ingested"]:
            print(f"[{time.strftime('%H:%M:%S')}] +{s['ingested']} sessions "
                  f"({s['skipped']} unchanged)")
        time.sleep(interval)
