"""Parser + store tests. Stdlib unittest, no network. Builds a synthetic Cursor
state.vscdb with the shapes observed on Cursor 3.21 (composerData _v 18)."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cursor_logger.ingest import ingest_once  # noqa: E402
from cursor_logger.parse import classify, connect_readonly, parse_composer  # noqa: E402
from cursor_logger.store import SQLiteStore  # noqa: E402

CID = "11111111-aaaa-bbbb-cccc-000000000001"
SUB = "22222222-aaaa-bbbb-cccc-000000000002"


def tool(name, params, result, status="completed", call="c", error=None):
    tf = {"toolCallId": call, "name": name, "status": status,
          "params": json.dumps(params), "result": json.dumps(result) if result is not None else None}
    if error:
        tf["error"] = json.dumps({"modelVisibleErrorMessage": error})
    return tf


BUBBLES = [
    {"bubbleId": "b0", "type": 1, "text": "run the tests", "requestId": "r1",
     "modelInfo": {"modelName": "grok-4.6"}, "createdAt": "2026-09-21T09:49:25.000Z",
     "contextWindowStatusAtCreation": {"tokensUsed": 9417, "tokenLimit": 125000},
     # a stub on a user bubble: no name, no call id -> not a tool call
     "toolFormerData": {"additionalData": "{'status': 'error'}"}},
    {"bubbleId": "b1", "type": 2, "thinking": {"text": "plan"}, "thinkingDurationMs": 900,
     "createdAt": "2026-09-21T09:49:26.000Z"},
    # shell, exit code omitted (proto default) -> success, basis exit_code_omitted
    {"bubbleId": "b2", "type": 2, "createdAt": "2026-09-21T09:49:27.000Z",
     "toolFormerData": tool("run_terminal_command_v2", {"command": "ls", "cwd": "/repo"},
                            {"output": "a\nb", "rejected": False}, call="t2")},
    # shell, exitCodeV2 nonzero -> failure with the output tail
    {"bubbleId": "b3", "type": 2, "createdAt": "2026-09-21T09:49:28.000Z",
     "toolFormerData": tool("run_terminal_cmd", {"command": "git status"},
                            {"output": "fatal: not a git repository", "exitCodeV2": 128,
                             "rejected": False}, call="t3")},
    # shell, explicit 0 -> success, basis exit_code
    {"bubbleId": "b4", "type": 2, "createdAt": "2026-09-21T09:49:29.000Z",
     "toolFormerData": tool("run_terminal_cmd", {"command": "echo hi"},
                            {"output": "hi", "exitCodeV2": 0}, call="t4")},
    # user rejected the command
    {"bubbleId": "b5", "type": 2, "createdAt": "2026-09-21T09:49:30.000Z",
     "toolFormerData": tool("run_terminal_cmd", {"command": "rm -rf x"},
                            {"rejected": True}, call="t5")},
    # tool error with the message in toolFormerData.error
    {"bubbleId": "b6", "type": 2, "createdAt": "2026-09-21T09:49:31.000Z",
     "toolFormerData": tool("glob_file_search", {"globPattern": "**/*", "targetDirectory": "/nope"},
                            {}, status="error", call="t6", error="Path does not exist: /nope")},
    # nested result error on a completed call
    {"bubbleId": "b7", "type": 2, "createdAt": "2026-09-21T09:49:32.000Z",
     "toolFormerData": tool("ripgrep_raw_search", {"pattern": "x", "path": "/r"},
                            {"error": {"error": "rg: no such file"}}, call="t7")},
    # cancelled; no createdAt -> inherits the previous bubble's timestamp
    {"bubbleId": "b8", "type": 2,
     "toolFormerData": tool("web_search", {"searchTerm": "q"}, None, status="cancelled", call="t8")},
    {"bubbleId": "b9", "type": 2, "text": "done. key sk-ant-api03-" + "A" * 40,
     "tokenCount": {"inputTokens": 100, "outputTokens": 20}, "turnDurationMs": 7000,
     "createdAt": "2026-09-21T09:49:40.000Z"},
]


def make_state_db(path, bubbles=BUBBLES, last_updated=1790000000000):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    meta = {"_v": 18, "composerId": CID, "name": "Test session", "unifiedMode": "agent",
            "isAgentic": True, "modelConfig": {"modelName": "default"}, "status": "completed",
            "createdAt": 1789999999000, "lastUpdatedAt": last_updated,
            "contextTokensUsed": 12000, "contextTokenLimit": 125000,
            "subagentComposerIds": [SUB],
            "fullConversationHeadersOnly": [{"bubbleId": b["bubbleId"], "type": b["type"]}
                                            for b in bubbles]}
    rows = [(f"composerData:{CID}", json.dumps(meta)),
            ("composerData:empty", json.dumps({"composerId": "empty",
                                                "fullConversationHeadersOnly": []})),
            ("composerData:null", None)]
    rows += [(f"bubbleId:{CID}:{b['bubbleId']}", json.dumps(b)) for b in bubbles]
    con.executemany("INSERT INTO cursorDiskKV VALUES(?,?)", rows)
    con.commit()
    con.close()
    return meta


class ClassifyTests(unittest.TestCase):
    def by_call(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "state.vscdb")
        meta = make_state_db(path)
        s = parse_composer(connect_readonly(path), meta)
        return s, {c.call_id: c for c in s.tool_calls}

    def test_outcomes(self):
        s, c = self.by_call()
        self.assertEqual(len(s.tool_calls), 7, "the user-bubble stub is not a tool call")
        self.assertEqual((c["t2"].status, c["t2"].exit_code, c["t2"].outcome_basis),
                         ("success", 0, "exit_code_omitted"))
        self.assertEqual((c["t3"].status, c["t3"].exit_code), ("failure", 128))
        self.assertIn("not a git repository", c["t3"].error_text)
        self.assertEqual((c["t4"].status, c["t4"].outcome_basis), ("success", "exit_code"))
        self.assertEqual(c["t5"].status, "rejected")
        self.assertEqual((c["t6"].status, c["t6"].error_text),
                         ("failure", "Path does not exist: /nope"))
        self.assertEqual((c["t7"].status, c["t7"].error_text), ("failure", "rg: no such file"))
        self.assertEqual(c["t8"].status, "aborted")

    def test_session_fields(self):
        s, c = self.by_call()
        self.assertEqual(s.model, "default")
        self.assertEqual(s.models_seen, "grok-4.6")
        self.assertEqual(c["t2"].model, "grok-4.6", "request model carries to later calls")
        self.assertEqual(c["t2"].cwd, "/repo")
        self.assertEqual(c["t8"].ts, "2026-09-21T09:49:32.000Z", "missing ts inherits")
        self.assertEqual(s.num_turns, 1)
        t = s.turns[0]
        self.assertEqual((t.context_tokens_used, t.turn_duration_ms, t.output_tokens,
                          t.num_tool_calls), (9417, 7000, 20, 7))
        kinds = [(m.role, m.kind) for m in s.messages]
        self.assertEqual(kinds, [("user", "text"), ("assistant", "thinking"), ("assistant", "text")])

    def test_unknown_is_not_success(self):
        self.assertEqual(classify({"name": "x", "status": None})[0], "unknown")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.vscdb")
        self.out = os.path.join(self.tmp, "out.db")
        self.env = {k: os.environ.pop(k) for k in ("CURSOR_LOGGER_DB",) if k in os.environ}

    def tearDown(self):
        os.environ.update(self.env)

    def test_incremental_and_redacted(self):
        make_state_db(self.state)
        s1 = ingest_once(db=self.out, state_db=self.state)
        self.assertEqual((s1["ingested"], s1["empty"], s1["failed"]), (1, 1, 0))
        s2 = ingest_once(db=self.out, state_db=self.state)
        self.assertEqual((s2["ingested"], s2["skipped"]), (0, 1))
        st = SQLiteStore(self.out)
        text = st.query("SELECT text FROM messages WHERE kind='text' AND role='assistant'")[0]["text"]
        self.assertNotIn("A" * 40, text)
        self.assertIn("REDACTED", text)
        self.assertEqual(st.query("SELECT parent_session_id FROM sessions")[0]["parent_session_id"], None)
        self.assertEqual(st.query("SELECT COUNT(*) AS n FROM v_failures")[0]["n"], 5)

    def test_checkpoint_rewind_removes_rows(self):
        """Restoring a checkpoint deletes later bubbles; the store must follow."""
        make_state_db(self.state)
        ingest_once(db=self.out, state_db=self.state)
        os.remove(self.state)
        make_state_db(self.state, bubbles=BUBBLES[:3], last_updated=1790000009999)
        s = ingest_once(db=self.out, state_db=self.state)
        self.assertEqual(s["ingested"], 1)
        st = SQLiteStore(self.out)
        self.assertEqual(st.query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"], 1)
        self.assertEqual(st.query("SELECT num_tool_calls FROM sessions")[0]["num_tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
