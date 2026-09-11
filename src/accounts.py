"""Posting-account registry and per-account runtime (client, timers, status).

Each posting account keeps its own instagrapi `Client`, its own session file
and its own posting timer, so one broken account never blocks the others.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from instagrapi import Client

import auth
import config
import helpers as Helper
import notifier
from db import PostingAccount, Session
from logger import get_logger

log = get_logger(__name__)

LEGACY_ACCOUNT_MARKER = "__legacy__"


class AccountRuntime:
    """In-memory runtime state for one posting account."""

    def __init__(self, username: str, password: str = "", session_id: str = "",
                 session_file: str = "", is_2fa: bool = False, totp_secret: str = "") -> None:
        """Create the runtime holder for a posting account."""
        self.username: str = username
        self.password: str = password or ""
        self.session_id: str = session_id or ""
        self.session_file: str = session_file or auth.session_path_for(username)
        self.is_2fa: bool = bool(is_2fa)
        self.totp_secret: str = totp_secret or ""
        self.client: Optional[Client] = None
        self.login_status: str = "unknown"
        self.last_error: str = ""
        self.next_post_at: datetime = datetime.now()
        self.next_login_attempt_at: datetime = datetime.now()
        self.last_health_check_at: Optional[datetime] = None
        self.last_post_at: Optional[datetime] = None
        self.is_recycling: bool = False
        self.is_uploading: bool = False

    # ------------------------------------------------------------------ #
    def ensure_login(self, force: bool = False) -> Optional[Client]:
        """Return a logged-in client, logging in when needed."""
        if self.client is not None and not force:
            return self.client
        if datetime.now() < self.next_login_attempt_at and not force:
            return None

        # Fetch latest totp_secret from DB if available
        record = get_account(self.username)
        totp_sec = self.totp_secret or (record.totp_secret if record else "")

        client, status, message = auth.login_account(
            username=self.username,
            password=self.password,
            sessionid=self.session_id,
            session_file=self.session_file,
            is_2fa=self.is_2fa,
            totp_secret=totp_sec,
        )
        self.login_status = status
        self.last_error = message
        self.client = client

        if status == "ok":
            self.next_login_attempt_at = datetime.now()
            notifier.reset_dedupe(f"login_failed:{self.username}")
            update_account(self.username, login_status="ok", last_error="")
            return client

        if status == "transient":
            # Rate limit / network blip: credentials and session file are fine.
            # Back off quietly, do not mark the account failed and do not send
            # a login-failure alert.
            wait = int(getattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 900))
            self.login_status = "transient"
            self.next_login_attempt_at = datetime.now() + timedelta(seconds=wait)
            log.warning(
                f"@{self.username}: login deferred after a transient error ({message}). "
                f"Retrying in {wait}s; session file left intact."
            )
            return None

        if status == "2fa":
            self.is_2fa = True
            update_account(self.username, is_2fa=1, login_status="2fa", last_error=message)
        elif status == "challenged":
            self.register_challenge(message)
        else:
            update_account(self.username, login_status="failed", last_error=message)

        notifier.alert_login_failed(self.username)
        self.next_login_attempt_at = datetime.now() + timedelta(
            seconds=int(getattr(config, "LOGIN_FAILURE_RETRY_SECONDS", 300))
        )
        return None

    def register_challenge(self, message: str = "") -> None:
        """Mark the session as challenged and back it off exponentially."""
        record = get_account(self.username)
        count = int((record.challenge_count if record else 0) or 0) + 1
        backoff = auth.challenge_backoff(count)
        until = datetime.now() + backoff
        self.login_status = "challenged"
        self.next_login_attempt_at = until
        update_account(
            self.username,
            login_status="challenged",
            last_error=message,
            challenge_count=count,
            challenged_until=until,
        )
        log.warning(
            f"@{self.username}: session challenged (#{count}). Next attempt after {until:%Y-%m-%d %H:%M:%S}."
        )

    def relogin(self) -> Optional[Client]:
        """Re-establish the client (used as the retry hook for API calls).

        The stored session file is deliberately left in place: `login_account`
        reuses it first and only re-authenticates when Instagram actually says
        the session is dead, so a relogin costs no login request in the common
        case.
        """
        self.client = None
        return self.ensure_login(force=True)

    def health_check(self) -> bool:
        """Keep the session warm; re-login immediately when it is dead."""
        interval = int(getattr(config, "SESSION_HEALTHCHECK_INTERVAL_SECONDS", 1800))
        now = datetime.now()
        if self.last_health_check_at and (now - self.last_health_check_at).total_seconds() < interval:
            return self.client is not None
        self.last_health_check_at = now
        if self.client is None:
            return self.ensure_login() is not None
        # is_session_alive() returns True for transient errors, so a 429 or a
        # dropped connection never triggers a needless re-login.
        if auth.is_session_alive(self.client):
            auth.dump_session(self.client, self.session_file)
            log.debug(f"@{self.username}: session healthy.")
            return True
        log.warning(f"@{self.username}: session is no longer authenticated. Re-authenticating.")
        return self.relogin() is not None

    def to_dict(self) -> Dict[str, object]:
        """Serialise runtime state for the dashboard."""
        return {
            "username": self.username,
            "login_status": self.login_status,
            "last_error": self.last_error,
            "is_2fa": self.is_2fa,
            "next_post_at": self.next_post_at.isoformat() if self.next_post_at else None,
            "last_post_at": self.last_post_at.isoformat() if self.last_post_at else None,
            "is_recycling": self.is_recycling,
            "is_uploading": self.is_uploading,
        }


# --------------------------------------------------------------------------- #
# DB helpers                                                                   #
# --------------------------------------------------------------------------- #
def list_accounts(enabled_only: bool = False) -> List[Dict[str, object]]:
    """Return posting accounts as plain dictionaries."""
    session = Session()
    try:
        query = session.query(PostingAccount)
        if enabled_only:
            query = query.filter(PostingAccount.is_enabled == 1)
        rows = query.order_by(PostingAccount.id).all()
        return [
            {
                "id": row.id,
                "username": row.username,
                "password": row.password or "",
                "session_id": row.session_id or "",
                "session_file": row.session_file or auth.session_path_for(row.username),
                "is_enabled": int(row.is_enabled or 0),
                "is_2fa": int(row.is_2fa or 0),
                "has_totp_secret": bool(row.totp_secret),
                "login_status": row.login_status or "unknown",
                "last_error": row.last_error or "",
                "last_post_at": row.last_post_at.isoformat() if row.last_post_at else None,
                "challenged_until": row.challenged_until.isoformat() if row.challenged_until else None,
            }
            for row in rows
        ]
    finally:
        session.close()


def enabled_usernames() -> List[str]:
    """Return the usernames of all enabled posting accounts."""
    return [account["username"] for account in list_accounts(enabled_only=True)]


def get_account(username: str) -> Optional[PostingAccount]:
    """Return a detached snapshot of one posting account row."""
    session = Session()
    try:
        row = session.query(PostingAccount).filter_by(username=username).first()
        if row is None:
            return None
        session.expunge(row)
        return row
    finally:
        session.close()


def add_account(username: str, password: str = "", session_id: str = "",
                totp_secret: str = "", is_enabled: int = 1, is_2fa: int = 0) -> Dict[str, object]:
    """Create or update a posting account."""
    username = (username or "").strip().lstrip("@")
    if not username:
        raise ValueError("username is required")

    session = Session()
    try:
        row = session.query(PostingAccount).filter_by(username=username).first()
        now = datetime.now()
        if row is None:
            row = PostingAccount(
                username=username,
                password=password or "",
                session_id=session_id or "",
                totp_secret=totp_secret or "",
                session_file=auth.session_path_for(username),
                is_enabled=int(is_enabled),
                is_2fa=int(is_2fa),
                login_status="unknown",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            if password:
                row.password = password
            if session_id:
                row.session_id = session_id
            if totp_secret:
                row.totp_secret = totp_secret
            row.is_enabled = int(is_enabled)
            row.is_2fa = int(is_2fa)
            row.session_file = row.session_file or auth.session_path_for(username)
            row.updated_at = now
        session.commit()
        log.info(f"Posting account @{username} saved (enabled={is_enabled}).")
        return {"username": username, "is_enabled": int(is_enabled), "is_2fa": int(is_2fa)}
    finally:
        session.close()


def update_account(username: str, **fields: object) -> None:
    """Update arbitrary columns of a posting account."""
    if not fields:
        return
    session = Session()
    try:
        row = session.query(PostingAccount).filter_by(username=username).first()
        if row is None:
            return
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        row.updated_at = datetime.now()
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive, never crash the loop
        session.rollback()
        log.warning(f"Could not update posting account @{username}: {exc}")
    finally:
        session.close()


def remove_account(username: str) -> bool:
    """Delete a posting account. Assigned reels are released back to the pool."""
    from db import Reel

    session = Session()
    try:
        row = session.query(PostingAccount).filter_by(username=username).first()
        if row is None:
            return False
        session.delete(row)
        session.query(Reel).filter_by(assigned_to=username).update({"assigned_to": None})
        session.commit()
        log.info(f"Posting account @{username} removed.")
        return True
    finally:
        session.close()


def set_enabled(username: str, enabled: bool) -> None:
    """Enable or disable a posting account."""
    update_account(username, is_enabled=1 if enabled else 0)


def mark_posted(username: str) -> None:
    """Record the time of the latest successful post for an account."""
    update_account(username, last_post_at=datetime.now())


def bootstrap_from_legacy_config() -> None:
    """Seed `posting_accounts` from the single-account dashboard settings.

    This keeps existing installs working: the account previously configured via
    USERNAME/PASSWORD/SESSIONID becomes the first posting account.
    """
    if list_accounts():
        return
    Helper.load_all_config()
    username = (Helper.get_config("USERNAME") or config.USERNAME or "").strip()
    if not username or username == "your_username":
        return
    password = Helper.get_config("PASSWORD") or config.PASSWORD or ""
    session_id = Helper.get_config("SESSIONID") or ""
    is_2fa = 1 if str(Helper.get_config("IS_2FA") or "0") == "1" else 0
    add_account(username, password=password, session_id=session_id, is_enabled=1, is_2fa=is_2fa)
    log.info(f"Bootstrapped posting account @{username} from legacy configuration.")


class AccountPool:
    """Keeps `AccountRuntime` objects in sync with the DB."""

    def __init__(self) -> None:
        """Initialise an empty pool."""
        self.runtimes: Dict[str, AccountRuntime] = {}

    def refresh(self) -> List[AccountRuntime]:
        """Reload accounts from the DB and return the enabled runtimes."""
        records = list_accounts(enabled_only=True)
        enabled = {record["username"] for record in records}

        for username in list(self.runtimes):
            if username not in enabled:
                self.runtimes.pop(username, None)

        for record in records:
            username = str(record["username"])
            runtime = self.runtimes.get(username)
            if runtime is None:
                runtime = AccountRuntime(
                    username=username,
                    password=str(record["password"]),
                    session_id=str(record["session_id"]),
                    session_file=str(record["session_file"]),
                    is_2fa=bool(record["is_2fa"]),
                )
                self.runtimes[username] = runtime
            else:
                runtime.password = str(record["password"])
                runtime.session_id = str(record["session_id"])
                runtime.is_2fa = bool(record["is_2fa"])
        return list(self.runtimes.values())

    def active(self) -> List[AccountRuntime]:
        """Return the currently loaded runtimes."""
        return list(self.runtimes.values())

    def any_logged_in(self) -> Optional[AccountRuntime]:
        """Return any account with a live client (used for scraping)."""
        for runtime in self.runtimes.values():
            if runtime.client is not None:
                return runtime
        return None
