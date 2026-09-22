"""Official Instagram publishing. Token files never enter API responses or logs."""
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import requests
import config

ROOT = Path(config.BASE_DIR) / '.official'
VERSION = os.environ.get('INSTAGRAM_GRAPH_VERSION', 'v24.0')

class APIError(Exception):
    pass

def folder(username):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', username):
        raise ValueError('Invalid account')
    path = ROOT / username
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path

def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}

def save(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)

def connected(username):
    return (folder(username) / 'token.json').exists()

def request(token, method, endpoint, **values):
    try:
        response = requests.request(method, 'https://graph.instagram.com/' + endpoint,
            headers={'Authorization': 'Bearer ' + token},
            **({'params': values} if method == 'GET' else {'data': values}),
            timeout=(10, 45))
        body = response.json()
    except (requests.RequestException, ValueError):
        raise APIError('Network or invalid response; no automatic publish retry') from None
    if not response.ok or 'error' in body:
        error = body.get('error', {})
        raise APIError('Meta HTTP %s, code %s, subcode %s' %
            (response.status_code, error.get('code', '?'), error.get('error_subcode', '?')))
    return body

def connect(username, token):
    identity = request(token, 'GET', VERSION + '/me', fields='user_id,username')
    if identity.get('username', '').lower() != username.lower():
        raise ValueError('Token belongs to a different Instagram account')
    user_id = str(identity.get('user_id') or identity.get('id') or '')
    if not user_id.isdigit():
        raise ValueError('Meta did not return an account ID')
    save(folder(username) / 'token.json', {'token': token, 'id': user_id, 'saved': time.time()})

def public_url(value):
    parsed = urlsplit(value)
    import ipaddress
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use a public HTTPS media URL')
    if parsed.hostname in ('localhost',) or parsed.hostname.endswith(('.local', '.internal')):
        raise ValueError('Meta cannot fetch a local address')
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise ValueError('Meta cannot fetch a private address')
    except ValueError as exc:
        if str(exc).startswith('Meta'):
            raise
    return value

def enqueue(username, video, cover, caption, source_code=None):
    public_url(video)
    if cover:
        public_url(cover)
    if len(caption) > 2200:
        raise ValueError('Caption exceeds 2200 characters')
    import hashlib
    key = hashlib.sha256((source_code or video).encode()).hexdigest()
    path = folder(username) / ('job-' + key + '.json')
    if path.exists():
        raise ValueError('This video URL is already queued or was posted for this account')
    save(path, dict(state='queued', video=video, cover=cover, caption=caption,
                    created=time.time(), next_check=0, source_code=source_code))

def status(username):
    jobs = [read(p) for p in sorted(folder(username).glob('job-*.json'))]
    return {'connected': connected(username), 'settings': read(folder(username) / 'settings.json'), 'jobs': [
        {k: j.get(k) for k in ('state', 'error', 'media_id', 'created')} for j in jobs]}

def prefetch(username):
    """Stage at most the current reel and one following reel, without publishing."""
    directory = folder(username)
    settings = read(directory / 'settings.json')
    if not settings.get('auto'):
        return
    for _ in range(2):
        jobs = sorted(directory.glob('job-*.json'), key=lambda p: read(p).get('created', 0))
        active = [read(p) for p in jobs if read(p).get('state') in ('queued','processing','publishing')]
        if len(active) >= 2:
            return
        import delivery
        import media_host
        from poster import build_caption, get_reel, resolve_cover_image
        import helpers
        reel = get_reel(assigned_to=username, exclude_codes={read(p).get("source_code") for p in jobs},
                        rotation_after=active[-1].get("source_account") if active else None)
        if reel is not None and not delivery.blocked(username, reel.code):
            path = directory / ('job-' + __import__('hashlib').sha256(reel.code.encode()).hexdigest() + '.json')
            if not path.exists():
                video, video_ticket = media_host.stage(reel.file_path)
                tickets = [video_ticket]
                cover = settings.get('cover', '')
                configured = helpers.get_config('REEL_COVER_PATH') or getattr(config, 'REEL_COVER_PATH', '')
                if configured:
                    image = resolve_cover_image(configured)
                    if not image:
                        raise ValueError('Configured cover file is missing')
                    cover, ticket = media_host.stage(image)
                    tickets.append(ticket)
                enqueue(username, video, cover, build_caption(reel), reel.code)
                job = read(path)
                job['tickets'] = tickets
                job['local_video'] = reel.file_path
                job['source_account'] = reel.account
                save(path, job)

def finish_confirmed(username, path, job):
    if job.get('cleanup_done'):
        return
    import media_host
    if job.get('source_code'):
        import delivery
        from types import SimpleNamespace
        from db import Session, Reel
        from accounts import update_account
        delivery.confirm(username, job['source_code'], SimpleNamespace(pk=job['media_id'], code=''))
        with Session() as session:
            reel = session.query(Reel).filter_by(code=job['source_code']).first()
            if reel:
                update_account(username, last_source=reel.account)
    # Revoke only this delivery's links; keep the reusable configured cover.
    media_host.revoke(job.get('tickets', []))
    if job.get('local_video'):
        import remover
        remover.remove_file(job['local_video'])
        remover.remove_file(job['local_video'] + '.jpg')
    job['cleanup_done'] = True
    save(path, job)


def tick(username):
    """Advance one persisted job. Never resubmit an ambiguous publish request."""
    import media_host
    media_host.cleanup()
    directory = folder(username)
    credentials = read(directory / 'token.json')
    if not credentials:
        return False
    token = credentials['token']
    # Dashboard-generated long-lived tokens are refreshed while still valid.
    if time.time() - credentials.get('saved', 0) > 86400 * 7:
        refreshed = request(token, 'GET', 'refresh_access_token', grant_type='ig_refresh_token', access_token=token)
        credentials.update(token=refreshed['access_token'], saved=time.time())
        save(directory / 'token.json', credentials)
        token = credentials['token']
    for saved in directory.glob('job-*.json'):
        confirmed = read(saved)
        if confirmed.get('state') == 'posted':
            finish_confirmed(username, saved, confirmed)
    prefetch(username)
    jobs = sorted(directory.glob('job-*.json'), key=lambda p: read(p).get('created', 0))
    for path in jobs:
        job = read(path)
        if job['state'] in ('posted', 'failed', 'uncertain'):
            continue
        if job['state'] == 'publishing':
            job.update(state='uncertain', error='Worker interrupted during publish; verify on Instagram')
            save(path, job)
            return False
        if job.get('next_check', 0) > time.time():
            return False
        try:
            if job['state'] == 'queued':
                if job.get('tickets'):
                    # Refresh only the host if a temporary tunnel restarted.
                    job['video'] = media_host.origin() + '/m/' + job['video'].split('/m/',1)[1]
                    if '/m/' in job.get('cover',''):
                        job['cover'] = media_host.origin() + '/m/' + job['cover'].split('/m/',1)[1]
                data = dict(media_type='REELS', video_url=job['video'], caption=job['caption'], share_to_feed='true')
                if job['cover']:
                    data['cover_url'] = job['cover']
                container = request(token, 'POST', VERSION + '/' + credentials['id'] + '/media', **data)
                job.update(state='processing', container=container['id'], next_check=time.time()+60)
                save(path, job)
                return False
            state = request(token, 'GET', VERSION + '/' + job['container'], fields='status_code')
            if state.get('status_code') in ('ERROR', 'EXPIRED'):
                raise APIError('Media processing ' + state['status_code'])
            if state.get('status_code') != 'FINISHED':
                job['next_check'] = time.time() + 60
                save(path, job)
                return False
            quota = request(token, 'GET', VERSION + '/' + credentials['id'] + '/content_publishing_limit', fields='quota_usage,config')['data'][0]
            if quota['quota_usage'] >= quota['config']['quota_total']:
                job.update(next_check=time.time()+3600, error='Publishing quota reached; waiting')
                save(path, job)
                return False
            if job.get('source_code'):
                import delivery
                if not delivery.claim(username, job['source_code']):
                    job.update(state='uncertain', error='Existing delivery record; review required')
                    save(path, job)
                    return False
            job['state'] = 'publishing'
            save(path, job)
            result = request(token, 'POST', VERSION + '/' + credentials['id'] + '/media_publish', creation_id=job['container'])
            media_id = str(result.get('id', ''))
            if not media_id.isdigit():
                raise APIError('Publish response had no media ID')
            job.update(state='posted', media_id=media_id, error='', posted=time.time())
            save(path, job)
            finish_confirmed(username, path, job)
            prefetch(username)
            return True
        except Exception as exc:
            if job['state'] == 'posted':
                # Publication succeeded; retry local reconciliation on the next tick.
                return True
            job.update(state='uncertain' if job['state']=='publishing' else 'failed',
                       error=str(exc) if isinstance(exc, APIError) else type(exc).__name__)
            save(path, job)
            return False
    return False
