#!/usr/bin/env python3
"""
stdio MCP server exposing hybrid memory search to Claude Code.

Tools:
  - mem_search(query, top_k=10, role=any, mode=hybrid) → ranked fuzzy hits
  - mem_probe(term, top_sessions=3) → exact-token coverage oracle
  - mem_entity(value, entity_type=None, limit=20) → scoped entity lookup
  - mem_get_thread(session_id) → continuation chain for a session
  - mem_get_turn(turn_id, context=2) → full turn + N surrounding turns
  - mem_get_session(session_id, max_turns=50) → session overview
  - mem_stats() → corpus statistics
  - mem_audit_tail(limit=20, action=None) → recent telemetry (for introspection)

Every tool call is recorded in anamnestic_audit as `action='mcp.<tool>'` with
a JSON `details` payload. Correlating mem_search and subsequent mem_get_turn
calls by timestamp proximity gives a passive relevance signal (which hits
the agent actually read) without any explicit feedback loop.

Run standalone for smoke test:
  python -m anamnestic.daemon.mcp_server

Claude Code config (add to ~/.claude.json or via `claude mcp add`):
  {
    "mcpServers": {
      "anamnestic": {
        "command": "$HOME/.claude-mem/semantic-env/bin/python",
        "args": ["-m", "anamnestic.daemon.mcp_server"]
      }
    }
  }
"""
from __future__ import annotations

import contextlib
import functools
import os
import sqlite3
import sys
import threading
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from anamnestic.audit import audited, recent as recent_audit
from anamnestic.threading import get_thread
from anamnestic.config import SEMANTIC_ENABLED, local_embed_model_ready
from anamnestic.db import connect
from anamnestic.search.hybrid import (
    _embedder,
    _chroma_col,
    search as hybrid_search,
    format_hit,
    _bm25,
    _semantic,
)

# Preload heavy resources at module import: embedder (~220 MB model) + Chroma.
# Done once per stdio process; subsequent tool calls are <100ms.
_EMB = None
_COL = None


class _ThreadStdoutProxy:
    """Route stdout writes from one thread to a target, preserving others."""

    def __init__(self, owner_ident: int, owner_stream, other_stream):
        self._owner_ident = owner_ident
        self._owner_stream = owner_stream
        self._other_stream = other_stream

    def _stream(self):
        if threading.get_ident() == self._owner_ident:
            return self._owner_stream
        return self._other_stream

    def write(self, text):
        return self._stream().write(text)

    def flush(self):
        return self._stream().flush()

    def __getattr__(self, name):
        return getattr(self._other_stream, name)


@contextlib.contextmanager
def _redirect_current_thread_stdout(target):
    original = sys.stdout
    proxy = _ThreadStdoutProxy(threading.get_ident(), target, original)
    sys.stdout = proxy
    try:
        yield
    finally:
        if sys.stdout is proxy:
            sys.stdout = original


def _auto_sync():
    """Lightweight incremental sync: ingest new files.

    Runs at MCP startup so data is fresh without waiting for the cron timer.
    Skips heavier enrichment (entities, threads, importance, summaries, graph)
    which run via `anamnestic sync` on a schedule.

    Embedding is deliberately opt-in at MCP startup. Loading ONNX/Chroma before
    the stdio initialize handshake can crash the native runtime and make the
    whole MCP server disappear from the client. Scheduled `anamnestic sync`
    remains responsible for embedding by default.
    """
    try:
        # MCP stdio reserves stdout for JSON-RPC frames. Some lower-level CLI
        # helpers still use print(); route only this auto-sync thread to stderr.
        with _redirect_current_thread_stdout(sys.stderr):
            from anamnestic.db import run_migrations
            from anamnestic.ingest.incremental import run as ingest

            run_migrations()
            ing = ingest(verbose=False)
            emb = {"embedded": 0, "skipped": "mcp_startup_default"}
            if os.environ.get("ANAMNESTIC_MCP_AUTO_EMBED", "0") == "1":
                from anamnestic.indexers.incremental_chroma import run as embed

                emb = embed(verbose=False, batch_size=64)
        total_new = ing.get("new", 0) + ing.get("updated", 0)
        if total_new > 0 or emb.get("embedded", 0) > 0:
            print(f"[anamnestic] auto-sync: ingested {total_new} new turns, "
                  f"embedded {emb.get('embedded', 0)}", file=sys.stderr)
    except Exception as exc:
        print(f"[anamnestic] auto-sync failed (non-fatal): {exc}", file=sys.stderr)


def _start_auto_sync_background() -> None:
    """Kick off startup ingest without delaying the MCP initialize handshake."""
    if os.environ.get("ANAMNESTIC_MCP_AUTO_SYNC", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    threading.Thread(
        target=_auto_sync,
        name="anamnestic-mcp-auto-sync",
        daemon=True,
    ).start()


def _init():
    global _EMB, _COL
    if _EMB is None:
        if not SEMANTIC_ENABLED:
            raise RuntimeError(
                "Semantic search is disabled by ANAMNESTIC_SEMANTIC=0. "
                "Use mode='bm25' or enable semantic indexing."
            )
        try:
            import fastembed  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Semantic search requires fastembed and chromadb. "
                "Install with: pip install anamnestic[semantic]"
            )
        if not local_embed_model_ready():
            raise RuntimeError("embedding model cache is missing")
        print("[anamnestic] loading embedder + Chroma...", file=sys.stderr)
        _EMB = _embedder()
        _COL = _chroma_col()
        # warm up: first embed call compiles the ONNX graph
        list(_EMB.embed(["warmup"]))
        print("[anamnestic] ready", file=sys.stderr)


mcp = FastMCP("anamnestic")


def _audited_tool(
    action: str,
    summarize: Callable[[tuple, dict, Any], dict] | None = None,
):
    """Decorator: wrap an MCP tool so every call lands in anamnestic_audit.

    `summarize(args, kwargs, result)` returns a JSON-safe dict of interesting
    fields for the `details` payload. It is called inside the audited context
    so its exceptions are swallowed (telemetry must never break a tool call).

    Status is 'error' if the returned dict has an 'error' key, else 'ok'.
    Duration is recorded by the `audited` context manager.
    """

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with audited(f"mcp.{action}") as details:
                result = fn(*args, **kwargs)
                try:
                    if summarize is not None:
                        details.update(summarize(args, kwargs, result) or {})
                except Exception:
                    pass
                if isinstance(result, dict) and "error" in result:
                    details["_status"] = "error"
                return result

        return inner

    return wrap


def _summarize_mem_search(args, kwargs, result):
    return {
        "query": (kwargs.get("query") or (args[0] if args else ""))[:200],
        "mode": result.get("mode") if isinstance(result, dict) else None,
        "total": result.get("total") if isinstance(result, dict) else None,
        "returned_turn_ids": [h.get("turn_id") for h in (result.get("hits") or [])][:10],
    }


def _summarize_mem_probe(args, kwargs, result):
    return {
        "term": (kwargs.get("term") or (args[0] if args else ""))[:200],
        "total_matches": result.get("total_matches") if isinstance(result, dict) else None,
        "n_top_sessions": len(result.get("top_sessions") or []) if isinstance(result, dict) else 0,
    }


def _summarize_mem_get_turn(args, kwargs, result):
    turn_id = kwargs.get("turn_id") or (args[0] if args else None)
    return {
        "turn_id": turn_id,
        "session": result.get("session") if isinstance(result, dict) else None,
        "found": isinstance(result, dict) and "error" not in result,
    }


def _summarize_mem_get_session(args, kwargs, result):
    return {
        "session": kwargs.get("session_id") or (args[0] if args else None),
        "total_turns": result.get("total_turns") if isinstance(result, dict) else None,
        "found": isinstance(result, dict) and "error" not in result,
    }


def _summarize_mem_stats(args, kwargs, result):
    totals = result.get("totals") if isinstance(result, dict) else {}
    return {"sessions": totals.get("sessions"), "turns": totals.get("turns")}


def _corpus_coverage(conn) -> dict[str, Any]:
    """Return a compact snapshot of what the corpus actually contains.

    Attached to empty/erroring mem_search responses so callers cannot
    confuse "no data indexed" with "searched and found nothing".
    """
    totals = conn.execute(
        "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM historical_turns"
    ).fetchone()
    by_source = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT platform_source, COUNT(*) FROM historical_turns "
            "GROUP BY platform_source"
        ).fetchall()
    }
    return {
        "n_turns": totals[0],
        "date_range_indexed": [
            (totals[1] or "")[:10],
            (totals[2] or "")[:10],
        ],
        "turns_by_source": by_source,
        "hint": (
            "Empty result means this query didn't match — not that the corpus "
            "is empty. Use mem_probe(term) for cross-source token frequency, "
            "or retry with different phrasing."
        ),
    }


def _safe_coverage(conn) -> dict[str, Any] | None:
    if conn is None:
        return None
    try:
        return _corpus_coverage(conn)
    except Exception:
        return None


def _search_error_response(
    query: str,
    mode: str,
    error: str,
    hint: str,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "query": query,
        "mode": mode,
        "total": 0,
        "hits": [],
        "error": error,
        "hint": hint,
    }
    if coverage is not None:
        resp["searched"] = coverage
    return resp


def _fts_syntax_hint() -> str:
    return (
        "FTS query syntax was not accepted. Try a plain-language query, "
        "drop boolean operators, or run separate searches."
    )


@mcp.tool()
@_audited_tool("mem_search", summarize=_summarize_mem_search)
def mem_search(
    query: str,
    top_k: int = 10,
    role: str = "any",
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Search historical Claude Code + subagent + Codex sessions.

    Args:
        query: free-form natural language query (Russian/English).
        top_k: number of hits to return (1..50).
        role: 'user' | 'assistant' | 'any' — filter by turn role.
        mode: 'hybrid' (BM25+semantic RRF, default) | 'semantic' | 'bm25'.

    Returns:
        {hits: [{rank, rrf_score, text, snippet, session, turn, role, timestamp, source, title, project, turn_id}]}
    """
    top_k = max(1, min(top_k, 50))
    rl = role if role in ("user", "assistant") else None
    conn = None
    coverage: dict[str, Any] | None = None
    try:
        conn = connect()

        if mode == "hybrid":
            hits = hybrid_search(conn, query, top_k=top_k, pool=50, role=rl)
        elif mode == "semantic":
            _init()
            hits = _semantic(_EMB, _COL, query, top_k, role=rl)
        elif mode == "bm25":
            hits = _bm25(conn, query, top_k)
        else:
            return _search_error_response(
                query=query,
                mode=mode,
                error=f"unknown mode: {mode}",
                hint="Use one of: hybrid, semantic, bm25.",
                coverage=_safe_coverage(conn),
            )
    except sqlite3.Error as exc:
        msg = str(exc)
        hint = _fts_syntax_hint() if "fts5" in msg.lower() and "syntax error" in msg.lower() else (
            "Search backend returned a SQLite error. Try a simpler query or retry later."
        )
        return _search_error_response(
            query=query,
            mode=mode,
            error=msg,
            hint=hint,
            coverage=_safe_coverage(conn),
        )
    except Exception as exc:
        return _search_error_response(
            query=query,
            mode=mode,
            error=f"{type(exc).__name__}: {exc}",
            hint="Search failed inside the MCP server. Retry with a simpler query or restart the server.",
            coverage=_safe_coverage(conn),
        )
    else:
        if not hits:
            coverage = _safe_coverage(conn)
    finally:
        if conn is not None:
            conn.close()

    out = []
    for rank, h in enumerate(hits, 1):
        out.append({
            "rank": rank,
            "rrf_score": round(h.rrf_score, 4) if h.rrf_score else None,
            "bm25_rank": h.bm25_rank,
            "sem_rank": h.sem_rank,
            "rerank_score": round(h.rerank_score, 4) if h.rerank_score is not None else None,
            "temporal_rank": h.temporal_rank,
            "graph_rank": getattr(h, "graph_rank", None),
            "turn_id": h.turn_id,
            "session": h.meta.get("session", ""),
            "turn": h.meta.get("turn"),
            "role": h.meta.get("role", ""),
            "timestamp": (h.meta.get("timestamp") or "")[:19],
            "source": h.meta.get("source", ""),
            "title": h.meta.get("title", ""),
            "project": h.meta.get("project", ""),
            "snippet": (h.text or "")[:400],
            "hit_type": getattr(h, "hit_type", "turn"),
        })
    resp: dict[str, Any] = {"query": query, "mode": mode, "total": len(out), "hits": out}
    if coverage is not None:
        resp["searched"] = coverage
    # Attach per-channel diagnostics when available (hybrid mode)
    if hasattr(hits, "diagnostics") and hits.diagnostics is not None:
        resp["diagnostics"] = hits.diagnostics.to_dict()
    return resp


@mcp.tool()
@_audited_tool("mem_get_turn", summarize=_summarize_mem_get_turn)
def mem_get_turn(turn_id: int, context: int = 2) -> dict[str, Any]:
    """Fetch a specific turn with N surrounding turns from the same session.

    Args:
        turn_id: id of the target turn (from mem_search results).
        context: number of turns before/after to include (0..10).
    """
    context = max(0, min(context, 10))
    conn = connect()

    target = conn.execute(
        "SELECT * FROM historical_turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if not target:
        conn.close()
        return {"error": f"turn_id {turn_id} not found"}
    sid = target["content_session_id"]
    tn = target["turn_number"]

    rows = conn.execute(
        """
        SELECT id, turn_number, role, text, timestamp
        FROM historical_turns
        WHERE content_session_id = ?
          AND turn_number BETWEEN ? AND ?
        ORDER BY turn_number
        """,
        (sid, tn - context, tn + context),
    ).fetchall()

    sess = conn.execute(
        "SELECT custom_title, project, platform_source FROM sdk_sessions "
        "WHERE content_session_id = ?",
        (sid,),
    ).fetchone()

    conn.close()
    return {
        "session": sid,
        "title": sess["custom_title"] if sess else "",
        "project": sess["project"] if sess else "",
        "source": sess["platform_source"] if sess else "",
        "target_turn": tn,
        "turns": [
            {
                "id": r["id"],
                "turn": r["turn_number"],
                "role": r["role"],
                "timestamp": r["timestamp"][:19] if r["timestamp"] else "",
                "text": r["text"],
                "is_target": r["turn_number"] == tn,
            }
            for r in rows
        ],
    }


@mcp.tool()
@_audited_tool("mem_get_session", summarize=_summarize_mem_get_session)
def mem_get_session(session_id: str, max_turns: int = 50) -> dict[str, Any]:
    """Get overview of a session: metadata + first N turns."""
    conn = connect()
    sess = conn.execute(
        """
        SELECT content_session_id, memory_session_id, project, platform_source,
               user_prompt, started_at, completed_at, prompt_counter, custom_title
        FROM sdk_sessions WHERE content_session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if not sess:
        conn.close()
        return {"error": f"session {session_id} not found"}

    turns = conn.execute(
        """
        SELECT id, turn_number, role, timestamp, substr(text, 1, 500) AS snippet
        FROM historical_turns
        WHERE content_session_id = ?
        ORDER BY turn_number
        LIMIT ?
        """,
        (session_id, max_turns),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM historical_turns WHERE content_session_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.close()
    return {
        "session": session_id,
        "title": sess["custom_title"] or "",
        "project": sess["project"],
        "source": sess["platform_source"],
        "started_at": sess["started_at"],
        "completed_at": sess["completed_at"],
        "prompt_count": sess["prompt_counter"],
        "total_turns": total,
        "turns_shown": len(turns),
        "turns": [
            {
                "id": r["id"],
                "turn": r["turn_number"],
                "role": r["role"],
                "timestamp": r["timestamp"][:19] if r["timestamp"] else "",
                "snippet": r["snippet"],
            }
            for r in turns
        ],
    }


def _fts_phrase(term: str) -> str:
    # FTS5: double-quote term as a phrase; escape embedded quotes by doubling.
    return '"' + term.replace('"', '""') + '"'


@mcp.tool()
@_audited_tool("mem_probe", summarize=_summarize_mem_probe)
def mem_probe(term: str, top_sessions: int = 3) -> dict[str, Any]:
    """Coverage oracle: does this exact token appear in the corpus, where, when.

    Contract: probe answers "is there **this** in the corpus", not "is there
    anything similar". No transliteration, no fuzzy matching, no stemming —
    just FTS on the literal term. For fuzzy/semantic variants call mem_search.

    Use before concluding "no records" from an empty mem_search result: if
    probe also returns zero, the term genuinely is not in historical_turns
    (under this spelling).

    Args:
        term: single word or short phrase to probe.
        top_sessions: number of highest-density sessions to return (0..10).

    Returns:
        {
          term, total_matches,
          date_range: [min, max],
          by_source: {source: count},
          by_month: [{month, count}],
          top_sessions: [{session, project, title, matches, last_seen}]
        }
    """
    term = (term or "").strip()
    if not term:
        return {"error": "empty term"}
    fts_expr = _fts_phrase(term)
    top_sessions = max(0, min(top_sessions, 10))

    conn = connect()
    try:
        try:
            totals = conn.execute(
                """
                SELECT COUNT(*), MIN(ht.timestamp), MAX(ht.timestamp)
                FROM historical_turns ht
                WHERE ht.id IN (
                    SELECT rowid FROM historical_turns_fts
                    WHERE historical_turns_fts MATCH ?
                )
                """,
                (fts_expr,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            return {
                "term": term,
                "error": str(exc),
                "hint": "FTS5 rejected the query. Try a simpler term.",
            }
        total = totals[0] or 0
        if total == 0:
            return {
                "term": term,
                "total_matches": 0,
                "date_range": ["", ""],
                "by_source": {},
                "by_month": [],
                "top_sessions": [],
                "hint": (
                    "Zero matches for the exact term. For fuzzy/semantic "
                    "variants (different spellings, morphology, translations) "
                    "call mem_search instead — it uses BM25 + multilingual "
                    "embeddings."
                ),
            }

        by_source = {
            r[0]: r[1]
            for r in conn.execute(
                """
                SELECT ht.platform_source, COUNT(*)
                FROM historical_turns ht
                WHERE ht.id IN (
                    SELECT rowid FROM historical_turns_fts
                    WHERE historical_turns_fts MATCH ?
                )
                GROUP BY ht.platform_source
                """,
                (fts_expr,),
            ).fetchall()
        }
        by_month = [
            {"month": r[0], "count": r[1]}
            for r in conn.execute(
                """
                SELECT substr(ht.timestamp, 1, 7) AS ym, COUNT(*)
                FROM historical_turns ht
                WHERE ht.id IN (
                    SELECT rowid FROM historical_turns_fts
                    WHERE historical_turns_fts MATCH ?
                )
                GROUP BY ym
                ORDER BY ym
                """,
                (fts_expr,),
            ).fetchall()
            if r[0]
        ]
        top = []
        if top_sessions > 0:
            top = [
                {
                    "session": r[0],
                    "project": r[1] or "",
                    "title": r[2] or "",
                    "matches": r[3],
                    "last_seen": (r[4] or "")[:19],
                }
                for r in conn.execute(
                    """
                    SELECT ht.content_session_id, s.project, s.custom_title,
                           COUNT(*) AS n, MAX(ht.timestamp)
                    FROM historical_turns ht
                    LEFT JOIN sdk_sessions s
                           ON s.content_session_id = ht.content_session_id
                    WHERE ht.id IN (
                        SELECT rowid FROM historical_turns_fts
                        WHERE historical_turns_fts MATCH ?
                    )
                    GROUP BY ht.content_session_id
                    ORDER BY n DESC, MAX(ht.timestamp) DESC
                    LIMIT ?
                    """,
                    (fts_expr, top_sessions),
                ).fetchall()
            ]
        return {
            "term": term,
            "total_matches": total,
            "date_range": [(totals[1] or "")[:10], (totals[2] or "")[:10]],
            "by_source": by_source,
            "by_month": by_month,
            "top_sessions": top,
        }
    finally:
        conn.close()


@mcp.tool()
@_audited_tool("mem_stats", summarize=_summarize_mem_stats)
def mem_stats() -> dict[str, Any]:
    """Corpus statistics: totals and per-source/per-project breakdowns."""
    from anamnestic.capabilities import semantic_snapshot

    conn = connect()
    totals = {
        "sessions": conn.execute("SELECT COUNT(*) FROM sdk_sessions").fetchone()[0],
        "user_prompts": conn.execute("SELECT COUNT(*) FROM user_prompts").fetchone()[0],
        "turns": conn.execute("SELECT COUNT(*) FROM historical_turns").fetchone()[0],
    }
    by_source = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT platform_source, COUNT(*) FROM historical_turns GROUP BY platform_source"
        ).fetchall()
    }
    by_role = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT role, COUNT(*) FROM historical_turns GROUP BY role"
        ).fetchall()
    }
    top_projects = [
        {"project": r[0], "sessions": r[1]}
        for r in conn.execute(
            """
            SELECT project, COUNT(*) n FROM sdk_sessions
            GROUP BY project ORDER BY n DESC LIMIT 10
            """
        ).fetchall()
    ]
    semantic = semantic_snapshot(conn)
    conn.close()
    return {
        "totals": totals,
        "turns_by_source": by_source,
        "turns_by_role": by_role,
        "top_projects": top_projects,
        "capabilities": {
            "semantic": semantic,
        },
    }


@mcp.tool()
@_audited_tool("mem_get_thread", summarize=lambda a, kw, r: {
    "session": kw.get("session_id") or (a[0] if a else None),
    "thread_length": len(r.get("sessions") or []) if isinstance(r, dict) else 0,
})
def mem_get_thread(session_id: str) -> dict[str, Any]:
    """Get the continuation thread for a session.

    A thread is a chain of sessions in the same project with temporal
    proximity (< 7 days gap). Returns all sessions in the thread, ordered
    chronologically, with the target session marked.

    Subagent sessions are excluded — they are already structurally linked
    to their parent via the ':' prefix in content_session_id.

    Args:
        session_id: content_session_id of any session in the thread.

    Returns:
        {session_id, thread_length, sessions: [{session, order, project,
         source, prompt, started_at, title, prompt_count, is_target}]}
    """
    sessions = get_thread(session_id)
    if not sessions:
        return {
            "session_id": session_id,
            "thread_length": 0,
            "sessions": [],
            "hint": "Session not found in threading index. Run `anamnestic threads` to recompute.",
        }
    return {
        "session_id": session_id,
        "thread_length": len(sessions),
        "sessions": sessions,
    }


@mcp.tool()
def mem_audit_tail(limit: int = 20, action: str | None = None) -> dict[str, Any]:
    """Return the last N audit records — telemetry of MCP tool calls and
    background jobs (sync, verify, backup).

    Args:
        limit: number of records (1..200).
        action: optional filter, e.g. 'mcp.mem_search' or 'sync'. None = all.

    Use this to:
      - introspect recent search activity (what was queried, how many hits),
      - correlate mem_search calls with subsequent mem_get_turn fetches
        (passive relevance signal: which hits the agent actually read),
      - verify sync/verify/backup ran successfully.
    """
    limit = max(1, min(limit, 200))
    rows = recent_audit(limit=limit if action is None else limit * 3)
    if action:
        rows = [r for r in rows if r["action"] == action][:limit]
    return {"count": len(rows), "records": rows}


@mcp.tool()
@_audited_tool("mem_entity", summarize=lambda a, kw, r: {
    "value": (kw.get("value") or (a[0] if a else ""))[:200],
    "total": r.get("total") if isinstance(r, dict) else None,
})
def mem_entity(
    value: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find turns that reference a specific entity (file path, URL, etc.).

    Searches the anamnestic_entities sidecar table — deterministic regex
    extraction, not fuzzy. Good for scoped queries like "what did we do
    with config.py?" that BM25/semantic handle poorly.

    Args:
        value: entity value or substring to search (e.g. "config.py",
            "/home/user/project", "github.com/repo"). Uses SQL LIKE
            matching so partial paths work.
        entity_type: optional filter — 'path', 'url', or None for all.
        limit: max results (1..100).

    Returns:
        {total, entities: [{entity_type, value, turn_id, role, timestamp,
         session, project, title, source, snippet}]}
    """
    value = (value or "").strip()
    if not value:
        return {"error": "empty value"}
    limit = max(1, min(limit, 100))
    like_pat = f"%{value}%"
    conn = connect()
    try:
        if entity_type:
            rows = conn.execute(
                """
                SELECT e.entity_type, e.value, e.turn_id,
                       ht.role, ht.timestamp, ht.content_session_id,
                       ht.platform_source, substr(ht.text, 1, 300) AS snippet,
                       s.custom_title, s.project
                FROM anamnestic_entities e
                JOIN historical_turns ht ON ht.id = e.turn_id
                LEFT JOIN sdk_sessions s
                       ON s.content_session_id = ht.content_session_id
                WHERE e.value LIKE ? AND e.entity_type = ?
                ORDER BY ht.timestamp DESC
                LIMIT ?
                """,
                (like_pat, entity_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.entity_type, e.value, e.turn_id,
                       ht.role, ht.timestamp, ht.content_session_id,
                       ht.platform_source, substr(ht.text, 1, 300) AS snippet,
                       s.custom_title, s.project
                FROM anamnestic_entities e
                JOIN historical_turns ht ON ht.id = e.turn_id
                LEFT JOIN sdk_sessions s
                       ON s.content_session_id = ht.content_session_id
                WHERE e.value LIKE ?
                ORDER BY ht.timestamp DESC
                LIMIT ?
                """,
                (like_pat, limit),
            ).fetchall()
    finally:
        conn.close()

    out = [
        {
            "entity_type": r["entity_type"],
            "value": r["value"],
            "turn_id": r["turn_id"],
            "role": r["role"],
            "timestamp": (r["timestamp"] or "")[:19],
            "session": r["content_session_id"],
            "project": r["project"] or "",
            "title": r["custom_title"] or "",
            "source": r["platform_source"],
            "snippet": r["snippet"],
        }
        for r in rows
    ]
    return {"query": value, "entity_type": entity_type, "total": len(out), "entities": out}


if __name__ == "__main__":
    _start_auto_sync_background()
    mcp.run()
