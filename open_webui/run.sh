#!/usr/bin/env bash
# Start Open WebUI with its data in /data (kept by Home Assistant backups) and the
# Personal AI Gateway as its only model source.
set -e
OPTS=/data/options.json
CONN=/share/personal_ai_gateway/connection.json
opt() {  # read one add-on option (empty when unset)
  python3 - "$1" <<'PY'
import json, os, sys
d = json.load(open("/data/options.json")) if os.path.exists("/data/options.json") else {}
v = d.get(sys.argv[1])
print("" if v is None else (("true" if v else "false") if isinstance(v, bool) else v))
PY
}
conn() { python3 -c "import json,sys;print(json.load(open('$CONN')).get(sys.argv[1],''))" "$1"; }

URL="$(opt gateway_url)"
KEY="$(opt api_key)"
if [ -z "$URL" ]; then
  # The gateway writes how to reach it into /share. Wait for it on the first start,
  # because Open WebUI only takes these settings the first time it runs.
  for i in $(seq 1 60); do
    [ -f "$CONN" ] && break
    [ "$i" = 1 ] && echo "[open-webui] Waiting for the Personal AI Gateway add-on (start it, version 2.0.0 or newer)..."
    sleep 2
  done
  if [ -f "$CONN" ]; then
    URL="$(conn base_url)"
    [ -n "$KEY" ] || KEY="$(conn api_key)"
  else
    echo "[open-webui] Gateway not found. Start the Personal AI Gateway add-on, or set gateway_url and api_key in this add-on's options."
  fi
fi
echo "[open-webui] Gateway: ${URL:-not set}"

mkdir -p /data/open-webui
[ -f /data/secret_key ] || python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/secret_key
export WEBUI_SECRET_KEY="$(cat /data/secret_key)"
export DATA_DIR=/data/open-webui
export PORT=8080
export WEBUI_NAME="AI Gateway Chat"
export ENABLE_OLLAMA_API=False
export ENABLE_OPENAI_API=True
export OPENAI_API_BASE_URL="$URL"
export OPENAI_API_KEY="$KEY"
[ "$(opt require_login)" = "false" ] && export WEBUI_AUTH=False
export SCARF_NO_ANALYTICS=true DO_NOT_TRACK=true ANONYMIZED_TELEMETRY=false

cd /app/backend
exec bash start.sh
