from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired
from rich import print
import config
import helpers as Helper
import os
import json

SESSION_FILE = os.path.join(config.BASE_DIR, 'session.json')


def _inject_session(api, sessionid, ds_user_id=None):
    """Inject session cookies directly, bypassing login API."""
    import urllib.parse
    import re
    if not ds_user_id:
        decoded_session = urllib.parse.unquote(sessionid)
        # Extract just the digits at the start of the session string
        match = re.search(r'^(\d+)', decoded_session)
        if match:
            ds_user_id = match.group(1)
        else:
            ds_user_id = sessionid.split('%')[0].split(':')[0]
    
    # Ensure ds_user_id contains only digits
    ds_user_id = re.sub(r'\D', '', str(ds_user_id))
    api.set_settings({
        "uuids": {
            "phone_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "uuid": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
            "client_session_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
            "advertising_id": "d4e5f6a7-b8c9-0123-defa-234567890123",
            "android_device_id": "android-1234567890abcdef",
            "request_id": "e5f6a7b8-c9d0-1234-efab-345678901234",
            "tray_session_id": "f6a7b8c9-d0e1-2345-fabc-456789012345",
        },
        "authorization_data": {
            "ds_user_id": ds_user_id,
            "sessionid": sessionid,
        },
        "cookies": {
            "sessionid": sessionid,
            "ds_user_id": ds_user_id,
        },
        "device_settings": {
            "cpu": "qcom",
            "dpi": "480dpi",
            "model": "SM-A546E",
            "device": "a54x",
            "resolution": "1080x2400",
            "app_version": "360.0.0.30.108",
            "manufacturer": "samsung",
            "version_code": "572810744",
            "android_release": "14",
            "android_version": 34,
        },
        "user_agent": "Instagram 360.0.0.30.108 Android (34/14; 480dpi; 1080x2400; samsung; SM-A546E; a54x; qcom; en_US; 572810744)",
    })
    api.authorization_data = {
        "ds_user_id": ds_user_id,
        "sessionid": sessionid,
    }
    api.username = config.USERNAME
    return api


# Login function
def login():
    print("   [green] Initializing login... [/green]")
    api = Client()
    api.delay_range = [1, 3]
    Helper.load_all_config()

    # Try normal session-based login first
    if os.path.exists(SESSION_FILE):
        print("   [green] Logging with previous session... [/green]")
        try:
            api.load_settings(SESSION_FILE)
            api.login(config.USERNAME, config.PASSWORD)
            api.dump_settings(SESSION_FILE)
            api.get_timeline_feed()
            print("   [green] Logged in successfully. [/green]")
            return api
        except (ChallengeRequired, LoginRequired, Exception) as e:
            print(f"   [yellow] Session login failed: {e.__class__.__name__}. Trying alternatives... [/yellow]")

    # Try fresh username/password login
    try:
        print("   [green] Logging with username and password... [/green]")
        api = Client()
        api.delay_range = [1, 3]
        api.login(config.USERNAME, config.PASSWORD)
        api.dump_settings(SESSION_FILE)
        api.get_timeline_feed()
        print("   [green] Logged in successfully. [/green]")
        return api
    except (ChallengeRequired, Exception) as e:
        print(f"   [yellow] Password login failed: {e.__class__.__name__}. Trying session cookie... [/yellow]")

    # Fallback: use SESSIONID from config DB
    sessionid = Helper.get_config('SESSIONID')
    if sessionid and sessionid.strip():
        print("   [green] Logging with session cookie... [/green]")
        api = Client()
        api.delay_range = [1, 3]
        _inject_session(api, sessionid.strip())
        api.dump_settings(SESSION_FILE)
        print("   [green] Logged in via session cookie. [/green]")
        return api

    print("   [red] All login methods failed! Set SESSIONID in the dashboard. [/red]")
    raise LoginRequired("Could not log in. Please set SESSIONID in the dashboard.")