import os
from instagrapi import Client
from instagrapi.types import StoryMention, StoryMedia, StoryLink, StoryHashtag
from db import Session, Reel, ReelEncoder
from datetime import datetime
import config
import auth
import time
import helpers as Helper
from moviepy.editor import VideoFileClip
import logging
logging.getLogger("moviepy").setLevel(logging.ERROR)

from helpers import print as log_print
import builtins

def console_print(message):
    """Print to both console (rich) and log file."""
    builtins.print(message)
    log_print(message)

# Trim Video for story
def trim_video(file_path, output_path, max_duration=15):
    clip = VideoFileClip(file_path)
    trimmed_clip = clip.subclip(0, max_duration)
    trimmed_clip.write_videofile(output_path)
    return output_path

# Get Video Duration
def get_video_duration(file_path):
    clip = VideoFileClip(file_path)
    duration = clip.duration
    return duration

# Update is_posted and posted_at field in DB
def update_status(code):
    session = Session()
    session.query(Reel).filter_by(code=code).update({'is_posted': True, 'posted_at': datetime.now()})
    session.commit()
    session.close()


# Get Unposted reels from database with valid video file on disk
def get_reel():
    session = Session()
    unposted_reels = session.query(Reel).filter_by(is_posted=False).all()
    for reel in unposted_reels:
        if reel.file_path and os.path.exists(reel.file_path):
            session.close()
            return reel
        else:
            console_print(f"  Video file missing for reel code {reel.code}. Skipping.")
            reel.is_posted = True
            session.commit()
    session.close()
    return None

def post_to_story(api,media,media_path):

    username = api.user_info_by_username(config.USERNAME)
    hashtag = api.hashtag_info('like')

    duration = get_video_duration(media_path)
    if duration > 15:
        media_path = trim_video(media_path,config.DOWNLOAD_DIR+os.sep+media.code+".mp4")

    media_pk = api.media_pk_from_url('https://www.instagram.com/p/'+media.code+'/')

    api.video_upload_to_story(
        media_path,
        "",
        mentions=[StoryMention(user=username, x=0.49892962, y=0.703125, width=0.8333333333333334, height=0.125)],
        links=[StoryLink(webUri='https://www.instagram.com/p/'+media.code+'/')],
        hashtags=[StoryHashtag(hashtag=hashtag, x=0.23, y=0.32, width=0.5, height=0.22)],
        medias=[StoryMedia(media_pk=media_pk, x=0.5, y=0.5, width=0.6, height=0.8)],
    )

def resolve_cover_image(path):
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


# Magic Starts Here
def main(api):
    """Post one reel. Returns True if successfully posted, False otherwise."""
    Helper.load_all_config()
    reel = get_reel()
    if not reel:
        console_print("  No pending reel with valid video file. Skipping.")
        return False

    try:
        console_print(f"  Uploading Reel {reel.code} from @{reel.account}...")
        api.delay_range = [1, 3]

        # Build caption: CUSTOM_CAPTION (or original reel caption), plus hashtags
        custom_desc = Helper.get_config('CUSTOM_CAPTION')
        if custom_desc is None:
            custom_desc = getattr(config, 'CUSTOM_CAPTION', '')

        hashtags = Helper.get_config('HASTAGS')
        if hashtags is None:
            hashtags = getattr(config, 'HASHTAGS', '')

        caption_parts = []
        if custom_desc and custom_desc.strip():
            caption_parts.append(custom_desc.strip())
        elif reel.caption and reel.caption.strip():
            caption_parts.append(reel.caption.strip())

        if hashtags and hashtags.strip():
            caption_parts.append(hashtags.strip())

        full_caption = "\n\n".join(caption_parts)

        # Determine reel cover image thumbnail
        cover_path = Helper.get_config('REEL_COVER_PATH') or getattr(config, 'REEL_COVER_PATH', '')
        thumbnail_file = resolve_cover_image(cover_path)

        upload_kwargs = {
            "caption": full_caption,
            "extra_data": {
                "like_and_view_counts_disabled": config.LIKE_AND_VIEW_COUNTS_DISABLED,
                "disable_comments": config.DISABLE_COMMENTS,
            }
        }
        if thumbnail_file:
            upload_kwargs["thumbnail"] = thumbnail_file
            console_print(f"  Using custom cover image: {thumbnail_file}")

        media = api.clip_upload(reel.file_path, **upload_kwargs)

        if media and getattr(media, 'pk', None):
            update_status(reel.code)
            console_print(f"  POSTED Reel {reel.code} to Instagram (pk={media.pk})")

            if str(config.IS_POST_TO_STORY) == "1":
                try:
                    post_to_story(api, media, reel.file_path)
                except Exception as story_err:
                    log_print(f"Story post warning: {story_err}")
            return True
        else:
            console_print(f"  FAILED: Upload returned empty response for {reel.code}. Not marked as posted.")
            return False

    except Exception as e:
        console_print(f"  FAILED: Error posting reel {reel.code}: {type(e).__name__}: {str(e)}")
        return False



# if __name__ == "__main__":
#     api = auth.login()
#     main(api)