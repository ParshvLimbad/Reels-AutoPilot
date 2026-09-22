"""Delete confirmed local media after the configured retention, never history."""
import os
from datetime import datetime, timedelta
from db import Session, Reel, Delivery
import config
import helpers as Helper


def remove_file(path):
    if not path:
        return
    root = os.path.realpath(config.DOWNLOAD_DIR)
    target = os.path.realpath(path)
    if os.path.commonpath([root, target]) != root:
        return
    try:
        os.remove(target)
    except FileNotFoundError:
        pass


def main():
    Helper.load_all_config()
    cutoff = datetime.now() - timedelta(minutes=max(1, int(config.REMOVE_FILE_AFTER_MINS)))
    with Session() as s:
        uncertain = {r.code for r in s.query(Delivery).filter(Delivery.status.in_(["uploading", "uncertain"])).all()}
        for reel in s.query(Reel).filter(Reel.is_posted == True, Reel.posted_at <= cutoff).all():
            if reel.code not in uncertain:
                remove_file(reel.file_path)
                remove_file((reel.file_path or "") + ".jpg")
