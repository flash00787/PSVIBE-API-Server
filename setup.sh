#!/bin/bash
# PS VIBE API Server — Prerequisite Checker & Setup
# Run: bash setup.sh
set -e

echo "══════════════════════════════════════════════"
echo "  PS VIBE API Server — Setup Check"
echo "══════════════════════════════════════════════"
echo ""

PASS=0
FAIL=0
WARN=0

ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
fail(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn(){ echo "  ⚠️  $1"; WARN=$((WARN+1)); }

# ── Python ──
echo "── Python ──"
PYTHON=$(which python3 2>/dev/null || echo "")
if [ -n "$PYTHON" ]; then
    PYVER=$($PYTHON --version 2>&1 | awk '{print $2}')
    ok "python3 found: $PYVER"
else
    fail "python3 not found"
fi

# ── Pip packages ──
echo ""
echo "── Python Dependencies ──"
for pkg in fastapi uvicorn mysql-connector-python pymysql pyjwt bcrypt httpx; do
    if $PYTHON -c "import $pkg" 2>/dev/null; then
        ok "$pkg"
    else
        # Map to import name
        case $pkg in
            mysql-connector-python) imp="mysql.connector" ;;
            pyjwt) imp="jwt" ;;
            *) imp="$pkg" ;;
        esac
        if $PYTHON -c "import $imp" 2>/dev/null; then
            ok "$pkg"
        else
            fail "$pkg (missing)"
        fi
    fi
done

# ── Systemd Service ──
echo ""
echo "── Systemd Service ──"
if systemctl is-active psvibe-api >/dev/null 2>&1; then
    ok "psvibe-api.service is running"
elif systemctl is-enabled psvibe-api >/dev/null 2>&1; then
    warn "psvibe-api.service is installed but not running"
else
    warn "psvibe-api.service not found"
fi

# ── MySQL ──
echo ""
echo "── MySQL ──"
if systemctl is-active mysql >/dev/null 2>&1; then
    ok "MySQL service is running"
else
    warn "MySQL service not running — API will use file-based fallbacks"
fi

# ── Required Environment Variables ──
echo ""
echo "── Environment Variables ──"
REQUIRED_VARS=("JWT_SECRET_KEY")
OPTIONAL_VARS=("API_KEY" "MYSQL_HOST" "MYSQL_USER" "MYSQL_PASSWORD" "MYSQL_DATABASE" "CUSTOMER_BOT_TOKEN" "BOT_TOKEN" "WEBHOOK_SECRET" "CORS_EXTRA_ORIGINS" "DEBUG")

for var in "${REQUIRED_VARS[@]}"; do
    if [ -n "${!var}" ]; then
        ok "$var is set"
    else
        fail "$var is NOT set (REQUIRED)"
    fi
done

for var in "${OPTIONAL_VARS[@]}"; do
    if [ -n "${!var}" ]; then
        ok "$var is set"
    else
        warn "$var is not set (optional)"
    fi
done

# ── File Permissions ──
echo ""
echo "── Files ──"
for f in app.py config.py auth.py; do
    if [ -f "$(dirname "$0")/$f" ]; then
        ok "$f exists"
    else
        fail "$f missing"
    fi
done

# ── Network ──
echo ""
echo "── Network ──"
PORT=${PORT:-8000}
if ss -tlnp | grep -q ":$PORT "; then
    ok "Port $PORT is listening"
else
    warn "Port $PORT not listening (API may not be started yet)"
fi

# ── Summary ──
echo ""
echo "══════════════════════════════════════════════"
echo "  Results: $PASS passed, $WARN warnings, $FAIL failures"
if [ $FAIL -gt 0 ]; then
    echo "  ⚠️  Fix failures above before starting the API."
    exit 1
else
    echo "  ✅ All checks passed!"
    echo ""
    echo "  To start the API:"
    echo "    sudo systemctl start psvibe-api"
    echo "  Or for development:"
    echo "    cd $(dirname "$0") && python3 app.py"
fi
echo "══════════════════════════════════════════════"
