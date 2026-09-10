import sqlite3
import json
from datetime import datetime
import os

c = sqlite3.connect(os.path.expanduser('~/reels-autopilot/database/sqlite.db'))
with open(os.path.expanduser('~/reels-autopilot/configs_export.json'), 'r', encoding='utf-8') as f:
    configs = json.load(f)

for conf in configs:
    exists = c.execute('SELECT id FROM config WHERE key = ?', (conf["key"],)).fetchone()
    if exists:
        c.execute('UPDATE config SET value = ?, updated_at = ? WHERE key = ?', (conf["value"], datetime.now(), conf["key"]))
    else:
        c.execute('INSERT INTO config (key, value, created_at) VALUES (?, ?, ?)', (conf["key"], conf["value"], datetime.now()))
c.commit()
print("Configs imported!")
