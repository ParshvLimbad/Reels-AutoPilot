import paramiko
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.29.60', 22, 'electro', 'electro')
cmds = [
    'cd ~/reels-autopilot && ./venv/bin/python3 -c "import sqlite3; c=sqlite3.connect(\'database/sqlite.db\'); print(c.execute(\'SELECT * FROM config WHERE key=\\\'CUSTOM_CAPTION\\\'\').fetchall())"'
]
for cmd in cmds:
    stdin, stdout, stderr = client.exec_command(cmd)
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err: print('ERR:', err)
