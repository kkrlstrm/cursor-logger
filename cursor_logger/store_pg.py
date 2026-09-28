# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Postgres backend: the SQLite schema with a cursor_ prefix, so Cursor sessions
sit beside cc-logger's tables and codex-logger's codex_* tables in one warehouse.

Lazy-imported by store.open_store() only for a postgresql:// URL, so psycopg is
an optional dependency.

prepare_threshold=None: psycopg starts preparing a statement after ~5 executions
and Neon's -pooler endpoint cannot serve prepared statements, so a pooled DSN
passes a first test and then fails once an ingest repeats its INSERTs. Disabling
preparation removes that failure on either endpoint.
"""
from __future__ import annotations

from .store import FAILURE_VIEW, SCHEMA, BaseStore


class PostgresStore(BaseStore):
    prefix = "cursor_"
    ph = "%s"
    now = "now()"

    def __init__(self, url: str):
        import psycopg  # lazy
        from psycopg.rows import dict_row
        self._dict_row = dict_row
        self.conn = psycopg.connect(url, autocommit=False, prepare_threshold=None,
                                    connect_timeout=20)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA.format(P="cursor_", TS="TIMESTAMPTZ", BIG="BIGINT"))
            cur.execute(FAILURE_VIEW.format(
                P="cursor_", DROP="DROP VIEW IF EXISTS cursor_v_failures;"))
        self.conn.commit()

    def _exec(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def _many(self, sql, rows):
        # psycopg 3 pipelines executemany: one batch per table, not one round trip per row
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany(sql, rows)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def query(self, sql: str, params=(), raw: bool = False):
        if not raw:
            # the CLI writes its queries once, in SQLite dialect
            sql = (sql.replace("{P}", self.prefix).replace("?", "%s")
                   .replace("datetime('now', %s)", "now() + (%s)::interval"))
        with self.conn.cursor(row_factory=self._dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self):
        self.conn.close()
