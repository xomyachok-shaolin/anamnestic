"""Tests for the crash-isolated incremental embedder / atomic reindex.

The defining property: a corrupt HNSW segment segfaults native hnswlib, and that
must never take down the orchestrator (sync/MCP). All chromadb work runs in child
processes; these tests pin the orchestrator's handling of worker outcomes.
"""
import json
from unittest import mock

from anamnestic.indexers import incremental_chroma as ic


def _proc(returncode=0, stdout="", stderr=""):
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_run_reports_corrupt_on_worker_segfault(monkeypatch):
    """A SIGSEGV in the embed worker becomes a chroma_index_corrupt result, not a crash."""
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(ic, "semantic_dependencies_available", lambda: True)
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **k: _proc(returncode=-11, stderr="boom"))
    res = ic.run()
    assert res["error"] == "chroma_index_corrupt"
    assert "SIGSEGV" in res["detail"]
    assert "reindex" in res["hint"]


def test_run_returns_worker_json_on_success(monkeypatch):
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(ic, "semantic_dependencies_available", lambda: True)
    out = "noise line\n" + json.dumps({"embedded": 7, "elapsed": 1.2})
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **k: _proc(returncode=0, stdout=out))
    assert ic.run() == {"embedded": 7, "elapsed": 1.2}


def test_run_skips_when_semantic_disabled(monkeypatch):
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", False)
    assert ic.run()["skipped"] == "semantic_disabled"


def test_run_nonzero_worker_is_error_not_crash(monkeypatch):
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(ic, "semantic_dependencies_available", lambda: True)
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **k: _proc(returncode=1, stderr="trace"))
    res = ic.run()
    assert res["error"] == "embed_worker_failed"
    assert res["embedded"] == 0


def test_reindex_aborts_and_cleans_staging_on_worker_crash(monkeypatch, tmp_path):
    live = tmp_path / "chroma"
    live.mkdir()
    staging = tmp_path / "chroma.reindex-tmp"
    staging.mkdir()  # simulate a leftover; reindex must remove it on failure
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(ic, "semantic_dependencies_available", lambda: True)
    monkeypatch.setattr(ic, "CHROMA_DIR", str(live))
    monkeypatch.setattr(ic.subprocess, "run", lambda *a, **k: _proc(returncode=-11, stderr="crash"))
    res = ic.reindex()
    assert "error" in res
    assert not staging.exists()        # staging cleaned up
    assert live.exists()               # live index left untouched


def test_reindex_does_not_swap_if_staging_unhealthy(monkeypatch, tmp_path):
    live = tmp_path / "chroma"
    live.mkdir()
    (live / "marker").write_text("original")
    monkeypatch.setattr(ic, "SEMANTIC_ENABLED", True)
    monkeypatch.setattr(ic, "semantic_dependencies_available", lambda: True)
    monkeypatch.setattr(ic, "CHROMA_DIR", str(live))
    # worker "succeeds" but the post-build validation fails -> must not swap
    monkeypatch.setattr(ic.subprocess, "run",
                        lambda *a, **k: _proc(returncode=0, stdout=json.dumps({"embedded": 5, "total_rows": 5})))
    monkeypatch.setattr(ic, "probe_index_health", lambda path=None, timeout=120: (False, "still broken"))
    res = ic.reindex()
    assert "error" in res
    assert (live / "marker").read_text() == "original"   # live untouched
    assert not (tmp_path / "chroma.reindex-tmp").exists()


def test_probe_detects_unreadable_dir(tmp_path):
    """Integration: a garbage Chroma dir must be reported unhealthy, not crash us."""
    if not ic.semantic_dependencies_available():
        import pytest
        pytest.skip("semantic dependencies (chromadb) not installed")
    bad = tmp_path / "bad-chroma"
    bad.mkdir()
    (bad / "chroma.sqlite3").write_bytes(b"garbage, not a sqlite database")
    healthy, detail = ic.probe_index_health(path=str(bad))
    assert healthy is False
    assert detail  # non-empty diagnosis
