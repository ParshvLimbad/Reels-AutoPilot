"""Instagram authentication with self-healing session handling.

Login order (per account):
  1. saved session file
  2. username / password (skipped when the account is flagged as 2FA)
  3. SESSIONID cookie injection

Every successful API call can refresh the stored session so Instagram keeps it
alive. Challenged sessions are backed off exponentially (1h, 4h, 12h, 24h).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

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


# Namespace used to derive stable per-account device identifiers. Changing this
# value re-rolls every device fingerprint, so leave it alone once deployed.
DEVICE_NAMESPACE = uuid.UUID("6f1c7f7e-8b6a-4d2b-9a5f-2c1f3f0b7d41")

# Errors that must NEVER be treated as "the session is dead". They are
# transient (rate limits, network blips, 5xx) and the stored session file has
# to survive them untouched.
TRANSIENT_ERRORS = (RateLimitError, PleaseWaitFewMinutes, OSError)


def _stable_uuid(username: str, purpose: str) -> str:
    """Return a deterministic, per-account UUID for a given device field.

    Deterministic means the same account always presents the same device to
    Instagram (which is what a real phone does), while two different accounts
    never share a device identifier.
    """
    seed = f"{(username or 'default').strip().lower()}::{purpose}"
    return str(uuid.uuid5(DEVICE_NAMESPACE, seed))


def device_uuids_for(username: str) -> Dict[str, str]:
    """Build the per-account `uuids` block used by instagrapi."""
    name = (username or "default").strip().lower()
    android_id = hashlib.sha256(f"{name}::android_device_id".encode("utf-8")).hexdigest()[:16]
    return {
        "phone_id": _stable_uuid(name, "phone_id"),
        "uuid": _stable_uuid(name, "uuid"),
        "client_session_id": _stable_uuid(name, "client_session_id"),
        "advertising_id": _stable_uuid(name, "advertising_id"),
        "android_device_id": f"android-{android_id}",
        "request_id": _stable_uuid(name, "request_id"),
        "tray_session_id": _stable_uuid(name, "tray_session_id"),
    }


def _apply_unique_device(api: Client, username: str) -> Client:
    """Give `api` a per-account device fingerprint and sane delays.

    Instagram revokes sessions when several accounts log in from byte-identical
    low-level device IDs, so each account gets its own stable set.
    """
    if api is None:
        return api
    api.delay_range = list(DELAY_RANGE)
    try:
        api.set_device(dict(DEVICE_SETTINGS))
        api.set_user_agent(USER_AGENT)
        api.set_uuids(device_uuids_for(username))
    except Exception as exc:  # pragma: no cover - older instagrapi fallbacks
        log.debug(f"@{username}: falling back to set_settings for device identity ({exc}).")
        try:
            settings = api.get_settings() or {}
        except Exception:
            settings = {}
        settings.update(
            {
                "uuids": device_uuids_for(username),
                "device_settings": dict(DEVICE_SETTINGS),
                "user_agent": USER_AGENT,
            }
        )
        api.set_settings(settings)
    return api


def _restore_device_identity(api: Client, username: str) -> None:
    """Re-assert the per-account device identity after `load_settings()`.

    Sessions saved before this fix contain the old shared UUIDs; rewriting them
    on load migrates those files to a unique device without a new login.
    """
    if api is None:
        return
    expected = device_uuids_for(username)
    try:
        current = (api.get_settings() or {}).get("uuids") or {}
    except Exception:
        current = {}
    if current == expected:
        return
    log.info(f"@{username}: migrating saved session to a unique device fingerprint.")
    try:
        api.set_uuids(expected)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug(f"@{username}: could not re-apply device uuids ({exc}).")


def session_path_for(username: str) -> str:
    """Return the dedicated session file path of a posting account."""
    safe = "".join(ch for ch in (username or "default") if ch.isalnum() or ch in "._-")
    return os.path.join(config.SESSION_DIR, f"session_{safe or 'default'}.json")


def _inject_session(api: Client, sessionid: str, username: str = "", ds_user_id: Optional[str] = None) -> Client:
    """Inject session cookies directly, bypassing the login API."""
    username = username or config.USERNAME
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
            # Per-account device identity: two accounts must never share these.
            "uuids": device_uuids_for(username),
            "authorization_data": {"ds_user_id": ds_user_id, "sessionid": sessionid},
            "cookies": {"sessionid": sessionid, "ds_user_id": ds_user_id},
            "device_settings": dict(DEVICE_SETTINGS),
            "user_agent": USER_AGENT,
        }
    )
    api.authorization_data = {"ds_user_id": ds_user_id, "sessionid": sessionid}
    api.username = username
    return api


def dump_session(api: Client, session_file: str) -> None:
    """Atomically persist the current session so it stays warm across restarts.

    The settings are serialised first, then written to a temp file in the same
    directory and moved into place with `os.replace()`. A crash, a full disk or
    a serialisation failure therefore leaves the previously stored (working)
    session file completely untouched instead of truncating it.
    """
    if api is None or not session_file:
        return

    try:
        settings = api.get_settings()
    except Exception as exc:
        log.debug(f"Could not read settings for {session_file}: {exc}")
        return

    # Refuse to persist an empty / half-built session over a good file.
    if not settings or not isinstance(settings, dict):
        log.debug(f"Refusing to write empty session settings to {session_file}.")
        return
    has_auth = bool(
        (settings.get("authorization_data") or {}).get("sessionid")
        or (settings.get("cookies") or {}).get("sessionid")
    )
    if not has_auth and os.path.exists(session_file):
        log.debug(f"Refusing to overwrite {session_file} with an unauthenticated session.")
        return

    directory = os.path.dirname(session_file) or "."
    tmp_path = ""
    try:
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(settings, default=str)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".session_", suffix=".tmp", delete=False
        ) as handle:
            tmp_path = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, session_file)
        tmp_path = ""
    except (OSError, TypeError, ValueError, ClientError) as exc:
        log.debug(f"Could not dump session to {session_file}: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def quarantine_session(session_file: str) -> None:
    """Move a definitively dead session file aside (never on transient errors).

    Keeping a copy makes post-mortems possible and guarantees we never silently
    destroy a file that might still have been usable.
    """
    if not session_file or not os.path.exists(session_file):
        return
    try:
        backup = f"{session_file}.invalid"
        shutil.move(session_file, backup)
        log.info(f"Session file {os.path.basename(session_file)} was invalid; moved to {os.path.basename(backup)}.")
    except OSError as exc:
        log.debug(f"Could not quarantine {session_file}: {exc}")


def is_session_alive(api: Client) -> bool:
    """Lightweight health check used to keep the session warm.

    Only a real authentication failure counts as "dead". Rate limits and
    network errors are transient, so the session is assumed to still be good
    and the caller must not throw the session file away because of them.
    """
    if api is None:
        return False
    try:
        api.account_info()
        return True
    except (LoginRequired, ChallengeRequired) as exc:
        log.warning(f"Session health check failed: {type(exc).__name__}: {exc}")
        return False
    except TRANSIENT_ERRORS as exc:
        log.warning(f"Session health check inconclusive (transient {type(exc).__name__}: {exc}). Keeping session.")
        return True
    except ClientError as exc:
        log.warning(f"Session health check inconclusive ({type(exc).__name__}: {exc}). Keeping session.")
        return True


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

    Login order: saved session file -> username/password -> SESSIONID cookie.
    The saved session is never revalidated with `api.login()`, because calling
    the login endpoint on a live session is what revokes it.

    Returns a tuple of (client_or_None, status, message) where status is one of
    `ok`, `2fa`, `challenged`, `transient` or `failed`. `transient` means "the
    credentials are probably fine, we were just rate limited / offline" and the
    caller should retry soon without touching stored credentials.
    """
    session_file = session_file or session_path_for(username)
    status = "failed"
    message = ""
    detected_2fa = bool(is_2fa)

    # 1) saved session file -- reused WITHOUT ever calling api.login().
    #
    # Calling /api/v1/accounts/login/ while a session is already valid is what
    # invalidates the active token, so the saved session is validated with a
    # cheap read request instead.
    if os.path.exists(session_file):
        api = Client()
        _apply_unique_device(api, username)
        try:
            api.load_settings(session_file)
            _restore_device_identity(api, username)
            # Health check only: a read request, never a new login request.
            api.get_timeline_feed()
            dump_session(api, session_file)
            log.info(f"@{username}: logged in with saved session.")
            return api, "ok", "session file"
        except ChallengeRequired as exc:
            # Real auth failure -> fall through to re-authentication.
            status = "challenged"
            message = f"ChallengeRequired: {exc}"
            log.warning(f"@{username}: saved session challenged ({exc}). Attempting re-authentication...")
        except LoginRequired as exc:
            message = f"LoginRequired: {exc}"
            log.warning(f"@{username}: saved session expired ({exc}). Attempting re-authentication...")
            quarantine_session(session_file)
        except TRANSIENT_ERRORS as exc:
            # 429 / network blip: the session is almost certainly still fine.
            # Preserve the file untouched and retry on the next cycle rather
            # than burning a login request.
            message = f"{type(exc).__name__}: {exc}"
            log.warning(
                f"@{username}: transient error while reusing saved session ({type(exc).__name__}: {exc}). "
                "Session file preserved, will retry later."
            )
            return None, "transient", message
        except (ValueError, OSError) as exc:
            # Unreadable / corrupt JSON on disk -- not an auth problem.
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: error loading session settings ({exc}).")
            quarantine_session(session_file)
        except ClientError as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning(
                f"@{username}: unclear API error while reusing saved session ({type(exc).__name__}: {exc}). "
                "Session file preserved, will retry later."
            )
            return None, "transient", message

    # 2) username + password -- ONLY reached when the saved session was
    #    missing, expired or challenged. Skipped for 2FA accounts.
    if password and not detected_2fa:
        api = Client()
        _apply_unique_device(api, username)
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
        except (RateLimitError, PleaseWaitFewMinutes) as exc:
            # Do not keep hammering the login endpoint while rate limited.
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: password login rate limited ({type(exc).__name__}). Backing off.")
            return None, "transient", message
        except (LoginRequired, ClientError, OSError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: password login failed ({type(exc).__name__}).")

    # 3) SESSIONID cookie (primary method for 2FA accounts)
    if sessionid and sessionid.strip():
        api = Client()
        _apply_unique_device(api, username)
        try:
            _inject_session(api, sessionid.strip(), username=username)
            api.account_info()
            dump_session(api, session_file)
            log.info(f"@{username}: logged in with SESSIONID cookie.")
            return api, "ok", "session cookie"
        except (RateLimitError, PleaseWaitFewMinutes) as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning(f"@{username}: session cookie login rate limited ({type(exc).__name__}). Backing off.")
            return None, "transient", message
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
