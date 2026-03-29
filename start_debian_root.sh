#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root." >&2
  exit 1
fi

APP_DIR="${APP_DIR:-/root/HI}"
SERVICE_NAME="${SERVICE_NAME:-monitoring-worker}"
CONFIG_PATH="${CONFIG_PATH:-$APP_DIR/config.yaml}"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Missing config file: $CONFIG_PATH" >&2
  echo "Upload your config.yaml first, then run this script again." >&2
  exit 1
fi

if grep -q '^# PLACEHOLDER_CONFIG_UPLOAD_REQUIRED' "$CONFIG_PATH"; then
  echo "Placeholder config detected: $CONFIG_PATH" >&2
  echo "Replace it with your real config.yaml first, then run this script again." >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo
echo "Follow logs with:"
echo "journalctl -u ${SERVICE_NAME} -f"
