import sys

with open('/root/psvibe_api_server/sync_service.py', 'r') as f:
    content = f.read()

# Backup
with open('/root/psvibe_api_server/sync_service.py.phase5.bak', 'w') as f:
    f.write(content)
print('Backup created')

# sync_members function
sync_members_func = '''
    # ── Sync: members ────────────────────────────────────────────

    def sync_members(self) -> int:
        """Sync Card_Wallet sheet → members MySQL table.

        Extracts member basic info from the Card_Wallet sheet.
        GSheet column mapping (0-indexed):
            1 → member_id, 2 → name, 3 → phone, 7 → balance_minutes

        Returns: Number of rows synced.
        """
        logger.info('Syncing members from %s …', SHEET_CARD_WALLET)
        try:
            ws = get_worksheet(SHEET_CARD_WALLET)
            rows_raw = ws.get_all_values()
        except Exception as e:
            logger.error('Failed to read %s for members: %s', SHEET_CARD_WALLET, e)
            self._update_sync_status('members', 0, 'error')
            return 0

        if len(rows_raw) < 2:
            logger.warning('%s has no data rows for members', SHEET_CARD_WALLET)
            self._update_sync_status('members', 0, 'ok')
            return 0

        parsed = []
        for row in rows_raw[1:]:
            if not row or len(row) < 2:
                continue
            mid = row[1].strip() if len(row) > 1 else ''
            if not mid:
                continue
            parsed.append({
                'member_id': mid,
                'name': row[2].strip() if len(row) > 2 else '',
                'phone': row[3].strip() if len(row) > 3 else '',
                'balance_minutes': float_safe(row[7]) if len(row) > 7 else 0.0,
            })

        if not parsed:
            logger.warning('No members found in %s', SHEET_CARD_WALLET)
            self._update_sync_status('members', 0, 'ok')
            return 0

        count = self._upsert_members(parsed)
        self._update_sync_status('members', len(parsed), 'ok')
        logger.info('members synced: %d rows', len(parsed))
        return len(parsed)

'''

# Find insertion point
marker = '    # ── Sync: card_wallet ────────────────────────────────────────'
if marker in content:
    content = content.replace(marker, sync_members_func + marker, 1)
    print('Inserted sync_members before sync_card_wallet')
else:
    print('ERROR: Marker not found!')
    sys.exit(1)

# Add to sync_all order
old_entry = '            ("card_wallet",       self.sync_card_wallet),'
new_entry = '            ("members",           self.sync_members),\n            ("card_wallet",       self.sync_card_wallet),'
if old_entry in content:
    content = content.replace(old_entry, new_entry, 1)
    print('Added members to sync_all order')
else:
    print('ERROR: sync_all entry not found!')
    sys.exit(1)

with open('/root/psvibe_api_server/sync_service.py', 'w') as f:
    f.write(content)

print('sync_service.py updated successfully')
