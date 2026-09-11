"""Instagram authentication with self-healing session handling.

Login order (per account):
  1. saved session file
  2. username / password (skipped when the account is flagged as 2FA)
  3. SESSIONID cookie injection

Every successful API call can refresh the stored session so Instagram keeps it
alive. Challenged sessions are backed off exponentially (1h, 4h, 12h, 24h).
"""
from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientError,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    TwoFactorRequired,
)

import config
import helpers as Helper
import notifier
from logger import get_logger

log = get_logger(__name__)

SESSION_FILE = os.path.join(config.BASE_DIR, "session.json")
DELAY_RANGE = [1, 3]

DEVICE_SETTINGS = {
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
}
USER_AGENT = (
    "Instagram 360.0.0.30.108 Android (34/14; 480dpi; 1080x2400; samsung; "
    "SM-A546E; a54x; qcom; en_US; 572810744)"
)


def session_path_for(username: str) -> str:
    """Return the dedicated session file path of a posting account."""
    safe = "".join(ch for ch in (username or "default") if ch.isalnum() or ch in "._-")
    return os.path.join(config.SESSION_DIR, f"session_{safe or 'default'}.json")


def _inject_session(api: Client, sessionid: str, username: str = "", ds_user_id: Optional[str] = None) -> Client:
    """Inject session cookies directly, bypassing the login API."""
    if not ds_user_id:
        decoded_session = urllib.parse.unquote(sessionid)
        match = re.search(r"^(\d+)", decoded_session)
        if match:
            ds_user_id = match.group(1)
        else:
            ds_user_id = sessionid.split("%")[0].split(":")[0]

    ds_user_id = re.sub(r"\D", "", str(ds_user_id))
    api.set_settings(
        {
            "uuids": {
                "phone_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "uuid": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "client_session_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
                "advertising_id": "d4e5f6a7-b8c9-0123-defa-234567890123",
                "android_device_id": "android-1234567890abcdef",
                "request_id": "e5f6a7b8-c9d0-1234-efab-345678901234",
                "tray_session_id": "f6a7b8c9-d0e1-2345-fabc-456789012345",
            },
            "authorization_data": {"ds_user_id": ds_user_id, "sessionid": sessionid},
            "cookies": {"sessionid": sessionid, "ds_user_id": ds_user_id},
            "device_settings": DEVICE_SETTINGS,
            "user_agent": USER_AGENT,
        }
    )
    api.authorization_data = {"ds_user_id": ds_user_id, "sessionid": sessionid}
    api.username = username or config.USERNAME
    return api


def dump_session(api: Client, session_file: str) -> None:
    """Silently persist the current session so it stays warm across restarts."""
    if api is None or not session_file:
        return
    try:
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        api.dump_settings(session_file)
    except (OSError, ClientError) as exc:
        log.debug(f"Could not dump session to {session_file}: {exc}")


def is_session_alive(api: Client) -> bool:
    """Lightweight health check used to keep the session warm."""
    if api is None:
        return False
    try:
        api.account_info()
        return True
    except (LoginRequired, ChallengeRequired, ClientError, OSError) as exc:
        log.warning(f"Session health check failed: {type(exc).__name__}: {exc}")
        return False


def challenge_backoff(challenge_count: int) -> timedelta:
    """Return the backoff duration for a challenged session."""
    hours = getattr(config, "CHALLENGE_BACKOFF_HOURS", [1, 4, 12, 24])
    index = min(max(challenge_count - 1, 0), len(hours) - 1)
    return timedelta(hours=hours[index])


def login_account(
    username: str,
    password: str = "",
    sessionid: str = "",
    session_file: Optional[str] = None,
    is_2fa: bool = False,
) -> Tuple[Optional[Client], str, str]:
    """Log a single account in.

    Returns a tuple of (client_or_None, status, message) where status is one of
    `ok`, `2fa`, `challenged` or `failed`.
    """
    session_file = session_file or session_path_for(username)
    status = "failed"
    message = ""
    detected_2fa = bool(is_2fa)

    # 1) saved session file
    if os.path.exists(session_file):
        api = Client()
        api.delay_range = list(DELAY_RANGE)
        try:
            api.load_settings(session_file)
            if password:
                api.login(username, password)
            api.get_timeline_feed()
            dump_session(api, session_file)
            log.info(f"@{username}: logged in with saved session.")
            return api, "ok", "session file"
        except TwoFactorRequired as exc:
            detected_2fa = True
            message = f"TwoFactorRequired: {exc}"
            log.warning(f"@{username}: session login needs 2FA. {exc}")
        except ChallengeRequired as exc:
            status = "challenged"
            message = f"ChallengeRequired: {exc}"
            log.warning(f"@{username}: session login challenged. {exc}")
        except (LoginRequired, ClientError, OSError, ValueError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: session login failed ({type(exc).__name__}). Trying alternatives...")

    # 2) username + password (skipped for 2FA accounts)
    if password and not detected_2fa:
        api = Client()
        api.delay_range = list(DELAY_RANGE)
        try:
            api.login(username, password)
            api.get_timeline_feed()
            dump_session(api, session_file)
            log.info(f"@{username}: logged in with username/password.")
            return api, "ok", "password"
        except TwoFactorRequired as exc:
            detected_2fa = True
            message = f"TwoFactorRequired: {exc}"
            log.warning(f"@{username}: 2FA enabled, password login will be skipped from now on.")
        except ChallengeRequired as exc:
            status = "challenged"
            message = f"ChallengeRequired: {exc}"
            log.warning(f"@{username}: password login challenged. {exc}")
        except (LoginRequired, RateLimitError, PleaseWaitFewMinutes, ClientError, OSError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: password login failed ({type(exc).__name__}).")

    # 3) SESSIONID cookie (primary method for 2FA accounts)
    if sessionid and sessionid.strip():
        api = Client()
        api.delay_range = list(DELAY_RANGE)
        try:
            _inject_session(api, sessionid.strip(), username=username)
            api.account_info()
            dump_session(api, session_file)
            log.info(f"@{username}: logged in with SESSIONID cookie.")
            return api, "ok", "session cookie"
        except (LoginRequired, ChallengeRequired, ClientError, OSError, ValueError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.error(f"@{username}: session cookie login failed ({type(exc).__name__}).")

    if detected_2fa and status != "challenged":
        status = "2fa"
    log.error(f"@{username}: all login methods failed. {message}")
    return None, status, message or "all login methods failed"


def login() -> Client:
    """Legacy single-account login (kept for backwards compatibility).

    Uses the credentials from the dashboard/config and the original
    `session.json` location.
    """
    Helper.load_all_config()
    username = Helper.get_config("USERNAME") or config.USERNAME
    password = Helper.get_config("PASSWORD") or config.PASSWORD
    sessionid = Helper.get_config("SESSIONID") or ""
    is_2fa = str(Helper.get_config("IS_2FA") or "0") == "1"

    api, status, message = login_account(
        username=username,
        password=password,
        sessionid=sessionid,
        session_file=SESSION_FILE,
        is_2fa=is_2fa,
    )
    if status == "2fa":
        Helper.save_config("IS_2FA", "1")
    if api is not None:
        return api

    notifier.alert_login_failed(username)
    raise LoginRequired(f"Could not log in ({status}): {message}. Set SESSIONID in the dashboard.")


def call_with_retry(
    func: Callable[..., Any],
    *args: Any,
    relogin: Optional[Callable[[], Optional[Client]]] = None,
    session_file: Optional[str] = None,
    client: Optional[Client] = None,
    **kwargs: Any,
) -> Any:
    """Call an Instagram API function with retry, backoff and auto re-login.

    Handles rate limits (429), unauthorized (401 / LoginRequired) and transient
    server/network errors with exponential backoff.
    """
    max_retries = int(getattr(config, "API_MAX_RETRIES", 4))
    base = int(getattr(config, "API_BACKOFF_BASE_SECONDS", 5))
    cap = int(getattr(config, "API_BACKOFF_MAX_SECONDS", 600))
    last_error: Optional[Exception] = None

    import time as _time

    for attempt in range(1, max_retries + 1):
        try:
            result = func(*args, **kwargs)
            if client is not None and session_file:
                dump_session(client, session_file)  # keep the session fresh
            return result
        except (RateLimitError, PleaseWaitFewMinutes) as exc:
            last_error = exc
            wait = int(getattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 900))
            log.warning(f"Rate limited ({type(exc).__name__}). Backing off {wait}s.")
            _time.sleep(min(wait, cap))
        except (LoginRequired, ChallengeRequired) as exc:
            last_error = exc
            log.warning(f"Auth error during API call: {type(exc).__name__}. Attempting re-login.")
            if relogin is not None:
                new_client = relogin()
                if new_client is None:
                    raise
            else:
                raise
        except (ClientError, OSError) as exc:
            last_error = exc
            wait = min(base * (2 ** (attempt - 1)), cap)
            log.warning(f"API error {type(exc).__name__}: {exc}. Retry {attempt}/{max_retries} in {wait}s.")
            _time.sleep(wait)

    if last_error is not None:
        raise last_error
    return None
