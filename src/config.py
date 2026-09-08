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
