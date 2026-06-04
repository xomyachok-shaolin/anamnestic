"""Incremental embedding: embed historical_turns rows missing from Chroma.

Uses anamnestic_embed_state to skip already-embedded turns. Safe to run on a timer
after ingest.incremental.

A corrupt HNSW segment makes chromadb segfault (SIGSEGV) inside native hnswlib on
query OR add. That used to kill the whole sync/MCP process on every run. The fix:
ALL chromadb work happens in isolated child processes, so a native crash is
observed by the orchestrator as a non-zero return code instead of taking it down.
  * run()      -> embed-worker child; on a crash returns chroma_index_corrupt.
  * reindex()  -> reindex-worker child builds a fresh collection in a staging dir
                  (chromadb flushes cleanly on the child's exit), the orchestrator
                  validates it reads back, then atomically renames it into place.
                  An interruption leaves staging incomplete and the live index
                  untouched — the fix for the corruption recidive caused by a
                  process killed mid-persist.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from anamnestic.config import (
    CHROMA_COLLECTION,
    CHROMA_DIR,
    EMBED_DIM,
    EMBED_MODEL,
    FASTEMBED_CACHE,
    SEMANTIC_ENABLED,
    SEMANTIC_REQUIRED,
    semantic_dependencies_available,
)
from anamnestic.db import connect

COLL = CHROMA_COLLECTION

_MODULE = "anamnestic.indexers.incremental_chroma"


def _embedder():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=FASTEMBED_CACHE)


def _chroma_col():
    from anamnestic.chroma_store import persistent_client

    client = persistent_client()
    try:
        return client.get_collection(COLL)
    except Exception:
        return client.create_collection(COLL, metadata={"hnsw:space": "cosine"})


def _index_corrupt_result(detail):
    return {
        "embedded": 0,
        "elapsed": 0,
        "error": "chroma_index_corrupt",
        "detail": detail,
        "hint": "HNSW index is unreadable; run `anamnestic reindex` to rebuild it",
    }


def _signal_name(returncode):
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"signal {-returncode}"


# --- isolated chromadb workers (run in their own process; see run()/reindex()) ---

def _embed_pending(batch_size=64, limit=None, verbose=False):
    """Embed turns missing from Chroma. Touches chromadb directly — meant to run
    inside an isolated child process so a native crash can't take down the caller."""
    conn = connect()
    cur = conn.cursor()

    q = """
        SELECT ht.id, ht.content_session_id, ht.turn_number, ht.role, ht.text,
               ht.timestamp, ht.platform_source,
               s.project, s.custom_title
        FROM historical_turns ht
        LEFT JOIN anamnestic_embed_state es ON es.turn_id = ht.id AND es.collection = ?
        LEFT JOIN sdk_sessions s ON s.content_session_id = ht.content_session_id
        WHERE es.turn_id IS NULL AND length(ht.text) > 15
        ORDER BY ht.id
    """
    if limit:
        q += f" LIMIT {int(limit)}"

    rows = cur.execute(q, (COLL,)).fetchall()
    if not rows:
        conn.close()
        return {"embedded": 0, "elapsed": 0}

    col = _chroma_col()
    emb = _embedder()
    list(emb.embed(["init"]))  # warmup

    t0 = time.time()
    buf_docs, buf_ids, buf_metas, buf_turn_ids = [], [], [], []
    embedded = 0

    def flush():
        nonlocal embedded, buf_docs, buf_ids, buf_metas, buf_turn_ids
        if not buf_docs:
            return
        existing_ids = set((col.get(ids=buf_ids, include=[]) or {}).get("ids", []))
        missing = [
            (doc, doc_id, meta)
            for doc, doc_id, meta in zip(buf_docs, buf_ids, buf_metas)
            if doc_id not in existing_ids
        ]
        if missing:
            missing_docs, missing_ids, missing_metas = zip(*missing)
            vectors = list(emb.embed(list(missing_docs)))
            col.add(
                ids=list(missing_ids),
                documents=list(missing_docs),
                metadatas=list(missing_metas),
                embeddings=[v.tolist() for v in vectors],
            )
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cur.executemany(
            "INSERT OR REPLACE INTO anamnestic_embed_state(turn_id, collection, embedded_at) "
            "VALUES (?, ?, ?)",
            [(tid, COLL, now) for tid in buf_turn_ids],
        )
        conn.commit()
        embedded += len(buf_docs)
        buf_docs, buf_ids, buf_metas, buf_turn_ids = [], [], [], []

    for r in rows:
        txt = r["text"][:2000].strip()
        if not txt:
            continue
        buf_docs.append(txt)
        buf_ids.append(f"ht-{r['id']}")
        buf_metas.append({
            "session": r["content_session_id"] or "",
            "project": r["project"] or "",
            "title": r["custom_title"] or "",
            "timestamp": r["timestamp"] or "",
            "turn": r["turn_number"] or 0,
            "role": r["role"] or "",
            "source": r["platform_source"] or "",
        })
        buf_turn_ids.append(r["id"])
        if len(buf_docs) >= batch_size:
            flush()
            if verbose and embedded % 1024 == 0:
                print(f"  embedded {embedded}/{len(rows)}", file=sys.stderr, flush=True)
    flush()
    conn.commit()
    conn.close()
    return {"embedded": embedded, "elapsed": round(time.time() - t0, 1)}


def _build_staging(staging_path, batch_size, verbose):
    """Build a fresh collection at staging_path from every turn. Meant to run in its
    own process so chromadb flushes HNSW to disk on clean exit."""
    import chromadb
    try:
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=staging_path, settings=Settings(anonymized_telemetry=False)
        )
    except TypeError:
        client = chromadb.PersistentClient(path=staging_path)
    col = client.create_collection(COLL, metadata={"hnsw:space": "cosine"})
    emb = _embedder()
    list(emb.embed(["init"]))  # warmup

    conn = connect()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT ht.id, ht.content_session_id, ht.turn_number, ht.role, ht.text,
               ht.timestamp, ht.platform_source, s.project, s.custom_title
        FROM historical_turns ht
        LEFT JOIN sdk_sessions s ON s.content_session_id = ht.content_session_id
        WHERE length(ht.text) > 15
        ORDER BY ht.id
        """
    ).fetchall()
    conn.close()

    buf_docs, buf_ids, buf_metas = [], [], []
    embedded = 0

    def _flush():
        nonlocal embedded, buf_docs, buf_ids, buf_metas
        if not buf_docs:
            return
        vectors = list(emb.embed(list(buf_docs)))
        col.add(
            ids=list(buf_ids),
            documents=list(buf_docs),
            metadatas=list(buf_metas),
            embeddings=[v.tolist() for v in vectors],
        )
        embedded += len(buf_docs)
        buf_docs, buf_ids, buf_metas = [], [], []

    for r in rows:
        txt = r["text"][:2000].strip()
        if not txt:
            continue
        buf_docs.append(txt)
        buf_ids.append(f"ht-{r['id']}")
        buf_metas.append({
            "session": r["content_session_id"] or "",
            "project": r["project"] or "",
            "title": r["custom_title"] or "",
            "timestamp": r["timestamp"] or "",
            "turn": r["turn_number"] or 0,
            "role": r["role"] or "",
            "source": r["platform_source"] or "",
        })
        if len(buf_docs) >= batch_size:
            _flush()
            if verbose and embedded % 2048 == 0:
                print(f"  reindex embedded {embedded}/{len(rows)}", file=sys.stderr, flush=True)
    _flush()
    _ = col.count()  # materialize before the process exits and persists
    return {"embedded": embedded, "total_rows": len(rows)}


# --- index health probe (subprocess-isolated; survives native SIGSEGV) -----------

def _open_collection_for_probe(path):
    """Open the live collection (path falsy) or a staging dir, for the probe child."""
    if path:
        import chromadb
        try:
            from chromadb.config import Settings
            client = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
        except TypeError:
            client = chromadb.PersistentClient(path=path)
        return client.get_collection(COLL)
    return _chroma_col()


def probe_index_health(path=None, timeout=120):
    """Return (healthy, detail). Reads the index (query forces HNSW load) in a
    separate process, so a native SIGSEGV/abort is reported as a return code
    instead of crashing the caller. path=None probes the live index."""
    argv = [sys.executable, "-m", _MODULE, "probe"]
    if path:
        argv.append(path)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {timeout}s (index hung)"
    if r.returncode == 0 and "probe-ok" in (r.stdout or ""):
        return True, "ok"
    if r.returncode < 0:
        return False, f"probe crashed with {_signal_name(r.returncode)} (native hnswlib crash — index corrupt)"
    return False, f"probe failed rc={r.returncode}: {(r.stderr or '').strip()[-300:]}"


# --- orchestrators (never touch chromadb in-process) -----------------------------

def run(batch_size=64, limit=None, verbose=False, skip_health_probe=False):
    """Incremental embed. Runs the embed loop in an isolated child process so a
    native HNSW segfault on a corrupt index cannot take down the caller (sync/MCP)
    — it is reported as a chroma_index_corrupt result asking for a reindex.
    (skip_health_probe is accepted for backwards compatibility; embedding is always
    isolated now, which subsumes the old in-process probe.)"""
    if not SEMANTIC_ENABLED:
        return {"embedded": 0, "elapsed": 0, "skipped": "semantic_disabled"}
    if not semantic_dependencies_available():
        if SEMANTIC_REQUIRED:
            return {"embedded": 0, "elapsed": 0, "error": "semantic dependencies are not installed"}
        return {"embedded": 0, "elapsed": 0, "skipped": "semantic_unavailable"}

    env = dict(
        os.environ,
        ANAMNESTIC_SEMANTIC="1",
        ANAMNESTIC_EMBED_BATCH=str(int(batch_size)),
        ANAMNESTIC_EMBED_LIMIT=str(int(limit) if limit else 0),
        ANAMNESTIC_EMBED_VERBOSE="1" if verbose else "0",
    )
    proc = subprocess.run(
        [sys.executable, "-m", _MODULE, "embed-worker"],
        env=env, capture_output=True, text=True,
    )
    if verbose and proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-400:]
        if proc.returncode < 0:
            return _index_corrupt_result(
                f"embed worker killed by {_signal_name(proc.returncode)} (native hnswlib crash). {detail}"
            )
        return {"embedded": 0, "elapsed": 0, "error": "embed_worker_failed", "detail": detail}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except Exception:
            continue
    return {"embedded": 0, "elapsed": 0, "skipped": "no_worker_output"}


def reindex(batch_size=256, verbose=False):
    """Full atomic rebuild of the Chroma HNSW index.

    Builds a fresh collection in a staging dir (child process, so chromadb flushes
    cleanly), validates it reads back without a native crash, then atomically
    renames it into place. An interruption leaves staging incomplete and the live
    index untouched — the fix for the corruption recidive.
    """
    if not SEMANTIC_ENABLED:
        return {"error": "semantic_disabled", "hint": "unset ANAMNESTIC_SEMANTIC=0 to reindex"}
    if not semantic_dependencies_available():
        return {"error": "semantic dependencies are not installed"}

    base = CHROMA_DIR.rstrip("/")
    staging = base + ".reindex-tmp"
    shutil.rmtree(staging, ignore_errors=True)

    t0 = time.time()
    env = dict(
        os.environ,
        ANAMNESTIC_SEMANTIC="1",  # the orchestrator may run with it disabled
        ANAMNESTIC_REINDEX_STAGING=staging,
        ANAMNESTIC_REINDEX_BATCH=str(int(batch_size)),
        ANAMNESTIC_REINDEX_VERBOSE="1" if verbose else "0",
    )
    proc = subprocess.run(
        [sys.executable, "-m", _MODULE, "reindex-worker"],
        env=env, capture_output=True, text=True,
    )
    if verbose and proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        detail = (proc.stderr or "").strip()[-400:]
        if proc.returncode < 0:
            detail = f"worker killed by {_signal_name(proc.returncode)}; {detail}"
        return {"error": f"reindex worker failed (rc={proc.returncode})", "detail": detail}

    built = {}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            built = json.loads(line)
            break
        except Exception:
            continue
    if "error" in built:
        shutil.rmtree(staging, ignore_errors=True)
        return built

    healthy, detail = probe_index_health(path=staging)
    if not healthy:
        shutil.rmtree(staging, ignore_errors=True)
        return {"error": "staging index unhealthy after build", "detail": detail}

    backup = base + ".old-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(base):
        os.rename(base, backup)
    os.rename(staging, base)

    # collection was rebuilt from scratch — rewrite embed_state to match it
    conn = connect()
    try:
        conn.execute("DELETE FROM anamnestic_embed_state WHERE collection = ?", (COLL,))
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO anamnestic_embed_state(turn_id, collection, embedded_at) "
            "SELECT id, ?, ? FROM historical_turns WHERE length(text) > 15",
            (COLL, now),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "reindexed": built.get("embedded", 0),
        "total_rows": built.get("total_rows", 0),
        "elapsed": round(time.time() - t0, 1),
        "backup": backup,
    }


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"

    if cmd == "embed-worker":
        batch = int(os.environ.get("ANAMNESTIC_EMBED_BATCH", "64"))
        limit = int(os.environ.get("ANAMNESTIC_EMBED_LIMIT", "0")) or None
        verbose = os.environ.get("ANAMNESTIC_EMBED_VERBOSE") == "1"
        try:
            res = _embed_pending(batch, limit, verbose)
        except Exception as exc:
            print(json.dumps({"embedded": 0, "elapsed": 0, "error": f"{type(exc).__name__}: {exc}"}))
            raise
        print(json.dumps(res))

    elif cmd == "reindex-worker":
        staging = os.environ["ANAMNESTIC_REINDEX_STAGING"]
        batch = int(os.environ.get("ANAMNESTIC_REINDEX_BATCH", "256"))
        verbose = os.environ.get("ANAMNESTIC_REINDEX_VERBOSE") == "1"
        try:
            res = _build_staging(staging, batch, verbose)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            raise
        print(json.dumps(res))

    elif cmd == "probe":
        # isolated read of the index; a corrupt HNSW segfaults this child only
        col = _open_collection_for_probe(argv[2] if len(argv) > 2 else None)
        col.count()
        col.query(query_embeddings=[[0.0] * EMBED_DIM], n_results=1)
        print("probe-ok")

    elif cmd == "reindex":
        print(json.dumps(reindex(verbose=True), ensure_ascii=False))

    else:
        stats = run(verbose=True)
        print(f"Chroma: embedded={stats.get('embedded')} in {stats.get('elapsed')}s")


if __name__ == "__main__":
    _main(sys.argv)
