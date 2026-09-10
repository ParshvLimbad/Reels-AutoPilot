import os
import paramiko
from scp import SCPClient

host = '192.168.29.60'
port = 22
username = 'electro'
password = 'electro'

def create_ssh_client(server, port, user, password):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(server, port, user, password)
    return client

print("Connecting...")
ssh = create_ssh_client(host, port, username, password)

print("Transferring configs...")
with SCPClient(ssh.get_transport()) as scp:
    scp.put('configs_export.json', '~/reels-autopilot/configs_export.json')
    scp.put('import_config.py', '~/reels-autopilot/import_config.py')

print("Running import...")
stdin, stdout, stderr = ssh.exec_command('cd ~/reels-autopilot && ./venv/bin/python3 import_config.py')
print(stdout.read().decode())
err = stderr.read().decode()
if err: print("ERR:", err)

print("Restarting service...")
stdin, stdout, stderr = ssh.exec_command('echo electro | sudo -S systemctl restart reels-autopilot')
print(stdout.read().decode())

ssh.close()
print("Done!")
