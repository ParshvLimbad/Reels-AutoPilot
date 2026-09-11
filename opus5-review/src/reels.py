"""Scrapes reels from the configured source Instagram accounts.

Adds per-account success/failure tracking, inter-account delays to dodge rate
limits, disk-space checks and Discord alerts for accounts that keep failing.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired, PrivateAccount, UserNotFound

import config
import diskspace
import helpers as Helper
import notifier
from db import Reel, ScrapeStatus, Session
from logger import get_logger

log = get_logger(__name__)

from helpers import print  # noqa: A001, E402 - preserved original helper import


def _source_accounts() -> List[str]:
    """Return the configured source accounts as a clean list."""
    accounts = config.ACCOUNTS
    if isinstance(accounts, str):
        accounts = [item.strip() for item in accounts.split(",") if item.strip()]
    return [str(item).strip() for item in accounts if str(item).strip()]


def record_scrape_result(
    account: str,
    success: bool,
    found: int = 0,
    downloaded: int = 0,
    error: str = "",
) -> int:
    """Persist the scrape outcome for one source account.

    Returns the number of consecutive failures for that account.
    """
    session = Session()
    try:
        row = session.query(ScrapeStatus).filter_by(account=account).first()
        now = datetime.now()
        if row is None:
            row = ScrapeStatus(account=account, consecutive_failures=0, reels_found=0, reels_downloaded=0)
            session.add(row)
        row.last_scraped_at = now
        row.updated_at = now
        if success:
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error = ""
            row.reels_found = int(found)
            row.reels_downloaded = int((row.reels_downloaded or 0) + downloaded)
        else:
            row.consecutive_failures = int((row.consecutive_failures or 0) + 1)
            row.last_error = error[:1000]
        failures = int(row.consecutive_failures or 0)
        session.commit()
        return failures
    except Exception as exc:  # pragma: no cover - never break scraping
        session.rollback()
        log.warning(f"Could not record scrape status for @{account}: {exc}")
        return 0
    finally:
        session.close()


def get_scrape_status() -> List[Dict[str, Any]]:
    """Return per source-account scrape statistics for the dashboard."""
    session = Session()
    try:
        rows = session.query(ScrapeStatus).order_by(ScrapeStatus.account).all()
        return [
            {
                "account": row.account,
                "last_scraped_at": row.last_scraped_at.isoformat() if row.last_scraped_at else None,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "consecutive_failures": int(row.consecutive_failures or 0),
                "reels_found": int(row.reels_found or 0),
                "reels_downloaded": int(row.reels_downloaded or 0),
                "last_error": row.last_error or "",
            }
            for row in rows
        ]
    finally:
        session.close()


def get_reels(account: str, api: Client) -> List[Any]:
    """Fetch the latest video reels of one source account."""
    account_name = str(account).strip()
    if not account_name:
        return []
    user_id = api.user_id_from_username(account_name)
    fetch_limit = int(getattr(config, "FETCH_LIMIT", 10))
    medias = api.user_medias(user_id, fetch_limit)
    return [item for item in medias if item.media_type == 2]  # video reels only


def main(api: Client) -> int:
    """Scrape every configured source account. Returns new reels downloaded."""
    Helper.load_all_config()
    accounts = _source_accounts()
    if not accounts:
        log.warning("[Scraper] No source accounts configured.")
        return 0

    diskspace.ensure_free_space()

    total_new = 0
    succeeded: List[str] = []
    failed: List[str] = []
    delay_min, delay_max = getattr(config, "SCRAPE_ACCOUNT_DELAY_RANGE", (2, 5))

    for index, account_name in enumerate(accounts):
        if index > 0:
            time.sleep(random.uniform(float(delay_min), float(delay_max)))

        try:
            reels_by_account = get_reels(account_name, api)
        except (UserNotFound, PrivateAccount, LoginRequired, ClientError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            failures = record_scrape_result(account_name, success=False, error=error)
            failed.append(account_name)
            log.error(f"[Scraper] @{account_name} failed ({failures} in a row): {error}")
            if failures >= int(getattr(config, "SCRAPE_FAILURE_ALERT_THRESHOLD", 3)):
                notifier.alert_scrape_failure(account_name, failures, error)
            continue

        downloaded = 0
        session = Session()
        try:
            for reel in reels_by_account:
                if reel.video_url is None:
                    continue
                try:
                    exists = session.query(Reel).filter_by(code=reel.code).first()
                    if exists:
                        continue
                    if not diskspace.ensure_free_space():
                        log.error("[Scraper] Not enough disk space to continue downloading.")
                        break

                    log.info(f"[Scraper] Downloading reel {reel.code} from @{account_name}")
                    downloaded_path = api.video_download_by_url(reel.video_url, folder=config.DOWNLOAD_DIR)
                    filepath = str(downloaded_path)
                    filename = os.path.basename(filepath)

                    reel_db = Reel(
                        post_id=reel.id,
                        code=reel.code,
                        account=account_name,
                        caption=reel.caption_text,
                        file_name=filename,
                        file_path=filepath,
                        data=json.dumps(reel, cls=__import__("db").ReelEncoder),
                        is_posted=False,
                        swap_phase=0,
                    )
                    session.add(reel_db)
                    session.commit()
                    downloaded += 1
                    total_new += 1
                    log.info(f"[Scraper] Saved reel {reel.code} -> {filepath}")
                except (ClientError, OSError, ValueError) as exc:
                    session.rollback()
                    log.warning(f"[Scraper] Could not save reel {reel.code}: {type(exc).__name__}: {exc}")
                except Exception as exc:  # pragma: no cover - keep scraping
                    session.rollback()
                    log.warning(f"[Scraper] Unexpected error on reel {reel.code}: {type(exc).__name__}: {exc}")
        finally:
            session.close()

        record_scrape_result(account_name, success=True, found=len(reels_by_account), downloaded=downloaded)
        succeeded.append(account_name)
        log.info(f"[Scraper] @{account_name}: {len(reels_by_account)} reels found, {downloaded} new downloaded.")

    log.info(
        f"[Scraper] Cycle finished. Success: {', '.join(succeeded) or 'none'} | "
        f"Failed: {', '.join(failed) or 'none'} | New reels: {total_new}"
    )
    return total_new
