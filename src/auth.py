"""Persistent Instagram sessions with bounded, worker-owned recovery."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Optional

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

# Errors that must NEVER be treated as a dead session. They are transient,
# so the stored session file has to survive them untouched.
TRANSIENT_ERRORS = (RateLimitError, PleaseWaitFewMinutes, OSError)


def generate_totp_code(secret_b32: str) -> str:
    """Generate a 6-digit TOTP verification code (RFC 6238) from a base32 secret."""
    import base64
    import hashlib
    import hmac
    import struct
    import time

    clean = str(secret_b32 or "").upper().replace(" ", "").replace("-", "")
    if not clean:
        raise ValueError("TOTP secret is empty")
    missing_padding = len(clean) % 8
    if missing_padding:
        clean += "=" * (8 - missing_padding)
    key = base64.b32decode(clean)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def _configure_client(api: Client) -> None:
    """Set transport settings only; session files own Instagram identity."""
    api.delay_range = list(DELAY_RANGE)
    api.request_timeout = 30


def session_path_for(username: str) -> str:
    """Return the dedicated session file path of a posting account."""
    safe = "".join(ch for ch in (username or "default") if ch.isalnum() or ch in "._-")
    return os.path.join(config.SESSION_DIR, f"session_{safe or 'default'}.json")


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
        raise
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


def login_account(username: str, password: str = "", sessionid: str = "",
                  session_file: Optional[str] = None, is_2fa: bool = False,
                  totp_secret: str = "", verification_code: str = ""):
    """Reuse saved identity; at most one credential attempt per invocation.

    Challenges stop recovery. Browser cookies are not replayed automatically.
    Only the worker calls this function (dashboard submits a command).
    """
    session_file = session_file or session_path_for(username)
    api = Client()
    _configure_client(api)
    if os.path.exists(session_file):
        try:
            # Update only the app-version fields. This preserves the saved
            # account UUIDs, device settings, cookies and proxy.
            api.load_settings(session_file, override_app_version=True)
            _configure_client(api)
        except (ValueError, OSError):
            return None, "failed", "Saved session cannot be read; restore its backup."
        try:
            api.account_info()
            dump_session(api, session_file)
            return api, "ok", "saved session"
        except ChallengeRequired:
            if not verification_code:
                return None, "challenged", "Complete Instagram verification, then request Resume."
        except LoginRequired:
            # The loaded settings already use this library's supported app profile.
            settings = api.get_settings()
            settings["authorization_data"] = {}
            settings["cookies"] = {}
            api.set_settings(settings)
            _configure_client(api)
        except Exception as exc:
            return None, "transient", type(exc).__name__
    else:
        # Library defaults generate a new identity once. Persist even before a
        # failed login so subsequent attempts do not present another device.
        dump_session(api, session_file)

    if not password:
        return None, "failed", "Password required for session recovery."
    if is_2fa and not (totp_secret or verification_code):
        return None, "2fa", "Submit a current code or configure the TOTP key."
    try:
        code = verification_code or (generate_totp_code(totp_secret) if totp_secret else "")
        api.login(username, password, verification_code=code)
        api.account_info()
        dump_session(api, session_file)
        return api, "ok", "authenticated"
    except TwoFactorRequired:
        return None, "2fa", "A current verification code is required."
    except ChallengeRequired:
        return None, "challenged", "Complete verification in Instagram, then request Resume."
    except (RateLimitError, PleaseWaitFewMinutes, OSError) as exc:
        return None, "transient", type(exc).__name__
    except Exception as exc:
        # No cookie fallback or immediate repeated login after a failed attempt.
        detail = type(exc).__name__
        payload = getattr(api, "last_json", {}) or {}
        error_type = payload.get("error_type", "")
        if isinstance(error_type, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", error_type):
            detail += ": " + error_type
        return None, "failed", detail


def login_with_2fa_code(username: str, password: str, code: str):
    return login_account(username, password, is_2fa=True, verification_code=code)


def login() -> Client:
    """Legacy single-account login (kept for backwards compatibility).

    Uses the credentials from the dashboard/config and the original
    `session.json` location.
    """
    Helper.load_all_config()
    username = Helper.get_config("USERNAME") or config.USERNAME
    password = Helper.get_config("PASSWORD") or config.PASSWORD
    is_2fa = str(Helper.get_config("IS_2FA") or "0") == "1"

    api, status, message = login_account(
        username=username,
        password=password,
        session_file=SESSION_FILE,
        is_2fa=is_2fa,
    )
    if status == "2fa":
        Helper.save_config("IS_2FA", "1")
    if api is not None:
        return api

    notifier.alert_login_failed(username)
    raise LoginRequired(f"Could not log in ({status}): {message}. complete Instagram verification, then use Resume in the dashboard.")
