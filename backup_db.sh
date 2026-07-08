#!/bin/bash
# PS VIBE MySQL Daily Backup
BACKUP_DIR="/root/psvibe_api_server/backups/db"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y-%m-%d_%H%M)
FILE="$BACKUP_DIR/psvibe_$DATE.sql.gz"
# Read password from secrets
MYSQL_PWD=$(grep MYSQL_PASSWORD /etc/psvibe/secrets.env | cut -d= -f2)
mysqldump -h 127.0.0.1 -u psvibe_user -p"$MYSQL_PWD" --no-tablespaces psvibe_api 2>/dev/null | gzip > "$FILE"
# Keep only last 7 days
find "$BACKUP_DIR" -name "psvibe_*.sql.gz" -mtime +7 -delete
echo "Backup: $(ls -lh $FILE | awk '{print $5}') → $FILE"
