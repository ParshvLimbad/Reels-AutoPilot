"""Disk space monitoring and emergency purge of posted reel files."""
from __future__ import annotations

import os
import shutil
from typing import Dict

import config
import notifier
from logger import get_logger

log = get_logger(__name__)

BYTES_PER_MB = 1024 * 1024


def usage() -> Dict[str, float]:
    """Return disk usage for the download directory in megabytes."""
    try:
        total, used, free = shutil.disk_usage(config.DOWNLOAD_DIR)
    except OSError as exc:
        log.warning(f"Could not read disk usage: {exc}")
        return {"total_mb": 0.0, "used_mb": 0.0, "free_mb": 0.0, "percent_used": 0.0}
    return {
        "total_mb": round(total / BYTES_PER_MB, 1),
        "used_mb": round(used / BYTES_PER_MB, 1),
        "free_mb": round(free / BYTES_PER_MB, 1),
        "percent_used": round((used / total) * 100, 1) if total else 0.0,
    }


def free_mb() -> float:
    """Return free megabytes on the volume holding the downloads folder."""
    return usage()["free_mb"]


def downloads_size_mb() -> float:
    """Return the total size of the downloads folder in megabytes."""
    total = 0
    for root, _dirs, files in os.walk(config.DOWNLOAD_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return round(total / BYTES_PER_MB, 1)


def purge_posted_files() -> int:
    """Delete video files of reels that have already been posted.

    Rows are kept in the DB so history and swap logic remain intact.
    Returns the number of files deleted.
    """
    from db import Session, Reel  # local import keeps module import order simple

    session = Session()
    removed = 0
    try:
        posted = session.query(Reel).filter_by(is_posted=True).all()
        for reel in posted:
            path = reel.file_path
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    removed += 1
                except OSError as exc:
                    log.warning(f"Could not remove {path}: {exc}")
            thumb = f"{path}.jpg" if path else None
            if thumb and os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    pass
            # Also delete the DB row so the reel can be re-scraped later
            session.delete(reel)
        session.commit()
    finally:
        session.close()
    log.info(f"Disk purge removed {removed} posted reel files and DB rows.")
    return removed


def ensure_free_space(minimum_mb: float = None) -> bool:
    """Ensure the minimum free space is available, purging posted files if not.

    Returns True when there is enough space after the (optional) purge.
    """
    threshold = float(minimum_mb if minimum_mb is not None else getattr(config, "MIN_FREE_DISK_MB", 500))
    available = free_mb()
    if available >= threshold:
        return True
    log.warning(f"Low disk space: {available} MB free (threshold {threshold} MB). Purging posted files...")
    notifier.alert_disk_low(available)
    purge_posted_files()
    available = free_mb()
    if available < threshold:
        log.error(f"Still low on disk after purge: {available} MB free.")
        return False
    log.info(f"Disk space recovered: {available} MB free.")
    return True


def log_usage() -> Dict[str, float]:
    """Log and return current disk usage."""
    stats = usage()
    log.info(
        f"Disk usage: {stats['used_mb']} MB used / {stats['total_mb']} MB total "
        f"({stats['percent_used']}%), {stats['free_mb']} MB free, "
        f"downloads folder {downloads_size_mb()} MB"
    )
    return stats
