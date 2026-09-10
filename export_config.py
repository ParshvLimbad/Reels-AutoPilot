import sqlite3
import json

c = sqlite3.connect('database/sqlite.db')
configs = []
try:
    for row in c.execute('SELECT key, value FROM config'):
        configs.append({"key": row[0], "value": row[1]})
    with open('configs_export.json', 'w', encoding='utf-8') as f:
        json.dump(configs, f, ensure_ascii=False)
    print("Configs exported!")
except Exception as e:
    print("Error:", e)
