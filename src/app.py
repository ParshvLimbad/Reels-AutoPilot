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


Helper.load_all_config()

next_reels_scraper_run_at = datetime.now()
next_poster_run_at = datetime.now()
next_remover_run_at = datetime.now()
next_youtube_run_at = datetime.now()

if config.IS_ENABLED_REELS_SCRAPER == "1" or config.IS_ENABLED_AUTO_POSTER == "1" : 
        #Instagram login client is here
        api = auth.login()

while True:
    
    if config.IS_ENABLED_REELS_SCRAPER == "1" or config.IS_ENABLED_AUTO_POSTER == "1" :     

        if config.IS_ENABLED_REELS_SCRAPER == "1" :
            if next_reels_scraper_run_at < datetime.now() :
                print("[Scraper] Scraping Reels...")
                reels.main(api)
                next_reels_scraper_run_at = datetime.now() + timedelta(seconds=int(config.SCRAPER_INTERVAL_IN_MIN)*60)
                print(f"[Scraper] Next scrape at: {next_reels_scraper_run_at.strftime('%H:%M:%S')}")

        if config.IS_ENABLED_AUTO_POSTER == "1" :
            if next_poster_run_at < datetime.now() :
                print("[Poster] Attempting to post a reel...")
                success = poster.main(api)
                interval_secs = (int(config.POSTING_INTERVAL_IN_MIN) * 60) + random.randint(5, 20)
                next_poster_run_at = datetime.now() + timedelta(seconds=interval_secs)
                if success:
                    print(f"[Poster] Next post at: {next_poster_run_at.strftime('%H:%M:%S')}")
                else:
                    print(f"[Poster] Post failed or skipped. Will retry at: {next_poster_run_at.strftime('%H:%M:%S')}")

    
        
    if config.IS_REMOVE_FILES == "1" :
        if next_remover_run_at < datetime.now() :
            remover.main()
            next_remover_run_at = datetime.now() + timedelta(seconds=int(config.REMOVE_FILE_AFTER_MINS)*60)

    if config.IS_ENABLED_YOUTUBE_SCRAPING == "1":
        if next_youtube_run_at < datetime.now() :
            shorts.main()
            next_youtube_run_at = datetime.now() + timedelta(seconds=int(config.SCRAPER_INTERVAL_IN_MIN)*60)

    time.sleep(1)