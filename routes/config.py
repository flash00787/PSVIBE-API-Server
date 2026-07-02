"""PS VIBE API — Config & Console Routes."""
from fastapi import APIRouter, Depends
from mysql_db import query as _mysql_query
from helpers import _mysql_get_setting, _mysql_get_settings_dict
from deps import verify_api_key
from auth import get_current_user

router = APIRouter(prefix="/api", tags=["Config"])


@router.get("/config")
async def get_config(auth=Depends(verify_api_key)):
    """Return all configuration from MySQL settings."""
    cfg = _mysql_get_settings_dict()
    return {"success": True, "data": cfg}


@router.get("/consoles")
async def get_consoles(auth=Depends(verify_api_key)):
    """Return current console status list."""
    rows = _mysql_query("SELECT * FROM console_status ORDER BY console_id")
    return {"success": True, "consoles": rows}
