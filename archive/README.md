# PS VIBE API Server — Archive

One-time migration, patching, and verification scripts. These were used during the
GSheets → MySQL migration (Phase 5, May 2026) and are kept for historical reference.

## Files

| File | Purpose | Date |
|------|---------|------|
| `patch_app.py` | Monkey-patches for app.py endpoints, added MySQL fallback paths | May 2026 |
| `patch-app.py` | Import patching script to inject MySQL paths into app.py | May 2026 |
| `fix_mysql_stock.py` | Fixed stock-out/in to use MySQL instead of GSheets | May 2026 |
| `fix_sheets_config.py` | Migrated settings from GSheets to MySQL config table | May 2026 |
| `phase5_migrate.py` | Main Phase 5 migration: full GSheets → MySQL data transfer | May 2026 |
| `verify_patch.py` | Verification script to confirm all patches were applied correctly | May 2026 |

## Status

✅ All migrations complete. MySQL is the primary data store.
These scripts are no longer needed for operation and are archived.
