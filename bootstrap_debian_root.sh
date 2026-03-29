#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root." >&2
  exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/SeanLau415/HI.git}"
APP_DIR="${APP_DIR:-/root/HI}"
SERVICE_NAME="${SERVICE_NAME:-monitoring-worker}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"

export DEBIAN_FRONTEND=noninteractive

echo "[1/6] Installing system packages..."
apt update
apt install -y git python3 python3-venv python3-pip ca-certificates

echo "[2/6] Fetching source code..."
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

echo "[3/6] Creating virtual environment..."
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[4/6] Preparing config slot..."
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cat >"$APP_DIR/config.yaml" <<'EOF'
# PLACEHOLDER_CONFIG_UPLOAD_REQUIRED
# Upload your real config.yaml to this path before starting the service.
# The worker will not start until this placeholder is replaced.
global:
  apprise_urls: []
targets: []
rss_feeds: []
EOF
fi

echo "[5/6] Installing systemd service..."
cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Monitoring Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
Environment=CONFIG_PATH=${APP_DIR}/config.yaml
Environment=PERSIST_STATE=true
Environment=DB_PATH=${APP_DIR}/history.db
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] Bootstrap finished."
echo
echo "Upload your real config.yaml to:"
echo "  ${APP_DIR}/config.yaml"
echo
echo "Then start the service with:"
echo "  systemctl daemon-reload"
echo "  systemctl enable --now ${SERVICE_NAME}"
echo
echo "Watch logs with:"
echo "  journalctl -u ${SERVICE_NAME} -f"
