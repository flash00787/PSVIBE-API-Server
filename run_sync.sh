#!/bin/bash
# PS VIBE GSheet → MySQL Sync Wrapper
# Loads secrets before running sync_service.py
# FIXED 2026-06-01: Use venv Python (system python3 lacked mysql.connector)
set -a
source /etc/psvibe/secrets.env
set +a
cd /root/psvibe_api_server
exec ./venv/bin/python3 sync_service.py
