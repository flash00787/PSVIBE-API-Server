#!/bin/bash
# PS VIBE GSheet → MySQL Sync Wrapper
# Loads secrets before running sync_service.py
set -a
source /etc/psvibe/secrets.env
set +a
cd /root/psvibe_api_server
exec python3 sync_service.py
