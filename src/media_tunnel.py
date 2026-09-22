"""Quick tunnel supervisor; only the isolated media listener is exposed."""
import os
import re
import signal
import subprocess
from pathlib import Path
import config
import official

root=Path(config.BASE_DIR)/'.media'
root.mkdir(exist_ok=True,mode=0o700)
state=root/'origin.json'
state.unlink(missing_ok=True)
process=subprocess.Popen([str(Path(config.BASE_DIR)/'bin/cloudflared'),'tunnel','--no-autoupdate','--url','http://127.0.0.1:8091','--protocol','http2'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
def stop(*args):
    process.terminate()
signal.signal(signal.SIGTERM,stop)
try:
    for line in process.stdout:
        match=re.search(r'https://[a-z0-9-]+\.trycloudflare.com',line)
        if match:
            official.save(state,{'url':match.group(0)})
            print('Temporary media tunnel URL registered',flush=True)
    raise SystemExit(process.wait())
finally:
    state.unlink(missing_ok=True)
    process.terminate()
