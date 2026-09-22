"""Runtime state persistence, crash-recovery markers and upload locking.

* `state.json` keeps timers and live status so a restart resumes cleanly.
* Pending-upload markers let the bot detect uploads interrupted by a crash.
* A file lock serialises uploads so the same reel can never be posted twice.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import config
from logger import get_logger

log = get_logger(__name__)

LOCK_STALE_SECONDS = 900  # a lock older than 15 minutes is considered abandoned
LOCK_WAIT_SECONDS = 30
LOCK_POLL_SECONDS = 0.5


# --------------------------------------------------------------------------- #
# state.json                                                                   #
# --------------------------------------------------------------------------- #
def load_state() -> Dict[str, Any]:
    """Load persisted runtime state, returning an empty dict when missing."""
    path = config.STATE_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"Could not read state file: {exc}")
        return {}


def save_state(state: Dict[str, Any]) -> None:
    """Atomically persist runtime state to disk."""
    path = config.STATE_FILE
    tmp_path = f"{path}.tmp"
    payload = dict(state)
    payload["saved_at"] = datetime.now().isoformat()
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        os.replace(tmp_path, path)
    except OSError as exc:
        log.warning(f"Could not write state file: {exc}")


def update_state(**values: Any) -> Dict[str, Any]:
    """Merge the given values into the persisted state and return it."""
    state = load_state()
    state.update(values)
    save_state(state)
    return state


# --------------------------------------------------------------------------- #
# pending upload markers (crash recovery)                                      #
# --------------------------------------------------------------------------- #
def _marker_path(account: str) -> str:
    """Return the marker file path for a posting account."""
    safe = "".join(ch for ch in (account or "default") if ch.isalnum() or ch in "._-")
    return os.path.join(config.PENDING_UPLOAD_DIR, f"{safe or 'default'}.json")


def write_pending_upload(account: str, reel_code: str, file_path: str = "") -> None:
    """Record that an upload is about to start."""
    try:
        with open(_marker_path(account), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "account": account,
                    "code": reel_code,
                    "file_path": file_path,
                    "started_at": datetime.now().isoformat(),
                },
                handle,
            )
    except OSError as exc:
        log.warning(f"Could not write pending-upload marker: {exc}")


def clear_pending_upload(account: str) -> None:
    """Remove the pending-upload marker after an upload finished."""
    try:
        os.remove(_marker_path(account))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning(f"Could not clear pending-upload marker: {exc}")


def read_pending_upload(account: str) -> Optional[Dict[str, Any]]:
    """Return the pending-upload marker for an account, if any."""
    path = _marker_path(account)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def all_pending_uploads() -> List[Dict[str, Any]]:
    """Return every pending-upload marker found on disk."""
    markers: List[Dict[str, Any]] = []
    if not os.path.isdir(config.PENDING_UPLOAD_DIR):
        return markers
    for name in os.listdir(config.PENDING_UPLOAD_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(config.PENDING_UPLOAD_DIR, name), "r", encoding="utf-8") as handle:
                markers.append(json.load(handle))
        except (OSError, json.JSONDecodeError):
            continue
    return markers


# --------------------------------------------------------------------------- #
# upload lock                                                                  #
# --------------------------------------------------------------------------- #
@contextmanager
def upload_lock(owner: str = "", wait_seconds: int = LOCK_WAIT_SECONDS):
    path = config.UPLOAD_LOCK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+b")
    if os.path.getsize(path) == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while time.monotonic() <= deadline:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def write_heartbeat():
    with open(os.path.join(config.STATE_DIR, "heartbeat"), "w") as f:
        f.write(str(time.time()))


def write_progress():
    with open(os.path.join(config.STATE_DIR, "progress"), "w") as f:
        f.write(str(time.time()))
