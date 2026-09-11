#!/usr/bin/env python3
"""Flask dashboard and JSON API for Reels-AutoPilot."""
from __future__ import annotations

import os
import subprocess
import sys

# Ensure src directory is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from datetime import datetime
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import desc
from werkzeug.utils import secure_filename

import accounts as AccountManager
import config
import diskspace
import distributor
import helpers
import logger
import reels as ReelsScraper
import statefile
from db import Config, Reel, Session

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SERVICE_COMMAND_TIMEOUT_SECONDS = 60
app = Flask(__name__)
log = logger.get_logger(__name__)


@app.route("/")
def index():
    """Serve the single page dashboard."""
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/api/config", methods=["GET"])
def get_config_api():
    """Return the merged configuration (DB values over config.py defaults)."""
    helpers.load_all_config()
    session = Session()
    try:
        configs = session.query(Config).all()
        result = {item.key: item.value for item in configs}
    finally:
        session.close()

    defaults = {
        "IS_REMOVE_FILES": config.IS_REMOVE_FILES,
        "REMOVE_FILE_AFTER_MINS": str(config.REMOVE_FILE_AFTER_MINS),
        "IS_ENABLED_REELS_SCRAPER": config.IS_ENABLED_REELS_SCRAPER,
        "IS_ENABLED_AUTO_POSTER": config.IS_ENABLED_AUTO_POSTER,
        "IS_POST_TO_STORY": config.IS_POST_TO_STORY,
        "FETCH_LIMIT": str(config.FETCH_LIMIT),
        "POSTING_INTERVAL_IN_MIN": str(config.POSTING_INTERVAL_IN_MIN),
        "SCRAPER_INTERVAL_IN_MIN": str(config.SCRAPER_INTERVAL_IN_MIN),
        "USERNAME": config.USERNAME,
        "PASSWORD": config.PASSWORD,
        "ACCOUNTS": ",".join(config.ACCOUNTS) if isinstance(config.ACCOUNTS, list) else str(config.ACCOUNTS),
        "HASHTAGS": config.HASHTAGS,
        "HASTAGS": getattr(config, "HASTAGS", config.HASHTAGS),
        "CUSTOM_CAPTION": getattr(config, "CUSTOM_CAPTION", ""),
        "REEL_COVER_PATH": getattr(config, "REEL_COVER_PATH", ""),
        "LIKE_AND_VIEW_COUNTS_DISABLED": config.LIKE_AND_VIEW_COUNTS_DISABLED,
        "DISABLE_COMMENTS": config.DISABLE_COMMENTS,
        "IS_ENABLED_YOUTUBE_SCRAPING": config.IS_ENABLED_YOUTUBE_SCRAPING,
        "YOUTUBE_API_KEY": config.YOUTUBE_API_KEY,
        "CHANNEL_LINKS": ",".join(config.CHANNEL_LINKS)
        if isinstance(config.CHANNEL_LINKS, list)
        else str(config.CHANNEL_LINKS),
        "DISCORD_WEBHOOK_URL": getattr(config, "DISCORD_WEBHOOK_URL", ""),
    }
    for key, default_val in defaults.items():
        if key not in result:
            result[key] = str(default_val) if default_val is not None else ""

    if "PASSWORD" in result:
        result["PASSWORD"] = "********"
    return jsonify(result)


@app.route("/api/config", methods=["POST"])
def save_config_api():
    """Persist configuration changes coming from the dashboard."""
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    for key, value in data.items():
        if key == "PASSWORD" and value == "********":
            continue
        helpers.save_config(key, str(value))

    helpers.load_all_config()
    return jsonify({"status": "ok", "message": "Configuration saved"})


@app.route("/api/upload_cover", methods=["POST"])
def upload_cover_api():
    """Upload a custom reel cover image and store its path."""
    try:
        if "cover_image" not in request.files:
            return jsonify({"status": "error", "message": "No file part"}), 400
        file = request.files["cover_image"]
        if file.filename == "":
            return jsonify({"status": "error", "message": "No selected file"}), 400

        filename = secure_filename(file.filename)
        save_path = os.path.join(config.BASE_DIR, filename)
        file.save(save_path)

        helpers.save_config("REEL_COVER_PATH", save_path)
        helpers.load_all_config()
        return jsonify(
            {"status": "ok", "message": "Cover image uploaded and path updated", "path": save_path}
        )
    except (OSError, ValueError) as exc:
        log.error(f"Cover upload failed: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 500


def _python_executable() -> str:
    """Return the interpreter to use for helper scripts."""
    venv_python = os.path.join(config.BASE_DIR, "venv", "bin", "python3")
    return venv_python if os.path.exists(venv_python) else sys.executable


def _restart_service(service: str) -> subprocess.CompletedProcess:
    """Restart a systemd service from the dashboard."""
    return subprocess.run(
        ["sudo", "-n", "systemctl", "restart", service],
        capture_output=True,
        text=True,
        timeout=SERVICE_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


@app.route("/api/purge_rescrape", methods=["POST"])
def purge_and_rescrape():
    """Purge unposted reels and restart the autopilot for a fresh scrape."""
    try:
        purger_path = os.path.join(config.BASE_DIR, "src", "purger.py")
        subprocess.run(
            [_python_executable(), purger_path],
            capture_output=True,
            text=True,
            timeout=SERVICE_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        _restart_service(getattr(config, "AUTOPILOT_SERVICE", "reels-autopilot"))
        return jsonify(
            {"status": "ok", "message": "Purged unposted reels and triggered a fresh scrape!"}
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"status": "error", "message": f"Failed to purge: {exc}"}), 500


@app.route("/api/restart", methods=["POST"])
def restart_api():
    """Force restart the autopilot (or web) service."""
    payload: Dict[str, Any] = request.json or {}
    service = str(payload.get("service") or getattr(config, "AUTOPILOT_SERVICE", "reels-autopilot"))
    allowed = {
        getattr(config, "AUTOPILOT_SERVICE", "reels-autopilot"),
        getattr(config, "WEB_SERVICE", "reels-web"),
        "reels-watchdog",
    }
    if service not in allowed:
        return jsonify({"status": "error", "message": "Unknown service"}), 400
    try:
        result = _restart_service(service)
        if result.returncode == 0:
            return jsonify({"status": "ok", "message": f"{service} restarted"})
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (result.stderr or "restart failed").strip()[:300],
                }
            ),
            500,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Return global counters plus the most recent reels."""
    session = Session()
    try:
        total = session.query(Reel).count()
        posted = session.query(Reel).filter_by(is_posted=True).count()
        pending = session.query(Reel).filter_by(is_posted=False).count()

        recent = session.query(Reel).order_by(desc(Reel.id)).limit(50).all()
        recent_reels = [
            {
                "id": row.id,
                "code": row.code,
                "account": row.account or "",
                "is_posted": row.is_posted,
                "assigned_to": row.assigned_to or "",
                "posted_by": row.posted_by or "",
                "swap_phase": int(row.swap_phase or 0),
                "posted_at": row.posted_at.strftime("%b %d, %H:%M") if row.posted_at else None,
            }
            for row in recent
        ]
    finally:
        session.close()

    state = statefile.load_state()
    return jsonify(
        {
            "total": total,
            "posted": posted,
            "pending": pending,
            "recent": recent_reels,
            "status": state.get("status", "unknown"),
            "network": state.get("network", "unknown"),
            "last_post_at": state.get("last_post_at"),
            "disk": diskspace.usage(),
        }
    )


@app.route("/api/accounts", methods=["GET"])
def list_accounts_api():
    """Return posting accounts with their per-account statistics."""
    records = AccountManager.list_accounts()
    counts = distributor.counts_by_account([record["username"] for record in records])
    state_accounts = (statefile.load_state().get("accounts") or {})
    payload = []
    for record in records:
        username = str(record["username"])
        runtime_state = state_accounts.get(username, {})
        payload.append(
            {
                "username": username,
                "is_enabled": record["is_enabled"],
                "is_2fa": record["is_2fa"],
                "has_password": bool(record["password"]),
                "has_session_id": bool(record["session_id"]),
                "login_status": runtime_state.get("login_status") or record["login_status"],
                "last_error": record["last_error"],
                "last_post_at": record["last_post_at"],
                "next_post_at": runtime_state.get("next_post_at"),
                "is_recycling": runtime_state.get("is_recycling", False),
                "challenged_until": record["challenged_until"],
                "stats": counts.get(username, {"assigned": 0, "posted": 0, "pending": 0}),
            }
        )
    return jsonify({"accounts": payload})


@app.route("/api/accounts", methods=["POST"])
def add_account_api():
    """Create or update a posting account."""
    data: Dict[str, Any] = request.json or {}
    username = str(data.get("username") or "").strip()
    if not username:
        return jsonify({"status": "error", "message": "username is required"}), 400
    try:
        AccountManager.add_account(
            username=username,
            password=str(data.get("password") or ""),
            session_id=str(data.get("session_id") or ""),
            is_enabled=1 if str(data.get("is_enabled", "1")) in ("1", "True", "true") else 0,
            is_2fa=1 if str(data.get("is_2fa", "0")) in ("1", "True", "true") else 0,
        )
        return jsonify({"status": "ok", "message": f"Account @{username} saved"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/accounts/<username>", methods=["PATCH"])
def patch_account_api(username: str):
    """Toggle a posting account on or off."""
    data: Dict[str, Any] = request.json or {}
    if "is_enabled" in data:
        AccountManager.set_enabled(username, str(data["is_enabled"]) in ("1", "True", "true"))
    if "is_2fa" in data:
        AccountManager.update_account(
            username, is_2fa=1 if str(data["is_2fa"]) in ("1", "True", "true") else 0
        )
    return jsonify({"status": "ok", "message": f"Account @{username} updated"})


@app.route("/api/accounts/<username>", methods=["DELETE"])
def delete_account_api(username: str):
    """Remove a posting account and release its reels."""
    removed = AccountManager.remove_account(username)
    if not removed:
        return jsonify({"status": "error", "message": "Account not found"}), 404
    return jsonify({"status": "ok", "message": f"Account @{username} removed"})


@app.route("/api/accounts/<username>/verify_2fa", methods=["POST"])
def verify_2fa_api(username: str):
    """Submit a 6-digit 2FA verification code for an account."""
    import auth
    data: Dict[str, Any] = request.json or {}
    code = str(data.get("code") or "").strip()
    if not code:
        return jsonify({"status": "error", "message": "2FA code is required"}), 400

    account = AccountManager.get_account(username)
    if not account:
        return jsonify({"status": "error", "message": "Account not found"}), 404

    password = account.password or getattr(config, "PASSWORD", "")
    api, status, message = auth.login_with_2fa_code(username, password, code)
    if status == "ok" and api is not None:
        AccountManager.update_account(username, login_status="ok", is_2fa=1, last_error="")
        state_accounts = statefile.load_state().get("accounts") or {}
        if username in state_accounts:
            state_accounts[username]["login_status"] = "ok"
            state_accounts[username]["last_error"] = ""
        return jsonify({"status": "ok", "message": f"@{username} 2FA verification successful!"})
    else:
        AccountManager.update_account(username, login_status="2fa", last_error=message)
        return jsonify({"status": "error", "message": message}), 400


@app.route("/api/scrape_status", methods=["GET"])
def scrape_status_api():
    """Return per source-account scraping statistics."""
    return jsonify({"sources": ReelsScraper.get_scrape_status()})


@app.route("/api/logs", methods=["GET"])
def logs_api():
    """Return the most recent log lines."""
    try:
        lines = int(request.args.get("lines", getattr(config, "DASHBOARD_LOG_LINES", 100)))
    except (TypeError, ValueError):
        lines = int(getattr(config, "DASHBOARD_LOG_LINES", 100))
    return jsonify({"lines": logger.read_recent_logs(lines)})


@app.route("/api/disk", methods=["GET"])
def disk_api():
    """Return disk usage details."""
    usage = diskspace.usage()
    usage["downloads_mb"] = diskspace.downloads_size_mb()
    usage["threshold_mb"] = float(getattr(config, "MIN_FREE_DISK_MB", 500))
    return jsonify(usage)


@app.route("/api/health", methods=["GET"])
def health_api():
    """Health endpoint used by the dashboard and the watchdog."""
    state = statefile.load_state()
    session = Session()
    try:
        pending = session.query(Reel).filter_by(is_posted=False).count()
        posted = session.query(Reel).filter_by(is_posted=True).count()
        last_reel = (
            session.query(Reel)
            .filter(Reel.is_posted == True)  # noqa: E712
            .filter(Reel.posted_at != None)  # noqa: E711
            .order_by(desc(Reel.posted_at))
            .first()
        )
        last_post_at = last_reel.posted_at.isoformat() if last_reel and last_reel.posted_at else None
    finally:
        session.close()

    accounts_payload = AccountManager.list_accounts()
    return jsonify(
        {
            "status": state.get("status", "unknown"),
            "network": state.get("network", "unknown"),
            "pid": state.get("pid"),
            "state_saved_at": state.get("saved_at"),
            "server_time": datetime.now().isoformat(),
            "pending": pending,
            "posted": posted,
            "last_post_at": last_post_at or state.get("last_post_at"),
            "next_scrape_at": state.get("next_reels_scraper_run_at"),
            "disk": diskspace.usage(),
            "accounts": [
                {
                    "username": account["username"],
                    "is_enabled": account["is_enabled"],
                    "login_status": account["login_status"],
                    "last_post_at": account["last_post_at"],
                }
                for account in accounts_payload
            ],
            "pending_uploads": statefile.all_pending_uploads(),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"[Dashboard] Running at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
