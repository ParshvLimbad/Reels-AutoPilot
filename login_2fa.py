import sys
import time
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, TwoFactorRequired

USERNAME = "viewsgimmeviews"
PASSWORD = "gimmeviews2004l"
SESSION_FILE = "session.json"

print(f"Starting login for {USERNAME}...")

api = Client()
api.delay_range = [1, 3]

try:
    print("Attempting login...")
    api.login(USERNAME, PASSWORD)
    api.dump_settings(SESSION_FILE)
    print("\n✅ SUCCESS! Logged in and saved session.json")

except TwoFactorRequired as e:
    print("\n🔒 2FA is required!")
    code = input("Enter the 6-digit code from your Authenticator app: ")
    try:
        api.login(USERNAME, PASSWORD, verification_code=code)
        api.dump_settings(SESSION_FILE)
        print("\n✅ SUCCESS! Logged in and saved session.json")
    except Exception as e2:
        print(f"\n❌ 2FA Login failed: {type(e2).__name__}: {e2}")

except ChallengeRequired as e:
    print(f"\n❌ Blocked by ChallengeRequired even with 2FA enabled. Try approving on phone first.")
except Exception as e:
    print(f"\n❌ Login failed: {type(e).__name__}: {e}")
