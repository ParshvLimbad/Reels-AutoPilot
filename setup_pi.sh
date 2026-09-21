#!/usr/bin/env bash
# Reels AutoPilot - Raspberry Pi setup (run as the normal Pi user, not root).
set -Eeuo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as the normal Pi user; it will use sudo only where needed."
  exit 1
fi

REPO_DIR="${REELS_AUTOPILOT_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
RUN_USER="$(id -un)"
VENV_PYTHON="$REPO_DIR/venv/bin/python3"

if [[ ! -f "$REPO_DIR/requirements.txt" ]]; then
  echo "Could not find requirements.txt in $REPO_DIR"
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-dev libffi-dev libssl-dev git ffmpeg

cd "$REPO_DIR"
if [[ ! -x "$VENV_PYTHON" ]]; then
  python3 -m venv venv
fi
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --no-cache-dir -r requirements.txt

mkdir -p database downloads sessions state logs covers

# The dashboard is intentionally password protected because it can manage
# posting accounts and display live TOTP codes. The generated credential stays
# only on the Pi in a mode-600 file.
if [[ ! -s "$REPO_DIR/.dashboard-password" ]]; then
  "$VENV_PYTHON" - "$REPO_DIR" <<'PY'
from pathlib import Path
import secrets
import sys
from werkzeug.security import generate_password_hash

root = Path(sys.argv[1])
password = secrets.token_urlsafe(18)
(root / ".dashboard-password").write_text(generate_password_hash(password) + "\n", encoding="utf-8")
(root / ".dashboard-access.txt").write_text(
    "Dashboard: http://<pi-ip>:8080\nUsername: admin\nPassword: " + password + "\n",
    encoding="utf-8",
)
(root / ".dashboard-password").chmod(0o600)
(root / ".dashboard-access.txt").chmod(0o600)
PY
  echo "Dashboard credentials were written to $REPO_DIR/.dashboard-access.txt"
fi

render_service() {
  local name="$1"
  local source="$REPO_DIR/systemd/$name.service"
  local target="/etc/systemd/system/$name.service"
  local temp
  temp="$(mktemp)"
  sed -e "s|__RUN_USER__|$RUN_USER|g" -e "s|__PROJECT_DIR__|$REPO_DIR|g" "$source" > "$temp"
  sudo install -m 0644 "$temp" "$target"
  rm -f "$temp"
}

render_service reels-autopilot
render_service reels-web
render_service reels-watchdog
sudo systemctl daemon-reload
sudo systemctl enable --now reels-autopilot reels-web reels-watchdog

echo "Setup complete. Dashboard: http://<pi-ip>:8080"
echo "Credentials: $REPO_DIR/.dashboard-access.txt"
echo "Logs: journalctl -u reels-autopilot -f"
