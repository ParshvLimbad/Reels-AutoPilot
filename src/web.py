#!/usr/bin/env python3
"""Lightweight web dashboard for Reels-AutoPilot configuration."""
import sys
import os

# Ensure src directory is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
from db import Session, Config, Reel
from sqlalchemy import desc
from datetime import datetime
import subprocess
import config
import helpers

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
app = Flask(__name__)


@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'dashboard.html')


@app.route('/api/config', methods=['GET'])
def get_config_api():
    helpers.load_all_config()
    session = Session()
    configs = session.query(Config).all()
    result = {}
    for c in configs:
        result[c.key] = c.value
    session.close()

    # Merge defaults for keys not yet in DB
    defaults = {
        'IS_REMOVE_FILES': config.IS_REMOVE_FILES,
        'REMOVE_FILE_AFTER_MINS': str(config.REMOVE_FILE_AFTER_MINS),
        'IS_ENABLED_REELS_SCRAPER': config.IS_ENABLED_REELS_SCRAPER,
        'IS_ENABLED_AUTO_POSTER': config.IS_ENABLED_AUTO_POSTER,
        'IS_POST_TO_STORY': config.IS_POST_TO_STORY,
        'FETCH_LIMIT': str(config.FETCH_LIMIT),
        'POSTING_INTERVAL_IN_MIN': str(config.POSTING_INTERVAL_IN_MIN),
        'SCRAPER_INTERVAL_IN_MIN': str(config.SCRAPER_INTERVAL_IN_MIN),
        'USERNAME': config.USERNAME,
        'PASSWORD': config.PASSWORD,
        'ACCOUNTS': ','.join(config.ACCOUNTS) if isinstance(config.ACCOUNTS, list) else str(config.ACCOUNTS),
        'HASHTAGS': config.HASHTAGS,
        'HASTAGS': getattr(config, 'HASTAGS', config.HASHTAGS),
        'CUSTOM_CAPTION': getattr(config, 'CUSTOM_CAPTION', ''),
        'REEL_COVER_PATH': getattr(config, 'REEL_COVER_PATH', ''),
        'LIKE_AND_VIEW_COUNTS_DISABLED': config.LIKE_AND_VIEW_COUNTS_DISABLED,
        'DISABLE_COMMENTS': config.DISABLE_COMMENTS,
        'IS_ENABLED_YOUTUBE_SCRAPING': config.IS_ENABLED_YOUTUBE_SCRAPING,
        'YOUTUBE_API_KEY': config.YOUTUBE_API_KEY,
        'CHANNEL_LINKS': ','.join(config.CHANNEL_LINKS) if isinstance(config.CHANNEL_LINKS, list) else str(config.CHANNEL_LINKS),
    }

    for key, default_val in defaults.items():
        if key not in result:
            result[key] = str(default_val) if default_val is not None else ''

    # Mask password
    if 'PASSWORD' in result:
        result['PASSWORD'] = '********'

    return jsonify(result)


@app.route('/api/config', methods=['POST'])
def save_config_api():
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data provided'}), 400

    for key, value in data.items():
        if key == 'PASSWORD' and value == '********':
            continue
        helpers.save_config(key, str(value))

    # Reload config into memory
    helpers.load_all_config()

    return jsonify({'status': 'ok', 'message': 'Configuration saved'})


@app.route('/api/upload_cover', methods=['POST'])
def upload_cover_api():
    if 'cover_image' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['cover_image']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    if file:
        filename = secure_filename(file.filename)
        # Save to base dir
        save_path = os.path.join(config.BASE_DIR, filename)
        file.save(save_path)
        
        # Update config
        db_path = '/home/electro/reels-autopilot/' + filename
        helpers.save_config('REEL_COVER_PATH', db_path)
        helpers.load_all_config()
        
        return jsonify({'status': 'ok', 'message': 'Cover image uploaded and path updated', 'path': db_path})

@app.route('/api/purge_rescrape', methods=['POST'])
def purge_and_rescrape():
    try:
        # Run the purger script
        purger_cmd = ['/home/electro/reels-autopilot/venv/bin/python3', '/home/electro/reels-autopilot/src/purger.py']
        subprocess.run(purger_cmd, check=True)
        
        # Restart the autopilot service to trigger an immediate rescrape
        restart_cmd = 'echo electro | sudo -S systemctl restart reels-autopilot'
        subprocess.run(restart_cmd, shell=True, check=True)
        
        return jsonify({'status': 'ok', 'message': 'Successfully purged unposted reels and triggered a fresh scrape!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Failed to purge: {str(e)}'}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    session = Session()
    total = session.query(Reel).count()
    posted = session.query(Reel).filter_by(is_posted=True).count()
    pending = session.query(Reel).filter_by(is_posted=False).count()

    recent = session.query(Reel).order_by(desc(Reel.id)).limit(50).all()
    recent_reels = []
    for r in recent:
        recent_reels.append({
            'id': r.id,
            'code': r.code,
            'account': r.account or '',
            'is_posted': r.is_posted,
            'posted_at': r.posted_at.strftime('%b %d, %H:%M') if r.posted_at else None,
        })

    session.close()
    return jsonify({
        'total': total,
        'posted': posted,
        'pending': pending,
        'recent': recent_reels,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"[Dashboard] Running at http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
