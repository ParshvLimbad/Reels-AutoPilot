import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import time
import config
import helpers as Helper
import reels,poster,shorts,remover
from instagrapi import Client
import auth
from datetime import datetime, timedelta
import random
import traceback


Helper.load_all_config()

next_reels_scraper_run_at = datetime.now()
next_poster_run_at = datetime.now()
next_remover_run_at = datetime.now()
next_youtube_run_at = datetime.now()
api = None

def ensure_login():
    """Login with automatic retry."""
    global api
    max_retries = 3
    for attempt in range(max_retries):
        try:
            api = auth.login()
            return api
        except Exception as e:
            print(f"[Login] Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
    print("[Login] All attempts failed. Will retry in next loop.")
    return None

if config.IS_ENABLED_REELS_SCRAPER == "1" or config.IS_ENABLED_AUTO_POSTER == "1":
    api = ensure_login()

while True:
    try:
        # Reload config each loop iteration to pick up dashboard changes
        Helper.load_all_config()

        if config.IS_ENABLED_REELS_SCRAPER == "1" or config.IS_ENABLED_AUTO_POSTER == "1":

            # Re-login if api is None (previous login failed)
            if api is None:
                api = ensure_login()
                if api is None:
                    print("[Main] No API connection. Sleeping 60s before retry...")
                    time.sleep(60)
                    continue

            if config.IS_ENABLED_REELS_SCRAPER == "1":
                if next_reels_scraper_run_at < datetime.now():
                    try:
                        print("[Scraper] Scraping Reels...")
                        reels.main(api)
                        next_reels_scraper_run_at = datetime.now() + timedelta(seconds=int(config.SCRAPER_INTERVAL_IN_MIN)*60)
                        print(f"[Scraper] Next scrape at: {next_reels_scraper_run_at.strftime('%H:%M:%S')}")
                    except Exception as e:
                        print(f"[Scraper] Error: {type(e).__name__}: {e}")
                        next_reels_scraper_run_at = datetime.now() + timedelta(seconds=60)

            if config.IS_ENABLED_AUTO_POSTER == "1":
                if next_poster_run_at < datetime.now():
                    try:
                        print("[Poster] Attempting to post a reel...")
                        success = poster.main(api)
                        interval_secs = (int(config.POSTING_INTERVAL_IN_MIN) * 60) + random.randint(5, 20)
                        next_poster_run_at = datetime.now() + timedelta(seconds=interval_secs)
                        if success:
                            print(f"[Poster] Next post at: {next_poster_run_at.strftime('%H:%M:%S')}")
                        else:
                            print(f"[Poster] Post failed or skipped. Will retry at: {next_poster_run_at.strftime('%H:%M:%S')}")
                    except Exception as e:
                        print(f"[Poster] Error: {type(e).__name__}: {e}")
                        next_poster_run_at = datetime.now() + timedelta(seconds=120)
                        # If it's a login error, reset api to force re-login
                        if 'login_required' in str(e).lower() or 'LoginRequired' in type(e).__name__:
                            print("[Poster] Session expired. Will re-login on next loop.")
                            api = None

        if config.IS_REMOVE_FILES == "1":
            if next_remover_run_at < datetime.now():
                try:
                    remover.main()
                    next_remover_run_at = datetime.now() + timedelta(seconds=int(config.REMOVE_FILE_AFTER_MINS)*60)
                except Exception as e:
                    print(f"[Remover] Error: {e}")
                    next_remover_run_at = datetime.now() + timedelta(seconds=300)

        if config.IS_ENABLED_YOUTUBE_SCRAPING == "1":
            if next_youtube_run_at < datetime.now():
                try:
                    shorts.main()
                    next_youtube_run_at = datetime.now() + timedelta(seconds=int(config.SCRAPER_INTERVAL_IN_MIN)*60)
                except Exception as e:
                    print(f"[YouTube] Error: {e}")
                    next_youtube_run_at = datetime.now() + timedelta(seconds=300)

    except Exception as e:
        print(f"[Main] Unexpected error: {type(e).__name__}: {e}")
        traceback.print_exc()
        time.sleep(30)

    time.sleep(1)