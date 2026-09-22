"""Isolated, read-only media server. Never exposes the dashboard or project tree."""
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from flask import Flask, abort, send_file
import config

ROOT = Path(config.BASE_DIR) / '.media'
app = Flask(__name__, static_folder=None)

def origin():
    data = json.loads((ROOT / 'origin.json').read_text())
    url = data['url']
    if not re.fullmatch(r'https://[a-z0-9.-]+', url):
        raise ValueError('Invalid media origin')
    return url

def stage(path):
    source = Path(path).resolve(strict=True)
    base = Path(config.BASE_DIR).resolve()
    if not source.is_relative_to(base) or source.suffix.lower() not in ('.mp4','.mov','.jpg','.jpeg','.png'):
        raise ValueError('Only project media files may be shared')
    if any(part.startswith('.') for part in source.relative_to(base).parts):
        raise ValueError('Private files cannot be shared')
    public = origin()
    ticket = secrets.token_hex(32)
    directory = ROOT / ticket
    directory.mkdir(parents=True, mode=0o700)
    name = 'media' + source.suffix.lower()
    try:
        os.link(source, directory / name)
    except OSError:
        shutil.copyfile(source, directory / name)
    (directory / 'expires').write_text(str(time.time()+86400))
    return public + '/m/' + ticket + '/' + name, ticket

def expire(tickets):
    for ticket in tickets:
        if re.fullmatch(r'[a-f0-9]{64}',ticket):
            path = ROOT / ticket / 'expires'
            if path.exists():
                path.write_text(str(time.time()+60))

def revoke(tickets):
    for ticket in tickets:
        if not re.fullmatch(r'[a-f0-9]{64}', ticket):
            raise ValueError('Invalid media ticket')
        directory = ROOT / ticket
        if directory.is_symlink():
            raise ValueError('Invalid media directory')
        if directory.exists():
            # Invalidate before deleting bytes; subsequent requests return 404.
            (directory / 'expires').write_text('0')
            shutil.rmtree(directory)


def cleanup():
    if not ROOT.exists():
        return
    for path in ROOT.iterdir():
        if path.is_dir() and re.fullmatch(r'[a-f0-9]{64}',path.name):
            try:
                if float((path/'expires').read_text()) < time.time():
                    shutil.rmtree(path)
            except (OSError,ValueError):
                pass

@app.route('/health')
def health():
    return {'ok':True}

@app.route('/m/<ticket>/<name>')
def media(ticket,name):
    if not re.fullmatch(r'[a-f0-9]{64}',ticket) or not re.fullmatch(r'media\.(mp4|mov|jpg|jpeg|png)',name):
        abort(404)
    folder = ROOT / ticket
    try:
        if float((folder/'expires').read_text()) < time.time():
            abort(404)
        path=(folder/name).resolve(strict=True)
        if path.parent != folder.resolve():
            abort(404)
    except (OSError,ValueError):
        abort(404)
    response=send_file(path,conditional=True,max_age=0)
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return response

if __name__=='__main__':
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='127.0.0.1',port=8091,threaded=True)
