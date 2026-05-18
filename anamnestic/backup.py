"""Safe backup of SQLite + Chroma to a dated tarball.

Uses SQLite .backup API (WAL-safe, online copy). Tars Chroma directory.
Output: ~/anamnestic-backups/claude-mem-YYYYMMDD-HHMMSS.tar.gz
"""
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

from anamnestic.config import DB_PATH, CHROMA_DIR, BACKUP_ROOT, BACKUP_KEEP_LAST

KEEP_LAST = BACKUP_KEEP_LAST


def _safe_sqlite_copy(dst):
    """Online, consistent copy of WAL-mode SQLite."""
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dst)
    with dst_conn:
        src.backup(dst_conn)
    dst_conn.close()
    src.close()


def run():
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(BACKUP_ROOT) / f"work-{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    db_copy = work / "claude-mem.db"
    chroma_copy = work / "semantic-chroma"

    _safe_sqlite_copy(str(db_copy))
    if os.path.isdir(CHROMA_DIR):
        shutil.copytree(CHROMA_DIR, chroma_copy)
    else:
        chroma_copy.mkdir(parents=True, exist_ok=True)

    archive = Path(BACKUP_ROOT) / f"claude-mem-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(db_copy, arcname="claude-mem.db")
        tf.add(chroma_copy, arcname="semantic-chroma")
    shutil.rmtree(work)

    # retention
    archives = sorted(Path(BACKUP_ROOT).glob("claude-mem-*.tar.gz"))
    if len(archives) > KEEP_LAST:
        for old in archives[:-KEEP_LAST]:
            old.unlink()

    size_mb = archive.stat().st_size / (1024 * 1024)
    return {"path": str(archive), "size_mb": round(size_mb, 1)}


if __name__ == "__main__":
    info = run()
    print(f"Backup: {info['path']} ({info['size_mb']} MB)")
