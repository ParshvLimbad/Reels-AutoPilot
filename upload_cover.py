import os
import paramiko
from scp import SCPClient

host = '192.168.29.60'
port = 22
username = 'electro'
password = 'electro'
local_image = r'C:\Users\Parshv\Downloads\cover.jpg'
remote_image = '/home/electro/reels-autopilot/cover_image.jpg'

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, port, username, password)

print("Uploading image...")
with SCPClient(client.get_transport()) as scp:
    scp.put(local_image, remote_image)

print("Updating DB...")
cmd = f'''cd ~/reels-autopilot && ./venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('database/sqlite.db'); c.execute('UPDATE config SET value=\\'{remote_image}\\' WHERE key=\\'REEL_COVER_PATH\\''); c.commit()"'''
stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode())
err = stderr.read().decode()
if err: print("ERR:", err)

print("Restarting service...")
client.exec_command('echo electro | sudo -S systemctl restart reels-autopilot')

client.close()
print("Done!")
