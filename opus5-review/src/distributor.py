"""Reel distribution, swapping and recycling across posting accounts.

Rules implemented here:
* new reels are split round-robin across the enabled posting accounts;
* an account only posts reels assigned to it;
* when every account is empty the assignments are swapped (A->B, B->C, C->A);
* a reel is never swapped to an account that already posted it;
* when all accounts posted everything, the whole pool is reset (last resort);
* single-account installs fall back to simple recycling of the oldest reels.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

import config
import helpers as Helper
import notifier
from db import Reel, Session
from logger import get_logger

log = get_logger(__name__)

LAST_POSTED_CONFIG_KEY = "LAST_POSTED_CODES"
SWAP_PHASE_CONFIG_KEY = "SWAP_PHASE"


# --------------------------------------------------------------------------- #
# last posted memory                                                           #
# --------------------------------------------------------------------------- #
def get_last_posted_codes() -> List[str]:
    """Return the most recently posted reel codes (newest first)."""
    raw = Helper.get_config(LAST_POSTED_CONFIG_KEY) or "[]"
    try:
        codes = json.loads(raw)
        return [str(code) for code in codes] if isinstance(codes, list) else []
    except (TypeError, ValueError):
        return []


def remember_posted_code(code: str) -> List[str]:
    """Push a reel code into the 'recently posted' memory."""
    limit = int(getattr(config, "LAST_POSTED_MEMORY", 20))
    codes = [c for c in get_last_posted_codes() if c != code]
    codes.insert(0, code)
    codes = codes[:limit]
    Helper.save_config(LAST_POSTED_CONFIG_KEY, json.dumps(codes))
    return codes


# --------------------------------------------------------------------------- #
# posted_by bookkeeping                                                        #
# --------------------------------------------------------------------------- #
def posted_by_list(value: Optional[str]) -> List[str]:
    """Parse the comma separated `posted_by` column."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def add_posted_by(current: Optional[str], username: str) -> str:
    """Append a posting account to the `posted_by` list without duplicates."""
    accounts = posted_by_list(current)
    if username and username not in accounts:
        accounts.append(username)
    return ",".join(accounts)


# --------------------------------------------------------------------------- #
# distribution                                                                 #
# --------------------------------------------------------------------------- #
def pending_count(username: str) -> int:
    """Count unposted reels assigned to an account (file must exist)."""
    session = Session()
    try:
        reels = (
            session.query(Reel)
            .filter(Reel.is_posted == False)  # noqa: E712 - SQLAlchemy expression
            .filter(Reel.assigned_to == username)
            .all()
        )
        return sum(1 for reel in reels if reel.file_path and os.path.exists(reel.file_path))
    finally:
        session.close()


def counts_by_account(usernames: List[str]) -> Dict[str, Dict[str, int]]:
    """Return assigned/posted/pending counts per posting account."""
    session = Session()
    stats: Dict[str, Dict[str, int]] = {}
    try:
        for username in usernames:
            assigned = session.query(Reel).filter(Reel.assigned_to == username).count()
            posted = (
                session.query(Reel)
                .filter(Reel.assigned_to == username, Reel.is_posted == True)  # noqa: E712
                .count()
            )
            stats[username] = {
                "assigned": assigned,
                "posted": posted,
                "pending": assigned - posted,
            }
    finally:
        session.close()
    return stats


def distribute_unassigned(usernames: List[str]) -> int:
    """Split unassigned reels evenly (round-robin) across posting accounts.

    Returns the number of reels that were assigned.
    """
    if not usernames:
        return 0

    session = Session()
    assigned = 0
    try:
        unassigned = (
            session.query(Reel)
            .filter((Reel.assigned_to == None) | (Reel.assigned_to == ""))  # noqa: E711
            .order_by(Reel.id)
            .all()
        )
        if not unassigned:
            return 0

        # start round-robin at the account that currently owns the fewest reels
        totals = {
            username: session.query(Reel).filter(Reel.assigned_to == username).count()
            for username in usernames
        }
        order = sorted(usernames, key=lambda name: totals[name])

        for index, reel in enumerate(unassigned):
            reel.assigned_to = order[index % len(order)]
            if reel.swap_phase is None:
                reel.swap_phase = 0
            assigned += 1
        session.commit()
        log.info(f"Distributed {assigned} new reels across {len(usernames)} posting account(s).")
        return assigned
    except Exception as exc:  # pragma: no cover - never crash the loop
        session.rollback()
        log.error(f"Distribution failed: {exc}")
        return 0
    finally:
        session.close()


def next_owner(current: str, usernames: List[str], already_posted: List[str]) -> Optional[str]:
    """Return the next account in rotation that has not posted this reel yet."""
    if not usernames:
        return None
    try:
        start = usernames.index(current)
    except ValueError:
        start = -1
    for offset in range(1, len(usernames) + 1):
        candidate = usernames[(start + offset) % len(usernames)]
        if candidate not in already_posted:
            return candidate
    return None


def swap_assignments(usernames: List[str]) -> int:
    """Rotate posted reels to the next account that has not posted them.

    Returns the number of reels handed over to another account.
    """
    if len(usernames) < 2:
        return 0

    session = Session()
    swapped = 0
    try:
        posted_reels = (
            session.query(Reel)
            .filter(Reel.is_posted == True)  # noqa: E712
            .order_by(Reel.posted_at.asc(), Reel.id.asc())
            .all()
        )
        for reel in posted_reels:
            if not reel.file_path or not os.path.exists(reel.file_path):
                continue
            history = posted_by_list(reel.posted_by)
            owner = reel.assigned_to or (history[-1] if history else usernames[0])
            candidate = next_owner(owner, usernames, history)
            if candidate is None:
                continue  # fully exhausted, handled by reset_all()
            reel.assigned_to = candidate
            reel.is_posted = False
            reel.swap_phase = int(reel.swap_phase or 0) + 1
            swapped += 1
        session.commit()
    except Exception as exc:  # pragma: no cover
        session.rollback()
        log.error(f"Swap failed: {exc}")
        return 0
    finally:
        session.close()

    if swapped:
        phase = int(Helper.get_config(SWAP_PHASE_CONFIG_KEY) or 1) + 1
        Helper.save_config(SWAP_PHASE_CONFIG_KEY, str(phase))
        log.info(f"[Poster] All accounts exhausted. Swapping reel assignments... ({swapped} reels, phase {phase})")
        notifier.alert_swap(phase)
    return swapped


def reset_all(usernames: List[str]) -> int:
    """Last resort: clear posting history for every reel and redistribute."""
    session = Session()
    reset = 0
    try:
        reels = session.query(Reel).order_by(Reel.id).all()
        for reel in reels:
            if not reel.file_path or not os.path.exists(reel.file_path):
                continue
            reel.is_posted = False
            reel.posted_by = None
            reel.assigned_to = None
            reset += 1
        session.commit()
    except Exception as exc:  # pragma: no cover
        session.rollback()
        log.error(f"Full reset failed: {exc}")
        return 0
    finally:
        session.close()

    if reset:
        log.warning(f"[Poster] Every reel was posted by every account. Reset {reset} reels and redistributed.")
        distribute_unassigned(usernames)
    return reset


def recycle_oldest(account_filter: Optional[str] = None, batch_size: Optional[int] = None) -> int:
    """Single-account fallback: repost the oldest posted reels.

    Reels posted within the last `LAST_POSTED_MEMORY` posts are skipped so the
    same reel is never posted twice in a row. Returns how many were recycled.
    """
    limit = int(batch_size or getattr(config, "RECYCLE_BATCH_SIZE", 10) or getattr(config, "FETCH_LIMIT", 10))
    recent = set(get_last_posted_codes())

    session = Session()
    recycled = 0
    try:
        query = session.query(Reel).filter(Reel.is_posted == True)  # noqa: E712
        if account_filter:
            query = query.filter(Reel.account == account_filter)
        candidates = query.order_by(Reel.posted_at.asc(), Reel.id.asc()).all()

        for reel in candidates:
            if recycled >= limit:
                break
            if reel.code in recent:
                continue
            if not reel.file_path or not os.path.exists(reel.file_path):
                continue
            reel.is_posted = False
            reel.swap_phase = int(reel.swap_phase or 0) + 1
            recycled += 1
        session.commit()
    except Exception as exc:  # pragma: no cover
        session.rollback()
        log.error(f"Recycling failed: {exc}")
        return 0
    finally:
        session.close()
    return recycled


def source_account_with_most_posted() -> Optional[str]:
    """Return the source account with the most posted reels (recycle first)."""
    session = Session()
    try:
        rows = session.query(Reel).filter(Reel.is_posted == True).all()  # noqa: E712
        counts: Dict[str, int] = {}
        for reel in rows:
            key = reel.account or "unknown"
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda name: counts[name])
    finally:
        session.close()


def recycle_round_robin() -> int:
    """Recycle a batch from the source account with the most posted reels."""
    account = source_account_with_most_posted()
    if account is None:
        return 0
    log.info(f"[Poster] No new reels available. Recycling older content from @{account}...")
    recycled = recycle_oldest(account_filter=account)
    if recycled == 0:
        recycled = recycle_oldest()  # ignore the source filter as a fallback
    if recycled:
        notifier.alert_recycling(account)
    return recycled


def mark_posted(code: str, posted_by: str) -> bool:
    """Mark a reel as posted and record which account posted it.

    Returns True when the DB write was verified.
    """
    session = Session()
    try:
        reel = session.query(Reel).filter_by(code=code).first()
        if reel is None:
            return False
        reel.is_posted = True
        reel.posted_at = datetime.now()
        reel.posted_by = add_posted_by(reel.posted_by, posted_by)
        session.commit()

        verified = session.query(Reel).filter_by(code=code).first()
        success = bool(verified and verified.is_posted)
        if not success:
            log.error(f"DB write verification failed for reel {code}.")
        return success
    except Exception as exc:
        session.rollback()
        log.error(f"Could not mark reel {code} as posted: {exc}")
        return False
    finally:
        session.close()
