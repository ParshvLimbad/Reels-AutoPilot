import sys
import time
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired

USERNAME = "viewsgimmeviews"
PASSWORD = "gimmeviews2004l"
SESSION_FILE = "session.json"

print(f"Starting login loop for {USERNAME}...")
print("If Instagram blocks the login, open your Instagram app on your phone.")
print("Look for a 'Was this you?' prompt and tap 'Yes, it was me'.")
print("This script will keep trying every 15 seconds until it succeeds.\n")

while True:
    api = Client()
    api.delay_range = [1, 3]
    try:
        print("Attempting login...")
        api.login(USERNAME, PASSWORD)
        api.dump_settings(SESSION_FILE)
        print("\n✅ SUCCESS! Logged in and saved session.json")
        break
    except ChallengeRequired as e:
        print(f"❌ Blocked by ChallengeRequired. Please check your phone/email to approve the login!")
    except Exception as e:
        print(f"❌ Login failed: {type(e).__name__}: {e}")
    
    print("Waiting 15 seconds before retrying...\n")
    time.sleep(15)
