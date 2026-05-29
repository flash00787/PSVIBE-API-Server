
import json

with open('/root/psvibe_api_server/app.py', 'r') as f:
    content = f.read()

# Insert _fetch_config_from_mysql after the _fetch_topups_from_mysql function
# Find the marker: 'return None' followed by '# ═══' after _fetch_topups
marker = '''    return None


# ═══════════════════════════════════════
#  fetch_console_status'''

new_func = '''    return None


def _fetch_config_from_mysql():
    """Try to read full config from MySQL settings table + related tables.
    Returns dict with config data or None if MySQL data insufficient."""
    try:
        if not _use_mysql():
            return None
        # Read all settings as key-value pairs
        settings_rows = mysql_query("SELECT setting_key, setting_value FROM settings")
        if not settings_rows:
            return None
        settings = {r['setting_key']: r['setting_value'] for r in settings_rows}

        # Check we have at least the critical keys
        if 'base_rate' not in settings:
            return None

        # Parse simple values
        base_rate = int_safe(settings.get('base_rate', '0'))
        master_thresh = int_safe(settings.get('master_threshold', '0'))
        immortal_thresh = int_safe(settings.get('immortal_threshold', '0'))
        card_price = int_safe(settings.get('new_member_card_price', '0'))
        base_mins = int_safe(settings.get('new_member_base_mins', '0'))

        # Parse JSON fields for complex data
        console_multipliers = json.loads(settings.get('console_multipliers', '{}'))
        food_prices = json.loads(settings.get('food_prices', '{}'))
        food_costs = json.loads(settings.get('food_costs', '{}'))
        bonus_table = json.loads(settings.get('bonus_table', '[]'))

        return {
            "base_rate": base_rate,
            "master_threshold": master_thresh,
            "immortal_threshold": immortal_thresh,
            "new_member_card_price": card_price,
            "new_member_base_mins": base_mins,
            "console_multipliers": console_multipliers,
            "food_prices": food_prices,
            "food_costs": food_costs,
            "bonus_table": bonus_table,
            "source": "mysql",
        }
    except Exception:
        pass
    return None


# ═══════════════════════════════════════
#  fetch_console_status'''

if marker in content:
    content = content.replace(marker, new_func)
    with open('/root/psvibe_api_server/app.py', 'w') as f:
        f.write(content)
    print('SUCCESS: Inserted _fetch_config_from_mysql')
else:
    print('ERROR: Marker not found')
    # Find the _fetch_topups function end
    idx = content.find('_fetch_topups_from_mysql')
    if idx > 0:
        print(content[idx:idx+500])

