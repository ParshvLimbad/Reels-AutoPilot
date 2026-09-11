import paramiko
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.29.60', 22, 'electro', 'electro', timeout=15)

# Wait for the scraper to finish
print("Waiting 30s for scraper to finish...")
time.sleep(30)

# Check logs
stdin, stdout, stderr = client.exec_command('journalctl -u reels-autopilot -n 30 --no-pager')
print(stdout.read().decode('utf-8', errors='replace'))

# Check DB now
stdin, stdout, stderr = client.exec_command("cd ~/reels-autopilot && ./venv/bin/python3 -c \"import sqlite3; c=sqlite3.connect('database/sqlite.db'); print('PENDING:', c.execute('SELECT account, COUNT(*) FROM reels WHERE is_posted=0 GROUP BY account').fetchall()); print('TOTAL:', c.execute('SELECT COUNT(*) FROM reels').fetchone())\"")
print(stdout.read().decode('utf-8', errors='replace'))

# Upload cover image via SCP
sftp = client.open_sftp()
sftp.put(r'C:\Users\Parshv\Downloads\cover_image.jpg', '/home/electro/reels-autopilot/cover_image.jpg')
sftp.close()
print("Cover image uploaded!")

# Save REEL_COVER_PATH in DB
stdin, stdout, stderr = client.exec_command("cd ~/reels-autopilot && ./venv/bin/python3 -c \"import sqlite3; c=sqlite3.connect('database/sqlite.db'); c.execute('UPDATE config SET value=\\\"/home/electro/reels-autopilot/cover_image.jpg\\\" WHERE key=\\\"REEL_COVER_PATH\\\"'); rows=c.execute('SELECT changes()').fetchone(); print('Updated rows:', rows); c.commit() if rows[0] > 0 else c.execute('INSERT INTO config (key, value) VALUES (\\\"REEL_COVER_PATH\\\", \\\"/home/electro/reels-autopilot/cover_image.jpg\\\")'); c.commit()\"")
print(stdout.read().decode('utf-8', errors='replace'))
err = stderr.read().decode('utf-8', errors='replace')
if err: print('ERR:', err)

client.close()
