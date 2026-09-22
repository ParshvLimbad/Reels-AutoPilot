"""Budgeted, asynchronous Apify discovery. Credentials never enter URLs or logs."""
import time
from datetime import datetime, timezone
from pathlib import Path
import requests
import config, official
from db import Session, Reel

ROOT = lambda: Path(config.BASE_DIR) / '.official'

def configured():
    return bool(official.read(ROOT() / 'apify-keys.json').get('keys'))

def call(key, method, path, **kwargs):
    try:
        r = requests.request(method, 'https://api.apify.com/v2/' + path,
            headers={'Authorization': 'Bearer ' + key}, timeout=(10, 30), **kwargs)
        if not r.ok:
            raise ValueError('Apify HTTP ' + str(r.status_code))
        return r.json()
    except requests.RequestException:
        raise ValueError('Apify connection failed; no automatic run resubmission') from None

def ingest(items):
    import public_reels
    count = 0
    with Session() as session:
        for item in items:
            url = item.get('url', '')
            try:
                code = public_reels.shortcode(url)
            except ValueError:
                continue
            if session.query(Reel).filter_by(code=code).first():
                continue
            owner = item.get('ownerUsername', '')
            if not owner:
                continue
            session.add(Reel(code=code, account=owner, post_id=str(item.get('id', '')),
                caption=item.get('caption', ''), is_posted=False))
            session.flush()
            count += 1
        session.commit()
    return count

def tick():
    keys = official.read(ROOT() / 'apify-keys.json').get('keys', [])
    if not keys:
        return 0
    path = ROOT() / 'apify-state.json'
    state = official.read(path)
    now = time.time()
    if state.get('uncertain') or state.get('retry_at', 0) > now:
        return 0
    try:
        active = state.get('active')
        if active:
            key = keys[active['key']]
            run = call(key, 'GET', 'actor-runs/' + active['id'])['data']
            if run['status'] in ('READY', 'RUNNING', 'TIMING-OUT', 'ABORTING'):
                return 0
            if run['status'] != 'SUCCEEDED':
                state.update(active=None, error='Apify run ' + run['status'], retry_at=now+3600)
                official.save(path, state)
                return 0
            items = call(key, 'GET', 'datasets/' + run['defaultDatasetId'] + '/items',
                         params={'clean': 'true', 'limit': 10})
            count = ingest(items)
            state.update(active=None, error='', last_imported=count, last_results=len(items))
            official.save(path, state)
            return count
        if state.get('next_run', 0) > now:
            return 0
        import reels
        sources = reels._source_accounts()
        if not sources:
            return 0
        # At most 120 requested results/day overall, 1800/key/month.
        stamp = datetime.now(timezone.utc)
        month, day = stamp.strftime('%Y-%m'), stamp.strftime('%Y-%m-%d')
        usage = state.setdefault('usage', {})
        choices = []
        for i in range(len(keys)):
            row = usage.setdefault(str(i), {})
            if row.get('month') != month:
                row.update(month=month, monthly=0)
            if row.get('day') != day:
                row.update(day=day, daily=0)
            if row['monthly'] + 10 <= 1800 and row['daily'] + 10 <= 60:
                choices.append(i)
        if not choices:
            state.update(error='Apify discovery budget reached', retry_at=now+3600)
            official.save(path, state)
            return 0
        i = min(choices, key=lambda n: usage[str(n)]['monthly'])
        source = sources[state.get('cursor', 0) % len(sources)]
        # Reserve before network: ambiguous POST never spends twice after restart.
        usage[str(i)]['monthly'] += 10
        usage[str(i)]['daily'] += 10
        state.update(uncertain=True, next_run=now+7200, cursor=state.get('cursor', 0)+1)
        official.save(path, state)
        run = call(keys[i], 'POST', 'acts/apify~instagram-reel-scraper/runs',
            params={'maxTotalChargeUsd': 0.026, 'timeout': 300},
            json={'username': [source], 'resultsLimit': 10, 'skipPinnedPosts': True,
                  'includeSharesCount': False, 'includeTranscript': False,
                  'includeDownloadedVideo': False})['data']
        state.update(uncertain=False, active={'id': run['id'], 'key': i}, error='')
        official.save(path, state)
    except (ValueError, KeyError, TypeError) as exc:
        state.update(error=str(exc) if isinstance(exc, ValueError) else 'Invalid Apify response', retry_at=now+3600)
        official.save(path, state)
    return 0
