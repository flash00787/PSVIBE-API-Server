#!/bin/bash
# PS VIBE Auto-Deploy — Pull & restart after git push
set -e
cd /root/psvibe_api_server
echo "[$(date)] Starting deploy..."

# Pull latest
git pull origin master 2>&1 || echo "⚠️ git pull had issues, continuing..."

# Install deps
./venv/bin/pip install -r requirements.txt --quiet 2>&1 | tail -1

# Restart API
systemctl restart psvibe-api
sleep 3

# Health check
if curl -s http://localhost:8000/api/health | grep -q '"ok"'; then
    echo "[$(date)] ✅ Deploy OK"
else
    echo "[$(date)] ❌ Health check failed — rolling back"
    systemctl status psvibe-api --no-pager -l | tail -5
    exit 1
fi

# Dashboard rebuild if needed
if [ -d /root/psvibe-dashboard ] && [ "$1" = "--dashboard" ]; then
    cd /root/psvibe-dashboard
    npm run build 2>&1 | tail -3
    systemctl restart psvibe-api
    echo "[$(date)] ✅ Dashboard rebuilt"
fi
