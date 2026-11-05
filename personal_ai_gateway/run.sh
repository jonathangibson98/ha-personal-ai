#!/usr/bin/with-contenv bashio
bashio::log.info "Starting Personal AI Gateway..."
exec python3 /app/server.py
