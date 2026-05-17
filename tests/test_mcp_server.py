import contextlib
import io
import os
import sqlite3
import sys
import threading
import unittest
from types import ModuleType
from unittest.mock import patch


class _FastMCPStub:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self):
        def decorator(func):
            return func
        return decorator


mcp_module = ModuleType("mcp")
mcp_server_module = ModuleType("mcp.server")
fastmcp_module = ModuleType("mcp.server.fastmcp")
fastmcp_module.FastMCP = _FastMCPStub
sys.modules.setdefault("mcp", mcp_module)
sys.modules.setdefault("mcp.server", mcp_server_module)
sys.modules.setdefault("mcp.server.fastmcp", fastmcp_module)

from anamnestic.daemon import mcp_server


_audit_log: list[tuple] = []


def _fake_write_audit(action, status, duration_sec, details):
    _audit_log.append((action, status, dict(details)))


# Silence real audit writes during tests, capture calls for inspection.
import anamnestic.audit as _audit_mod  # noqa: E402

_audit_mod.write_audit = _fake_write_audit


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class MCPPServerTests(unittest.TestCase):
    def test_mem_search_returns_friendly_fts_error(self):
        conn = _FakeConn()
        with (
            patch.object(mcp_server, "connect", return_value=conn),
            patch.object(
                mcp_server,
                "_bm25",
                side_effect=sqlite3.OperationalError('fts5: syntax error near "OR"'),
            ),
        ):
            result = mcp_server.mem_search('a OR b', mode="bm25")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["error"], 'fts5: syntax error near "OR"')
        self.assertIn("plain-language query", result["hint"])
        self.assertTrue(conn.closed)

    def test_mem_search_returns_friendly_error_for_unknown_mode(self):
        conn = _FakeConn()
        with patch.object(mcp_server, "connect", return_value=conn):
            result = mcp_server.mem_search("test", mode="weird")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["error"], "unknown mode: weird")
        self.assertIn("hybrid", result["hint"])
        self.assertTrue(conn.closed)

    def test_mem_search_attaches_coverage_on_empty_hits(self):
        conn = _FakeConn()
        coverage = {"n_turns": 42, "date_range_indexed": ["2025-01-01", "2026-04-15"]}
        with (
            patch.object(mcp_server, "connect", return_value=conn),
            patch.object(mcp_server, "_bm25", return_value=[]),
            patch.object(mcp_server, "_safe_coverage", return_value=coverage),
        ):
            result = mcp_server.mem_search("nothing here", mode="bm25")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["searched"], coverage)
        self.assertNotIn("error", result)
        self.assertTrue(conn.closed)

    def test_mem_search_bm25_does_not_load_embedder(self):
        conn = _FakeConn()
        with (
            patch.object(mcp_server, "connect", return_value=conn),
            patch.object(mcp_server, "_bm25", return_value=[]),
            patch.object(mcp_server, "_init", side_effect=AssertionError("_init should not be called")),
        ):
            result = mcp_server.mem_search("test", mode="bm25")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hits"], [])
        self.assertNotIn("error", result)
        self.assertTrue(conn.closed)

    def test_mem_search_semantic_disabled_returns_error_without_loading_embedder(self):
        conn = _FakeConn()
        with (
            patch.object(mcp_server, "connect", return_value=conn),
            patch.object(mcp_server, "SEMANTIC_ENABLED", False),
            patch.object(mcp_server, "_EMB", None),
            patch.object(mcp_server, "_COL", None),
        ):
            result = mcp_server.mem_search("test", mode="semantic")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hits"], [])
        self.assertIn("ANAMNESTIC_SEMANTIC=0", result["error"])
        self.assertTrue(conn.closed)


class AuditTelemetryTests(unittest.TestCase):
    def setUp(self):
        _audit_log.clear()

    def test_successful_mem_search_records_ok_with_query_and_turn_ids(self):
        conn = _FakeConn()
        fake_hit = type(
            "H",
            (),
            {
                "turn_id": 123,
                "text": "x",
                "meta": {},
                "bm25_rank": 1,
                "sem_rank": None,
                "rrf_score": 0.0,
                "rerank_score": None,
                "temporal_rank": None,
                "graph_rank": None,
                "hit_type": "turn",
            },
        )()
        with (
            patch.object(mcp_server, "connect", return_value=conn),
            patch.object(mcp_server, "_bm25", return_value=[fake_hit]),
        ):
            mcp_server.mem_search("hello", mode="bm25")

        self.assertEqual(len(_audit_log), 1)
        action, status, details = _audit_log[0]
        self.assertEqual(action, "mcp.mem_search")
        self.assertEqual(status, "ok")
        self.assertEqual(details["query"], "hello")
        self.assertEqual(details["total"], 1)
        self.assertEqual(details["returned_turn_ids"], [123])

    def test_errored_mem_search_records_error_status(self):
        conn = _FakeConn()
        with patch.object(mcp_server, "connect", return_value=conn):
            mcp_server.mem_search("x", mode="nope")

        self.assertEqual(len(_audit_log), 1)
        action, status, _ = _audit_log[0]
        self.assertEqual(action, "mcp.mem_search")
        self.assertEqual(status, "error")


class AutoSyncTests(unittest.TestCase):
    def test_auto_sync_redirects_helper_stdout_to_stderr(self):
        import anamnestic.db as db
        import anamnestic.ingest.incremental as incremental

        def noisy_migrations():
            print("Applying 999_test.sql...")

        def noisy_ingest(verbose=False):
            print("ingest progress")
            return {"new": 1, "updated": 0}

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(db, "run_migrations", noisy_migrations),
            patch.object(incremental, "run", noisy_ingest),
            patch.dict(os.environ, {"ANAMNESTIC_MCP_AUTO_EMBED": "0"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            mcp_server._auto_sync()

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Applying 999_test.sql", stderr.getvalue())
        self.assertIn("ingest progress", stderr.getvalue())
        self.assertIn("[anamnestic] auto-sync", stderr.getvalue())

    def test_auto_sync_stdout_redirect_does_not_capture_other_threads(self):
        import anamnestic.db as db
        import anamnestic.ingest.incremental as incremental

        entered = threading.Event()
        release = threading.Event()

        def noisy_migrations():
            print("Applying 999_test.sql...")
            entered.set()
            self.assertTrue(release.wait(2), "auto-sync test release timed out")

        def quiet_ingest(verbose=False):
            return {"new": 0, "updated": 0}

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(db, "run_migrations", noisy_migrations),
            patch.object(incremental, "run", quiet_ingest),
            patch.dict(os.environ, {"ANAMNESTIC_MCP_AUTO_EMBED": "0"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            thread = threading.Thread(target=mcp_server._auto_sync)
            thread.start()
            self.assertTrue(entered.wait(2), "auto-sync test did not enter redirect")
            print('{"jsonrpc":"2.0","id":1}')
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertIn('{"jsonrpc":"2.0","id":1}', stdout.getvalue())
        self.assertNotIn("Applying 999_test.sql", stdout.getvalue())
        self.assertIn("Applying 999_test.sql", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
