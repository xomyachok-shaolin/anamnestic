"""Restore claude-mem data from a backup tarball.

Usage:
    anamnestic restore <path/to/claude-mem-YYYYMMDD-HHMMSS.tar.gz>

Safety:
- Refuses to overwrite unless --force is passed.
- Stages into a temp dir then atomic-swaps DB and Chroma dir.
- Keeps the previous DB/Chroma as `<name>.pre-restore-<stamp>` until manually deleted.
"""
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from anamnestic.config import CHROMA_DIR, DB_PATH, DATA_DIR


def _safe_extractall(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball, refusing members that would escape ``dest`` (zip-slip).

    On Python 3.12+ this delegates to the stdlib ``data`` filter. On 3.11 (no
    filter support) it rejects any member whose name is absolute or contains a
    ``..`` component, and skips link members whose target escapes ``dest``.
    Behavior is identical for benign archives.
    """
    if sys.version_info >= (3, 12):
        tf.extractall(dest, filter="data")
        return

    dest = dest.resolve()
    safe_members = []
    for member in tf.getmembers():
        name = member.name
        # Reject absolute paths and any '..' traversal component.
        if os.path.isabs(name) or name.startswith(("/", os.sep)):
            raise RuntimeError(f"refusing unsafe tar member (absolute path): {name!r}")
        parts = name.replace("\\", "/").split("/")
        if ".." in parts:
            raise RuntimeError(f"refusing unsafe tar member (path traversal): {name!r}")
        # Reject symlinks/hardlinks whose target escapes the staging dir.
        if member.islnk() or member.issym():
            link = member.linkname
            # symlink targets resolve relative to the link's own directory;
            # hardlink names are relative to the archive root (dest).
            base = (dest / Path(name).parent) if member.issym() else dest
            resolved = (base / link).resolve()
            if not (resolved == dest or dest in resolved.parents):
                raise RuntimeError(
                    f"refusing unsafe tar link member (escapes staging): {name!r} -> {link!r}"
                )
        safe_members.append(member)
    tf.extractall(dest, members=safe_members)


def run(tarball: str, force: bool = False) -> dict:
    tarball = os.path.expanduser(tarball)
    if not os.path.isfile(tarball):
        raise FileNotFoundError(tarball)

    data_dir = DATA_DIR
    stamp = time.strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory(dir=str(data_dir)) as staging:
        staging = Path(staging)
        with tarfile.open(tarball, "r:gz") as tf:
            _safe_extractall(tf, staging)

        src_db = staging / "claude-mem.db"
        src_chroma = staging / "semantic-chroma"
        if not src_db.exists() or not src_chroma.exists():
            raise RuntimeError(
                f"Tarball missing expected members (claude-mem.db, semantic-chroma). "
                f"Found: {[p.name for p in staging.iterdir()]}"
            )

        # Safety: back up current state before swap
        db_target = Path(DB_PATH)
        chroma_target = Path(CHROMA_DIR)

        db_pre = db_target.with_suffix(f".db.pre-restore-{stamp}")
        chroma_pre = chroma_target.parent / f"{chroma_target.name}.pre-restore-{stamp}"

        if db_target.exists():
            if not force and db_pre.exists():
                raise RuntimeError(f"refuse to overwrite existing {db_pre}")
            shutil.move(str(db_target), str(db_pre))
        if chroma_target.exists():
            shutil.move(str(chroma_target), str(chroma_pre))

        # SQLite WAL/SHM sidecars are stale after restore — remove if present
        for sidecar in (db_target.with_suffix(".db-wal"), db_target.with_suffix(".db-shm")):
            if sidecar.exists():
                sidecar.unlink()

        shutil.move(str(src_db), str(db_target))
        shutil.move(str(src_chroma), str(chroma_target))

        return {
            "restored_from": tarball,
            "db": str(db_target),
            "chroma": str(chroma_target),
            "prev_db_saved_as": str(db_pre) if db_pre.exists() else None,
            "prev_chroma_saved_as": str(chroma_pre) if chroma_pre.exists() else None,
        }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    info = run(sys.argv[1], force="--force" in sys.argv)
    print(json.dumps(info, indent=2, ensure_ascii=False))
