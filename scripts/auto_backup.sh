#!/bin/bash
# =============================================================================
# PS VIBE — MySQL Auto Backup Script
# =============================================================================
# Dumps psvibe_api database → /backups/psvibe_YYYY-MM-DD.sql.gz
# Keeps last 7 days of backups, deletes older ones.
# Run daily at 03:00 MMT (20:30 UTC) via cron.
#
# MySQL Credentials: from psvibe-mysql Docker container
# Database: psvibe_api
# User: psvibe_user
# Password: PsVibe@2026_Rotated!
# =============================================================================
set -euo pipefail

BACKUP_DIR="/backups"
DB_NAME="psvibe_api"
DB_USER="psvibe_user"
DB_PASS="PsVibe@2026_Rotated!"
CONTAINER="psvibe-mysql"
DATE_STAMP="$(date +%F)"
BACKUP_FILE="${BACKUP_DIR}/psvibe_${DATE_STAMP}.sql.gz"
LOG_FILE="/var/log/psvibe_backup.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ── Ensure backup directory exists ────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

# ── Run mysqldump via Docker container ────────────────────────────────────────
log "Starting MySQL backup for ${DB_NAME}..."

if docker exec "$CONTAINER" mysqldump \
    -u"$DB_USER" \
    -p"$DB_PASS" \
    --single-transaction \
    --routines \
    --triggers \
    --events \
    --hex-blob \
    "$DB_NAME" 2>/tmp/psvibe_dump_err.log | gzip > "$BACKUP_FILE"; then
    
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "✅ Backup created: ${BACKUP_FILE} (${BACKUP_SIZE})"
else
    DUMP_ERR=$(cat /tmp/psvibe_dump_err.log 2>/dev/null || echo "Unknown error")
    log "❌ Backup FAILED: ${DUMP_ERR}"
    # Clean up partial file
    rm -f "$BACKUP_FILE"
    exit 1
fi

# ── Cleanup: keep last 7 days, delete older ───────────────────────────────────
log "Cleaning up old backups (keeping last 7 days)..."

DELETED_COUNT=0
for old_backup in $(find "$BACKUP_DIR" -name "psvibe_*.sql.gz" -mtime +7 -type f | sort); do
    log "🗑️  Deleting old backup: $(basename "$old_backup")"
    rm -f "$old_backup"
    DELETED_COUNT=$((DELETED_COUNT + 1))
done

if [ "$DELETED_COUNT" -gt 0 ]; then
    log "🧹 Cleaned up ${DELETED_COUNT} old backup(s)"
else
    log "✨ No old backups to clean up"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
CURRENT_BACKUPS=$(find "$BACKUP_DIR" -name "psvibe_*.sql.gz" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo "N/A")
log "📊 Backup summary: ${CURRENT_BACKUPS} files, ${TOTAL_SIZE} total"
log "Done."
