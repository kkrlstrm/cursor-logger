"""cursor-logger: telemetry for Cursor's agent, read from Cursor's own SQLite store.

The Cursor sibling of cc-logger (Claude Code, hooks) and codex-logger (Codex,
rollout files). Reads state.vscdb read-only; installs nothing into Cursor, and
backfills every session already on disk.
"""
from .parse import Session, ToolCall, Message, Turn, parse_composer, classify  # noqa: F401
from .store import open_store, SQLiteStore  # noqa: F401
from .ingest import ingest_once, watch  # noqa: F401

__version__ = "0.1.0"
