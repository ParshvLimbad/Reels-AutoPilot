import os

#--------------------------------------------------------------------------------------------------#
# Global Configurations                                                                            #
#--------------------------------------------------------------------------------------------------#

# Base Directory (repository root)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SQLite DB path
DB_DIR = os.path.join(BASE_DIR, 'database')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'sqlite.db')

# Download Path
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads') + os.sep
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


#IS REMOVE FILES
IS_REMOVE_FILES = "1"

# Remove Posted Files Interval
REMOVE_FILE_AFTER_MINS = 120 #every two hours

#--------------------------------------------------------------------------------------------------#
# Instagram Configurations                                                                         #
#--------------------------------------------------------------------------------------------------#

# IS RUN REELS SCRAPER
IS_ENABLED_REELS_SCRAPER = "1"

# IS RUN AUTO POSTER
IS_ENABLED_AUTO_POSTER = "1"

# IS POST STORY
IS_POST_TO_STORY = "1"

# Fetch LIMIT for scraper script
FETCH_LIMIT = 10

# Posting interval in Minutes
POSTING_INTERVAL_IN_MIN = 10  # Every 10 Minutes


# Scraper interval in Minutes
SCRAPER_INTERVAL_IN_MIN = 720  # Every 12 hours

# Instagram Username & Password
USERNAME = "your_username"
PASSWORD = "your_password"

# Account List for scraping
ACCOUNTS = [
    "totalgaming_official",
    "carryminati",
    "techno_gamerz",
    "payalgamingg",
    "dynamo__gaming"
]

# like_and_view_counts_disabled
LIKE_AND_VIEW_COUNTS_DISABLED = "0"

# disable_comments
DISABLE_COMMENTS = "0"

# HASHTAGS to add while Posting (empty by default)
HASHTAGS = ""


# Custom Description / Caption to use for all Reels (optional)
CUSTOM_CAPTION = ""

# Path to custom cover image file to use for all Reels (optional)
REEL_COVER_PATH = ""


#--------------------------------------------------------------------------------------------------#
# Youtube Configurations                                                                           #
#--------------------------------------------------------------------------------------------------#

# IS RUN YOUTUBE SCRAPER
IS_ENABLED_YOUTUBE_SCRAPING = "1"


# IS RUN YOUTUBE SCRAPER
YOUTUBE_SCRAPING_INTERVAL_IN_MINS = 120


# YOUTUBE API KEY
YOUTUBE_API_KEY = "YOUR_API_KEY"



# YouTube Channel List short for scraping
CHANNEL_LINKS = [
    "https://www.youtube.com/@exampleChannleName."
]

# Discord Webhook URL for post notifications (optional)
DISCORD_WEBHOOK_URL = ""

#--------------------------------------------------------------------------------------------------#
# Bulletproof / Resilience Configurations                                                           #
#--------------------------------------------------------------------------------------------------#

# Logging
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "application.log")
LOG_MAX_BYTES = 5 * 1024 * 1024      # 5 MB per log file
LOG_BACKUP_COUNT = 3                  # keep 3 rotated backups
LOG_LEVEL = "INFO"                   # DEBUG | INFO | WARNING | ERROR

# Session storage (one session file per posting account)
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
SESSION_FILE = os.path.join(BASE_DIR, "session.json")   # legacy single-account session

# Runtime state / crash-recovery markers
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_UPLOAD_DIR = os.path.join(STATE_DIR, "pending")
os.makedirs(PENDING_UPLOAD_DIR, exist_ok=True)
UPLOAD_LOCK_FILE = os.path.join(STATE_DIR, "upload.lock")

# Retry / backoff constants (no magic numbers elsewhere)
MAX_LOGIN_ATTEMPTS = 3
LOGIN_RETRY_DELAY_SECONDS = 10
LOGIN_FAILURE_RETRY_SECONDS = 300          # 5 minutes instead of 60s when login is dead
API_MAX_RETRIES = 4
API_BACKOFF_BASE_SECONDS = 5               # exponential: 5, 10, 20, 40
API_BACKOFF_MAX_SECONDS = 600
RATE_LIMIT_BACKOFF_SECONDS = 900           # 15 min cool-off after HTTP 429
SESSION_HEALTHCHECK_INTERVAL_SECONDS = 1800  # 30 minutes
CHALLENGE_BACKOFF_HOURS = [1, 4, 12, 24]   # exponential backoff for challenged sessions
SCRAPE_ACCOUNT_DELAY_RANGE = (2, 5)        # seconds between scraping two source accounts
SCRAPE_FAILURE_ALERT_THRESHOLD = 3         # consecutive failures before alerting
RECYCLE_BATCH_SIZE = 10                    # reels recycled per batch
LAST_POSTED_MEMORY = 20                    # never repost one of the last N codes
MAIN_LOOP_SLEEP_SECONDS = 1
NETWORK_RETRY_SECONDS = 60
NETWORK_CHECK_HOSTS = [("1.1.1.1", 53), ("8.8.8.8", 53)]
NETWORK_CHECK_TIMEOUT_SECONDS = 5

# Disk management
MIN_FREE_DISK_MB = 500                     # purge posted files below this threshold
DISK_LOG_INTERVAL_SECONDS = 3600           # log disk usage hourly

# Watchdog
WATCHDOG_INTERVAL_SECONDS = 300            # every 5 minutes
WATCHDOG_POST_STALL_MULTIPLIER = 5         # POSTING_INTERVAL * 5 without a post => restart
WATCHDOG_WEB_URL = "http://127.0.0.1:8080/api/health"
AUTOPILOT_SERVICE = "reels-autopilot"
WEB_SERVICE = "reels-web"

# Dashboard
DASHBOARD_LOG_LINES = 100
