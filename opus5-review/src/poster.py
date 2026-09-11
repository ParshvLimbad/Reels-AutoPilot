"""Uploads one reel per call, per posting account.

Keeps the original single-account entry point `main(api)` working while adding
multi-account posting, duplicate protection, crash recovery, upload
verification and recycling/swap integration.
"""
from __future__ import annotations

import builtins
import logging
import os
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

# Fast in-memory guard against double posting within one process
_posted_codes: set = set()
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


def update_status(code: str, posted_by: str = "") -> bool:
    """Mark a reel as posted, recording which account posted it."""
    success = distributor.mark_posted(code, posted_by)
    if success:
        _posted_codes.add(code)
        distributor.remember_posted_code(code)
    return success


def get_reel(assigned_to: Optional[str] = None) -> Optional[Reel]:
    """Return the next unposted reel with a valid file, round-robin by source.

    When `assigned_to` is given, only reels assigned to that posting account
    are considered.
    """
    session = Session()
    try:
        query = session.query(Reel).filter(Reel.is_posted == False)  # noqa: E712
        if assigned_to:
            query = query.filter(Reel.assigned_to == assigned_to)
        unposted_reels = query.order_by(Reel.id).all()

        recent_codes = set(distributor.get_last_posted_codes())
        valid_by_account: Dict[str, Reel] = {}
        fallback_by_account: Dict[str, Reel] = {}
        for reel in unposted_reels:
            if not reel.file_path or not os.path.exists(reel.file_path):
                continue
            if reel.code in _posted_codes:
                continue
            source = reel.account or "unknown"
            if reel.code in recent_codes:
                fallback_by_account.setdefault(source, reel)
                continue
            valid_by_account.setdefault(source, reel)

        pool = valid_by_account or fallback_by_account
        if not pool:
            return None

        last_posted_times: Dict[str, datetime] = {}
        for source in pool:
            last_posted = (
                session.query(Reel)
                .filter_by(is_posted=True, account=source)
                .filter(Reel.posted_at != None)  # noqa: E711
                .order_by(desc(Reel.posted_at))
                .first()
            )
            last_posted_times[source] = last_posted.posted_at if last_posted else datetime.min

        selected_source = sorted(pool.keys(), key=lambda name: last_posted_times[name])[0]
        selected = pool[selected_source]
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


def verify_upload(api: Client, reel: Reel) -> bool:
    """Verify an upload really landed when instagrapi errored after uploading."""
    try:
        user_id = api.user_id
        if not user_id:
            account_info = api.account_info()
            user_id = getattr(account_info, "pk", None)
        if not user_id:
            log.warning("Upload verification skipped: no user id available.")
            return False
        medias = api.user_medias(user_id, UPLOAD_VERIFY_MEDIA_COUNT)
        if not medias:
            log.warning("Upload verification: account has no media.")
            return False
        latest = medias[0]
        taken_at = getattr(latest, "taken_at", None)
        recent = True
        if taken_at is not None:
            try:
                delta = datetime.now(taken_at.tzinfo) - taken_at
                recent = delta.total_seconds() < 900
            except (TypeError, ValueError):
                recent = True
        log.info(
            f"Upload verification for {reel.code}: latest media {getattr(latest, 'code', '?')} "
            f"(recent={recent})"
        )
        return bool(recent)
    except (LoginRequired, ClientError, OSError) as exc:
        log.warning(f"Upload verification failed: {type(exc).__name__}: {exc}")
        return False


def recover_pending_upload(api: Client, account: str) -> Optional[str]:
    """Verify an upload that was interrupted by a crash.

    Returns the reel code that was reconciled, if any.
    """
    marker = statefile.read_pending_upload(account)
    if not marker:
        return None
    code = str(marker.get("code") or "")
    log.warning(f"Found interrupted upload marker for reel {code} (@{account}). Verifying with Instagram...")

    session = Session()
    try:
        reel = session.query(Reel).filter_by(code=code).first()
        if reel is None:
            statefile.clear_pending_upload(account)
            return code
        already_posted = bool(reel.is_posted)
        session.expunge(reel)
    finally:
        session.close()

    if already_posted:
        statefile.clear_pending_upload(account)
        return code

    if api is not None and verify_upload(api, reel):
        log.warning(f"Reel {code} was already live on Instagram. Marking it as posted.")
        update_status(code, posted_by=account)
    else:
        log.info(f"Reel {code} was not posted before the crash. It stays in the queue.")
    statefile.clear_pending_upload(account)
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
                clip = VideoFileClip(reel.file_path)
                try:
                    clip.save_frame(generated_thumb, t=(clip.duration / 2.0))
                finally:
                    clip.close()
            upload_kwargs["thumbnail"] = generated_thumb
            console_print(f"  Generated thumbnail: {generated_thumb}")
        except (OSError, ValueError, IndexError) as exc:
            console_print(f"  Could not generate thumbnail: {exc}")

    try:
        return api.clip_upload(reel.file_path, **upload_kwargs), True
    except Exception as upload_error:  # instagrapi raises many unrelated types here
        message = str(upload_error)
        post_upload_failure = (
            "qe/expose" in message
            or "404" in message
            or "configure" in message.lower()
            or "JSONDecodeError" in type(upload_error).__name__
        )
        if not post_upload_failure:
            raise
        console_print(
            f"  [Warning] instagrapi failed after uploading ({type(upload_error).__name__}: {message[:200]}). "
            "Verifying whether the upload actually succeeded..."
        )
        verified = verify_upload(api, reel)
        console_print(f"  Upload verification result for {reel.code}: {'confirmed' if verified else 'unconfirmed'}")

        class DummyMedia:
            """Placeholder media returned when instagrapi loses the response."""

            pk = "unknown_pk"
            code = reel.code

        return DummyMedia(), verified


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

        statefile.write_pending_upload(username, reel.code, file_path)
        is_uploading = True
        try:
            api.delay_range = [1, 3]
            media, verified = _upload(api, reel, full_caption)

            if media and getattr(media, "pk", None) and verified:
                if not update_status(reel.code, posted_by=username):
                    console_print(f"  [Warning] DB write for {reel.code} could not be verified.")
                console_print(f"  POSTED reel {reel.code} as @{username} (pk={media.pk})")
                notify_discord(reel.code, reel.account, full_caption, posted_by=username)

                if str(config.IS_POST_TO_STORY) == "1" and getattr(media, "code", None):
                    try:
                        post_to_story(api, media, file_path, username=story_username or username)
                    except Exception as story_error:  # story failures must never break posting
                        log.warning(f"Story post warning: {type(story_error).__name__}: {story_error}")
                return True

            console_print(f"  FAILED: upload of {reel.code} could not be confirmed. Not marked as posted.")
            return False
        except (LoginRequired, ClientError) as exc:
            console_print(f"  FAILED: API error posting {reel.code}: {type(exc).__name__}: {exc}")
            raise
        except Exception as exc:  # keep the loop alive no matter what
            console_print(f"  FAILED: error posting reel {reel.code}: {type(exc).__name__}: {exc}")
            return False
        finally:
            is_uploading = False
            statefile.clear_pending_upload(username)


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
