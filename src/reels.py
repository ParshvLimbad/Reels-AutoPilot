from instagrapi import Client
from db import Session, Reel, ReelEncoder
import json
import config
import time
import auth
import helpers as Helper
from helpers import print


import os

#Function to fetch reel from given account
def get_reels(account, api):
    account_name = str(account).strip()
    if not account_name:
        return []
    user_id = api.user_id_from_username(account_name)
    fetch_limit = int(getattr(config, 'FETCH_LIMIT', 10))
    medias = api.user_medias(user_id, fetch_limit)
    reels = [item for item in medias if item.media_type == 2]  # Filter for video reels
    return reels


#Magic Starts Here
def main(api):
    Helper.load_all_config()
    session = Session()
    accounts = config.ACCOUNTS
    if isinstance(accounts, str):
        accounts = [a.strip() for a in accounts.split(",") if a.strip()]

    for account in accounts:
        account_name = str(account).strip()
        if not account_name:
            continue

        try:
            reels_by_account = get_reels(account_name, api)
        except Exception as e:
            print(f"[red] Error fetching reels for account {account_name}: {e} [/red]")
            continue

        for reel in reels_by_account:
            if reel.video_url != None:
                try:
                    print('------------------------------------------------------------------------------------')
                    print('Checking if reel : '+reel.code+' already downloaded')
                    exists = session.query(Reel).filter_by(code=reel.code).first()
                    if not exists:
                        print('Downloading Reel From : ' +account_name+ ' | Code : '+ reel.code)
                        downloaded_path = api.video_download_by_url(reel.video_url, folder=config.DOWNLOAD_DIR)
                        filepath = str(downloaded_path)
                        filename = os.path.basename(filepath)

                        print('Downloaded Reel Code : ' +reel.code+ ' | Path : '+filepath)
                        print('<---------Database Insert Start--------->')

                        reel_db = Reel(
                                    post_id=reel.id,
                                    code=reel.code,
                                    account = account_name,
                                    caption = reel.caption_text,
                                    file_name = filename,
                                    file_path = filepath,
                                    data = json.dumps(reel, cls=ReelEncoder),
                                    is_posted = False,
                                    )
                        session.add(reel_db)
                        session.commit()
                        
                        print('Inserting Record...')
                        print('<---------Database Insert End--------->')
                    print('------------------------------------------------------------------------------------')
                except Exception as e:
                    print(f"[red] Error downloading/saving reel {reel.code}: {e} [/red]")
                    pass
                
    session.close()

    # time.sleep(int(config.SCRAPER_INTERVAL_IN_MIN)*60)
    # main(api)

# if __name__ == "__main__":
#     api = auth.login()
#     main(api)