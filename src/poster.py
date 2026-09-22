"""Uploads one reel per call, per posting account.

Keeps the original single-account entry point `main(api)` working while adding
multi-account posting, duplicate protection, crash recovery, upload
verification and recycling/swap integration.
"""
from __future__ import annotations

import builtins
import logging
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests as req  # noqa: F401 - kept for backwards compatibility
from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired
from instagrapi.types import StoryHashtag, StoryLink, StoryMedia, StoryMention
from moviepy.editor import VideoFileClip
from sqlalchemy import desc

import config
import diskspace
import distributor
import delivery
import helpers as Helper
import notifier
import statefile
from db import Reel, Session
from logger import get_logger

logging.getLogger("moviepy").setLevel(logging.ERROR)
log = get_logger(__name__)

from helpers import print as log_print  # noqa: E402 - preserved public helper

STORY_MAX_DURATION_SECONDS = 15
UPLOAD_VERIFY_MEDIA_COUNT = 1

# Set while an upload is in flight so shutdown can wait for it
is_uploading: bool = False


def console_print(message: str) -> None:
    """Print to both console and the rotating log file."""
    builtins.print(message)
    log.info(str(message))


def notify_discord(reel_code: str, account: str, caption: str = "", posted_by: str = "") -> None:
    """Send a Discord notification for a successful post."""
    notifier.notify_posted(reel_code, account or "unknown", posted_by or account or "unknown", caption)


def trim_video(file_path: str, output_path: str, max_duration: int = STORY_MAX_DURATION_SECONDS) -> str:
    """Trim a video down to `max_duration` seconds for story posting."""
    clip = VideoFileClip(file_path)
    try:
        trimmed_clip = clip.subclip(0, max_duration)
        trimmed_clip.write_videofile(output_path)
    finally:
        clip.close()
    return output_path


def get_video_duration(file_path: str) -> float:
    """Return a video's duration in seconds."""
    clip = VideoFileClip(file_path)
    try:
        return float(clip.duration)
    finally:
        clip.close()



def get_reel(assigned_to: Optional[str] = None, exclude_codes=(), rotation_after=None) -> Optional[Reel]:
    """Return the next unposted reel with a valid file, ensuring source rotation.

    When `assigned_to` is given, only reels assigned to that posting account
    are considered.
    """
    session = Session()
    try:
        query = session.query(Reel).filter(Reel.is_posted == False)  # noqa: E712
        if assigned_to:
            query = query.filter(Reel.assigned_to == assigned_to)
        unposted_reels = query.order_by(Reel.id).all()

        valid_by_account: Dict[str, Reel] = {}
        for reel in unposted_reels:
            if reel.code in exclude_codes:
                continue
            if not reel.file_path or not os.path.exists(reel.file_path):
                continue
            if assigned_to and delivery.blocked(assigned_to, reel.code):
                continue
            source = reel.account or "unknown"
            valid_by_account.setdefault(source, reel)

        pool = valid_by_account
        if not pool:
            return None

        from accounts import get_account
        record = get_account(assigned_to) if assigned_to else None
        last_source = rotation_after if rotation_after is not None else (record.last_source if record else None)

        raw_sources = config.ACCOUNTS
        sources = [v.strip() for v in raw_sources.split(",") if v.strip()] if isinstance(raw_sources, str) else list(raw_sources)
        start = sources.index(last_source) + 1 if last_source in sources else 0
        order = sources[start:] + sources[:start]
        selected_source = next((v for v in order if v in pool and v != last_source), None)
        if selected_source is None:
            selected_source = next(iter(pool))
            if len(sources) > 1 and selected_source == last_source:
                log.warning("Other sources unavailable; continuing with available content.")
        target_pool = pool
        selected = target_pool[selected_source]
        session.expunge(selected)
        return selected
    finally:
        session.close()


def post_to_story(api: Client, media: Any, media_path: str, username: str = "") -> None:
    """Share a posted reel to the account's story."""
    target_username = username or config.USERNAME
    user_info = api.user_info_by_username(target_username)
    hashtag = api.hashtag_info("like")

    duration = get_video_duration(media_path)
    if duration > STORY_MAX_DURATION_SECONDS:
        media_path = trim_video(media_path, os.path.join(config.DOWNLOAD_DIR, f"{media.code}.mp4"))

    media_pk = api.media_pk_from_url(f"https://www.instagram.com/p/{media.code}/")
    api.video_upload_to_story(
        media_path,
        "",
        mentions=[StoryMention(user=user_info, x=0.49892962, y=0.703125, width=0.8333333333333334, height=0.125)],
        links=[StoryLink(webUri=f"https://www.instagram.com/p/{media.code}/")],
        hashtags=[StoryHashtag(hashtag=hashtag, x=0.23, y=0.32, width=0.5, height=0.22)],
        medias=[StoryMedia(media_pk=media_pk, x=0.5, y=0.5, width=0.6, height=0.8)],
    )


def resolve_cover_image(path: Optional[str]) -> Optional[str]:
    """Resolve a configured cover image path to an existing file."""
    if not path:
        return None
    path_str = str(path).strip('"' + "'").strip()
    if not path_str:
        return None
    if os.path.exists(path_str):
        return path_str
    rel_path = os.path.join(config.BASE_DIR, path_str)
    if os.path.exists(rel_path):
        return rel_path
    return None


def build_caption(reel: Reel) -> str:
    """Build the caption from the custom caption/original caption + hashtags."""
    custom_desc = Helper.get_config("CUSTOM_CAPTION")
    if custom_desc is None:
        custom_desc = getattr(config, "CUSTOM_CAPTION", "")

    hashtags = Helper.get_config("HASTAGS")
    if hashtags is None:
        hashtags = getattr(config, "HASHTAGS", "")

    parts = []
    if custom_desc and custom_desc.strip():
        parts.append(custom_desc.strip())
    elif reel.caption and reel.caption.strip():
        parts.append(reel.caption.strip())
    if hashtags and hashtags.strip():
        parts.append(hashtags.strip())
    return "\n\n".join(parts)


def recover_pending_upload(api: Client, account: str) -> Optional[str]:
    """Quarantine legacy interrupted uploads; never infer success from recency."""
    marker = statefile.read_pending_upload(account)
    if not marker:
        return None
    code = str(marker.get("code") or "")
    if code and delivery.claim(account, code):
        delivery.uncertain(account, code, "Legacy interrupted upload; manual reconciliation required")
    return code


def _upload(api: Client, reel: Reel, full_caption: str) -> Tuple[Any, bool]:
    """Upload a reel, tolerating instagrapi's post-upload configuration errors.

    Returns (media_or_dummy, verified) where `verified` reports whether the
    upload was confirmed through the API after an error.
    """
    upload_kwargs: Dict[str, Any] = {
        "caption": full_caption,
        "extra_data": {
            "like_and_view_counts_disabled": config.LIKE_AND_VIEW_COUNTS_DISABLED,
            "disable_comments": config.DISABLE_COMMENTS,
        },
    }

    cover_path = Helper.get_config("REEL_COVER_PATH") or getattr(config, "REEL_COVER_PATH", "")
    thumbnail_file = resolve_cover_image(cover_path)
    if thumbnail_file:
        upload_kwargs["thumbnail"] = thumbnail_file
        console_print(f"  Using custom cover image: {thumbnail_file}")
    else:
        try:
            generated_thumb = f"{reel.file_path}.jpg"
            if not os.path.exists(generated_thumb):
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "0",
                                "-i", reel.file_path, "-frames:v", "1", "-threads", "1", generated_thumb],
                               check=True, timeout=45, capture_output=True)
            upload_kwargs["thumbnail"] = generated_thumb
            console_print(f"  Generated thumbnail: {generated_thumb}")
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            console_print(f"  Could not generate thumbnail: {exc}")

    return api.clip_upload(reel.file_path, **upload_kwargs), True


def post_for_account(api: Client, username: str, story_username: Optional[str] = None) -> bool:
    """Post one reel assigned to `username`. Returns True on success."""
    global is_uploading

    Helper.load_all_config()
    if api is None:
        console_print(f"  @{username}: no API client available. Skipping.")
        return False

    reel = get_reel(assigned_to=username)
    if not reel:
        console_print(f"  @{username}: no pending reel with a valid video file. Skipping.")
        return False

    with statefile.upload_lock(owner=username) as acquired:
        if not acquired:
            console_print(f"  @{username}: another upload is in progress. Skipping this cycle.")
            return False

        # Re-read the row inside the lock (SELECT ... FOR UPDATE semantics)
        session = Session()
        try:
            fresh_reel = session.query(Reel).filter_by(code=reel.code).first()
            if not fresh_reel or fresh_reel.is_posted:
                console_print(f"  Reel {reel.code} already posted (race condition avoided). Skipping.")
                return False
            if fresh_reel.assigned_to and username and fresh_reel.assigned_to != username:
                console_print(f"  Reel {reel.code} was reassigned to @{fresh_reel.assigned_to}. Skipping.")
                return False
            if username and username in distributor.posted_by_list(fresh_reel.posted_by):
                console_print(f"  @{username} already posted reel {reel.code}. Skipping.")
                return False
            file_path = fresh_reel.file_path
            session.expunge(fresh_reel)
            reel = fresh_reel
        finally:
            session.close()

        if not file_path or not os.path.exists(file_path):
            console_print(f"  File missing for {reel.code}: {file_path}. Skipping.")
            return False

        full_caption = build_caption(reel)
        console_print(f"  @{username}: uploading reel {reel.code} from @{reel.account}...")
        console_print(f"  Using caption: {repr(full_caption)}")

        if not delivery.claim(username, reel.code):
            return False
        statefile.write_pending_upload(username, reel.code, file_path)
        is_uploading = True
        try:
            api.delay_range = [1, 3]
            media, verified = _upload(api, reel, full_caption)

            if media and getattr(media, "pk", None) and verified:
                delivery.confirm(username, reel.code, media)
                from accounts import update_account
                update_account(username, last_source=reel.account)
                statefile.clear_pending_upload(username)
                console_print(f"  POSTED reel {reel.code} as @{username} (pk={media.pk})")
                try:
                    notify_discord(media.code, reel.account, full_caption, posted_by=username)
                except Exception:
                    log.warning("Notification failed; confirmed delivery is preserved.")

                if str(config.IS_POST_TO_STORY) == "1" and getattr(media, "code", None):
                    try:
                        post_to_story(api, media, file_path, username=story_username or username)
                    except Exception as story_error:  # story failures must never break posting
                        log.warning(f"Story post warning: {type(story_error).__name__}: {story_error}")
                return True

            delivery.uncertain(username, reel.code, "Upload response missing destination ID")
            console_print(f"  UNCERTAIN: upload of {reel.code}; held for review.")
            return False
        except (LoginRequired, ClientError) as exc:
            delivery.uncertain(username, reel.code, type(exc).__name__)
            console_print(f"  FAILED: API error posting {reel.code}: {type(exc).__name__}: {exc}")
            raise
        except Exception as exc:  # keep the loop alive no matter what
            delivery.uncertain(username, reel.code, type(exc).__name__)
            console_print(f"  FAILED: error posting reel {reel.code}: {type(exc).__name__}: {exc}")
            return False
        finally:
            is_uploading = False


def main(api: Client) -> bool:
    """Legacy single-account entry point. Posts one reel; True when posted."""
    Helper.load_all_config()
    diskspace.ensure_free_space()
    username = (Helper.get_config("USERNAME") or config.USERNAME or "").strip()

    reel = get_reel(assigned_to=None)
    if reel is None:
        console_print("  No pending reel with valid video file. Skipping.")
        return False

    # Reuse the hardened path, but without the per-account assignment filter
    session = Session()
    try:
        row = session.query(Reel).filter_by(code=reel.code).first()
        if row is not None and not row.assigned_to:
            row.assigned_to = username or None
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

    return post_for_account(api, username or (reel.assigned_to or ""), story_username=username)
