"""Unified CLI: anamnestic <subcommand>"""
import argparse
import json
import sys

from anamnestic.audit import audited, write_health, recent
from anamnestic.db import connect

DEFAULT_SYNC_EMBED_LIMIT = 512


def _wal_checkpoint():
    conn = connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def _compute_status() -> dict:
    from anamnestic.capabilities import semantic_snapshot

    conn = connect()
    cur = conn.cursor()
    totals = {
        "sessions": cur.execute("SELECT COUNT(*) FROM sdk_sessions").fetchone()[0],
        "turns": cur.execute("SELECT COUNT(*) FROM historical_turns").fetchone()[0],
        "user_prompts": cur.execute("SELECT COUNT(*) FROM user_prompts").fetchone()[0],
    }
    embedded = cur.execute(
        "SELECT COUNT(*) FROM anamnestic_embed_state WHERE collection='history_turns'"
    ).fetchone()[0]
    unembedded = cur.execute(
        """
        SELECT COUNT(*) FROM historical_turns ht
        LEFT JOIN anamnestic_embed_state es ON es.turn_id = ht.id AND es.collection='history_turns'
        WHERE es.turn_id IS NULL
        """
    ).fetchone()[0]
    last_ingest = cur.execute("SELECT MAX(ingested_at) FROM anamnestic_ingest_state").fetchone()[0]
    files_tracked = cur.execute("SELECT COUNT(*) FROM anamnestic_ingest_state").fetchone()[0]
    recent_audit = recent(5)
    semantic = semantic_snapshot(conn)
    conn.close()

    drift = totals["turns"] - embedded
    return {
        "totals": totals,
        "embedded": embedded,
        "unembedded": unembedded,
        "drift_turns_vs_chroma": drift,
        "files_tracked": files_tracked,
        "last_ingest": last_ingest,
        "healthy": drift == unembedded and drift >= 0,
        "capabilities": {
            "semantic": semantic,
        },
        "recent_audit": recent_audit,
    }


def cmd_sync(args):
    from anamnestic.db import run_migrations
    from anamnestic.ingest.incremental import run as ingest
    from anamnestic.indexers.incremental_chroma import run as embed

    applied = run_migrations()
    if applied:
        print(f"migrations: {', '.join(applied)}")

    from anamnestic.entities import backfill as entity_backfill
    from anamnestic.threading import compute as thread_compute
    from anamnestic.importance import backfill as importance_backfill
    from anamnestic.summarize import backfill as summarize_backfill
    from anamnestic.graph import build_edges as graph_build_edges

    with audited("sync") as details:
        ing = ingest(verbose=args.verbose)
        embed_limit = None if args.embed_limit == 0 else args.embed_limit
        emb = embed(verbose=args.verbose, batch_size=args.batch, limit=embed_limit)
        ent = entity_backfill()
        thr = thread_compute()
        imp = importance_backfill()
        smr = summarize_backfill()
        grp = graph_build_edges()
        _wal_checkpoint()
        details.update({
            "ingest": ing,
            "embed": emb,
            "entities": ent,
            "threads": thr,
            "importance": imp,
            "summaries": smr,
            "graph": grp,
            "_status": "ok" if ing["errors"] == 0 and "error" not in emb else "warn",
        })

    sync_result = {
        "ingest": ing, "embed": emb, "entities": ent, "threads": thr,
        "importance": imp, "summaries": smr, "graph": grp,
    }
    snapshot = _compute_status()
    snapshot["last_sync"] = sync_result
    write_health(snapshot)
    print(json.dumps(sync_result, ensure_ascii=False))


def cmd_reindex(args):
    from anamnestic.indexers.incremental_chroma import reindex
    with audited("reindex") as details:
        res = reindex(batch_size=args.batch, verbose=args.verbose)
        details.update(res)
        if "error" in res:
            details["_status"] = "error"
    print(json.dumps(res, ensure_ascii=False))
    return 1 if "error" in res else 0


def cmd_status(args):
    snapshot = _compute_status()
    print(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str))


def cmd_search(args):
    from anamnestic.search.hybrid import search, format_hit
    conn = connect()
    rl = args.role if args.role in ("user", "assistant") else None
    hits = search(conn, args.query, top_k=args.top_k, pool=args.pool, role=rl)
    for h in hits:
        print(format_hit(h))
        print()
    if getattr(hits, "diagnostics", None) is not None:
        diag = hits.diagnostics.to_dict()
        sem = diag.get("semantic") or {}
        print(
            f"# channels={','.join(diag.get('channels_used') or ['none'])} "
            f"semantic={sem.get('status', 'unknown')}"
            f"{' pending=' + str(sem['pending']) if sem.get('pending') is not None else ''}",
            file=sys.stderr,
        )


def cmd_backup(args):
    from anamnestic.backup import run
    with audited("backup") as details:
        info = run()
        details.update(info)
    print(json.dumps(info, indent=2))


def cmd_restore(args):
    from anamnestic.restore import run
    from anamnestic.db import run_migrations
    with audited("restore") as details:
        info = run(args.tarball, force=args.force)
        details.update(info)
    run_migrations()
    print(json.dumps(info, indent=2, ensure_ascii=False))


def cmd_verify(args):
    from anamnestic.verify import run
    with audited("verify") as details:
        report = run()
        details.update({
            "healthy": report["healthy"],
            "issues_count": len(report["issues"]),
            "_status": "ok" if report["healthy"] else "warn",
        })
    # Make the latest verification visible without invoking Python:
    # health.json is the canonical snapshot for external monitors (conky, bar, cron alerts).
    snapshot = _compute_status()
    snapshot["last_verify"] = report
    write_health(snapshot)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["healthy"] else 1


def cmd_audit(args):
    rows = recent(args.limit)
    for r in rows:
        d = r["details"] or {}
        compact = {k: v for k, v in d.items() if k not in ("_status",)}
        print(f"{r['at']} [{r['status']}] {r['action']} "
              f"({r['duration_sec']}s) {json.dumps(compact, ensure_ascii=False)[:150]}")


def cmd_cross_sync(args):
    from anamnestic.sync.cross import run as run_cross
    out = run_cross(peers=args.peer or None, verbose=args.verbose)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    bad = [r for r in out.get("results", []) if not r.get("ok") and not r.get("skipped")]
    return 0 if not bad else 2


def cmd_errors(args):
    conn = connect()
    rows = conn.execute(
        """
        SELECT id, at, source, path, error_class, error_message, resolved_at
        FROM anamnestic_ingest_errors
        WHERE (? = 1) OR resolved_at IS NULL
        ORDER BY at DESC LIMIT ?
        """,
        (1 if args.all else 0, args.limit),
    ).fetchall()
    conn.close()
    if not rows:
        print("no ingest errors recorded")
        return 0
    for r in rows:
        marker = "✓ resolved" if r["resolved_at"] else "✗ open"
        print(f"[{r['at']}] {marker} {r['source']} {r['error_class']}")
        print(f"  {r['path']}")
        print(f"  {r['error_message'][:200]}")
        print()
    return 0


def cmd_entities(args):
    from anamnestic.entities import backfill, stats
    with audited("entities") as details:
        r = backfill()
        details.update(r)
    s = stats()
    print(json.dumps({"backfill": r, "stats": s}, indent=2, ensure_ascii=False))


def cmd_threads(args):
    from anamnestic.threading import compute, stats
    with audited("threads") as details:
        r = compute()
        details.update(r)
    s = stats()
    print(json.dumps({"compute": r, "stats": s}, indent=2, ensure_ascii=False))


def cmd_archive(args):
    from anamnestic.db import connect, run_migrations
    from anamnestic.decay import archive_old_turns
    from anamnestic.config import ARCHIVE_AGE_DAYS

    run_migrations()
    conn = connect()
    result = archive_old_turns(
        conn, age_days=args.age or ARCHIVE_AGE_DAYS,
        importance_threshold=args.threshold,
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_eval(args):
    from anamnestic.eval.run import evaluate, load_golden
    from pathlib import Path
    golden = args.golden or str(Path(__file__).parent / "eval" / "golden.yaml")
    queries = load_golden(golden)
    r = evaluate(queries, top_k=args.top_k, mode=args.mode)
    print(f"{args.mode}: {r['total_passed']}/{r['total_queries']} "
          f"pass ({r['pass_rate']:.0%}), p@{args.top_k}={r['avg_precision_at_k']:.3f}")
    return 0 if r["total_passed"] == r["total_queries"] else 1


def build_parser():
    ap = argparse.ArgumentParser(prog="anamnestic")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="incremental ingest + embed + WAL checkpoint")
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--batch", type=int, default=64)
    s.add_argument(
        "--embed-limit",
        type=int,
        default=DEFAULT_SYNC_EMBED_LIMIT,
        help="max turns to embed per sync run; 0 means unlimited",
    )
    s.set_defaults(func=cmd_sync)

    rx = sub.add_parser("reindex", help="atomic full rebuild of the Chroma HNSW index (staging + swap)")
    rx.add_argument("--verbose", action="store_true")
    rx.add_argument("--batch", type=int, default=256)
    rx.set_defaults(func=cmd_reindex)

    st = sub.add_parser("status", help="health + drift report + recent audit")
    st.set_defaults(func=cmd_status)

    sr = sub.add_parser("search", help="hybrid search")
    sr.add_argument("query")
    sr.add_argument("--top-k", type=int, default=10)
    sr.add_argument("--pool", type=int, default=50)
    sr.add_argument("--role", default="any")
    sr.set_defaults(func=cmd_search)

    b = sub.add_parser("backup", help="safe tarball of DB + Chroma")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="restore from a backup tarball")
    r.add_argument("tarball")
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_restore)

    v = sub.add_parser("verify", help="integrity + consistency check")
    v.set_defaults(func=cmd_verify)

    au = sub.add_parser("audit", help="recent operational events")
    au.add_argument("--limit", type=int, default=20)
    au.set_defaults(func=cmd_audit)

    cs = sub.add_parser("cross-sync", help="bidirectional jsonl rsync with peers")
    cs.add_argument("--peer", action="append",
                    help="peer 'user@host'. Repeatable. If omitted, uses ANAMNESTIC_PEERS / peers.txt")
    cs.add_argument("--verbose", action="store_true")
    cs.set_defaults(func=cmd_cross_sync)

    er = sub.add_parser("errors", help="show ingest errors")
    er.add_argument("--limit", type=int, default=50)
    er.add_argument("--all", action="store_true",
                    help="include resolved errors (default: open only)")
    er.set_defaults(func=cmd_errors)

    ent = sub.add_parser("entities", help="extract entities (paths, URLs) from turns")
    ent.set_defaults(func=cmd_entities)

    th = sub.add_parser("threads", help="recompute session continuation threads")
    th.set_defaults(func=cmd_threads)

    ar = sub.add_parser("archive", help="archive old low-importance turns (opt-in)")
    ar.add_argument("--age", type=int, default=None, help="min age in days (default: ARCHIVE_AGE_DAYS)")
    ar.add_argument("--threshold", type=float, default=0.3, help="importance threshold")
    ar.set_defaults(func=cmd_archive)

    e = sub.add_parser("eval", help="run golden eval")
    e.add_argument("--mode", choices=["semantic", "hybrid", "bm25"], default="hybrid")
    e.add_argument("--top-k", type=int, default=10)
    e.add_argument("--golden", default=None)
    e.set_defaults(func=cmd_eval)

    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    rc = args.func(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
