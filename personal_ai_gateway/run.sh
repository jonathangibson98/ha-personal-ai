#!/bin/sh
echo "Starting Personal AI Gateway..."
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA/cli"
if [ "$(id -u)" = 0 ] && id aicli >/dev/null 2>&1; then
  # The sign-in tools run as "aicli": they get /data/cli and can't read the gateway's keys.
  chmod 711 "$DATA" 2>/dev/null
  chmod 600 "$DATA"/*.json 2>/dev/null
  [ "$(stat -c %U "$DATA/cli")" = aicli ] || chown -R aicli "$DATA/cli"
fi
exec python3 /app/server.py
