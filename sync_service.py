"""PS VIBE API Server — GSheet Sync is DISABLED (migrated to MySQL only)"""
# All Google Sheet sync functions have been removed.
# Financial data is now managed entirely through MySQL.
# See: dashboard_routes.py, app.py, fix_protocol, coordination tools

def now_mmt():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=6, minutes=30)))

def get_sync_service():
    return None

def start_background_sync(*a,**kw):
    pass

def stop_background_sync(*a,**kw):
    pass

def get_last_sync_time(*a,**kw):
    return None

def is_data_stale(*a,**kw):
    return True

if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    logging.info('GSheet sync is DISABLED — MySQL only')
