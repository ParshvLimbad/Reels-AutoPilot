"""Known public reel downloader adapted from the user's brave-davinci project.
This resolves reel URLs; it does not discover a profile's latest reels.
"""
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit
import requests
import config

class DownloadError(Exception):
    pass

def create_payload(shortcode):
    """Create payload with dynamic shortcode"""
    variables = json.dumps({"shortcode": shortcode})
    encoded_variables = quote(variables)
    
    return f'av=0&__d=www&__user=0&__a=1&__req=u&__hs=20371.HYP%3Ainstagram_web_pkg.2.1...0&dpr=1&__ccg=GOOD&__rev=1028249517&__s=ywybjm%3Aq4co81%3Adplvd8&__hsi=7559456450740095677&__dyn=7xeUjG1mxu1syUbFp41twpUnwgU7SbzEdF8aUco2qwJw5ux609vCwjE1EE2Cw8G11wBz81s8hwGxu786a3a1YwBgao6C0Mo2swtUd8-U2zxe2GewGw9a361qw8Xxm16wa-0raazo7u3C2u2J0bS1LwTwKG0WE8oC1Iwqo5p0OwUQp1yU426V89F8uwm8jwhUaE4e1tyVrx60gm5oswFwtF85i5E&__csr=geIAaiFliZllsBav4trBuTJ-KJ5WhnQyAnxeEWpBCC-hJADG9AgG4qpQ8zat5BypWy9eaRgBaJ2Xx2p6WgymmGDzQjJo8JJ4iKi8xObCjx50FzLF4-8DiwxDyGqoydV-ESQ9DLAB_GdDzFEsyUSeG8xmF9oymWyqyVFF84q5ooHohwuE5a0CU01kUUb81CE12E5V08m0WFA0ei80n2bLwjp42TOw2J-0rq04tUKp06PwEhy1u1ig4Dgy9wdW0D8n80rl0UxGtw53hEx2E1yPUy7U1J9Q0JFvc0cXwpyG4B6B2US01IAw2Bo0K215w0YEwj8&__hsdp=gaQbh9gple4i4WuA2XCG7RVt5m8DxGU4K32awCF0GBcq1AyH40uWxe3AwboK5-0FE8UbkkU4-4o11XwQCyE9UswZweC4U6iq6UOewJyEhwBwjQ2259o1oE1E85u0km5Unw7Pwaau1CwMwkEeU1v82ew2rA0LoW0W8aO0Ewc6&__hblp=0nE20wpGx6vxy2i1ryE9Gg6q1hwkE9WwkocUso4O2vDyof98K7o4-48hDwyLBx61HwkGg8VoGqawDxCGBwQxG6S0I8jwywXBCxKczEqxaax62m1FDxim1nw4axq0oC362m0iu7ohBxu11wEwfm0AE421xDwhEvwxzEvG2-3K0nO0zE1MUK0DA1DwgEizEW0Qp-2Awa8nxyi1fwRBwFwau68bE&__comet_req=7&lsd=AdGtgRvhyjc&jazoest=21085&__spin_r=1028249517&__spin_b=trunk&__spin_t=1760073111&__crn=comet.igweb.PolarisLoggedOutDesktopPostRouteNext&fb_api_caller_class=RelayModern&fb_api_req_friendly_name=PolarisPostRootQuery&server_timestamps=true&variables={encoded_variables}&doc_id=24368985919464652'

def shortcode(url):
    parsed=urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('instagram.com','www.instagram.com'):
        raise ValueError('Use an Instagram HTTPS reel URL')
    match=re.fullmatch(r'/(?:[A-Za-z0-9_.]+/)?(?:reel|p)/([A-Za-z0-9_-]+)/?',parsed.path)
    if not match:
        raise ValueError('Invalid reel URL')
    return match.group(1)

def fetch(code):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',code):
        raise ValueError('Invalid reel code')
    csrf=secrets.token_hex(16)
    try:
        response=requests.post('https://www.instagram.com/graphql/query',data=create_payload(code),
            headers={'content-type':'application/x-www-form-urlencoded',
                     'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
                     'x-ig-app-id':'936619743392459','x-csrftoken':csrf,'Cookie':'csrftoken='+csrf},timeout=(10,20))
        if response.status_code != 200:
            raise DownloadError('Public scraper HTTP '+str(response.status_code))
        items=response.json().get('data',{}).get('xdt_api__v1__media__shortcode__web_info',{}).get('items',[])
    except (requests.RequestException,ValueError):
        raise DownloadError('Public scraper network or response error') from None
    if not items or items[0].get('code') != code or not items[0].get('video_versions'):
        raise DownloadError('Public reel not available')
    return items[0]

def download(code):
    item=fetch(code)
    variant=max(item['video_versions'],key=lambda v:int(v.get('width',0))*int(v.get('height',0)))
    url=variant['url'];parsed=urlsplit(url)
    if parsed.scheme!='https' or not (parsed.hostname or '').endswith(('.cdninstagram.com','.fbcdn.net')):
        raise DownloadError('Unexpected video host')
    target=Path(config.DOWNLOAD_DIR)/(code+'.mp4')
    temp=target.with_suffix('.mp4.part')
    try:
        with requests.get(url,stream=True,timeout=(10,30)) as response:
            if response.status_code!=200:
                raise DownloadError('Video download HTTP '+str(response.status_code))
            total=0
            with temp.open('wb') as stream:
                for chunk in response.iter_content(65536):
                    total+=len(chunk)
                    if total>300*1024*1024:
                        raise DownloadError('Video exceeds Pi download limit')
                    stream.write(chunk)
        probe=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_type','-of','csv=p=0',str(temp)],capture_output=True,text=True,timeout=20)
        if probe.returncode or 'video' not in probe.stdout:
            raise DownloadError('Downloaded file has no valid video stream')
        os.replace(temp,target)
    except requests.RequestException:
        raise DownloadError('Video transfer interrupted') from None
    finally:
        temp.unlink(missing_ok=True)
    return item,str(target)

def import_url(url, assigned_to=None):
    from db import Session,Reel
    import delivery
    code=shortcode(url)
    with Session() as session:
        row=session.query(Reel).filter_by(code=code).first()
        if row and (row.is_posted or (assigned_to and delivery.blocked(assigned_to,code))):
            return False
        if row and row.file_path and Path(row.file_path).is_file():
            return False
    item,path=download(code)
    return store(item,path,assigned_to)

def store(item,path,assigned_to=None):
    from db import Session,Reel
    code=item['code']
    with Session() as session:
        row=session.query(Reel).filter_by(code=code).first()
        if row is None:
            row=Reel(code=code,is_posted=False,assigned_to=assigned_to)
            session.add(row)
        row.file_path=path;row.file_name=Path(path).name
        row.post_id=str(item.get('pk',''));row.account=item.get('user',{}).get('username','')
        row.caption=(item.get('caption') or {}).get('text','')
        row.data=json.dumps(item)
        session.commit()
    return True

def repair_pending(limit=3):
    from db import Session,Reel
    import delivery
    import time, official
    state_path=Path(config.BASE_DIR)/'.official'/'public-download-backoff.json'
    state_path.parent.mkdir(exist_ok=True,mode=0o700)
    state=official.read(state_path)
    with Session() as session:
        rows=[(r.code,r.assigned_to) for r in session.query(Reel).filter_by(is_posted=False).order_by(Reel.id)
              if state.get(r.code,0)<=time.time() and (not r.file_path or not Path(r.file_path).is_file()) and not (r.assigned_to and delivery.blocked(r.assigned_to,r.code))][:limit]
    if state.get('_retry_at',0)>time.time():
        return 0
    count=0
    for code,owner in rows:
        # Stop the batch on first error, especially 429; no password fallback.
        if state.get(code,0)>time.time():
            continue
        try:
            count+=bool(import_url('https://www.instagram.com/reel/'+code+'/',owner))
        except DownloadError as exc:
            from logger import get_logger
            get_logger(__name__).warning('Public download %s: %s',code,exc)
            state[code]=time.time()+86400
            if '429' in str(exc) or 'network' in str(exc).lower():
                state['_retry_at']=time.time()+3600
            official.save(state_path,state)
            break
    return count
