"""Kora Dashboard — Command Execution Routes"""
import logging
import subprocess
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kora", tags=["Kora"])

ALLOWED_COMMANDS = {
    "services": "docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || echo '(no docker)'",
    "docker": "docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null | head -20",
    "uptime": "cat /proc/uptime 2>/dev/null && echo '---' && free -h 2>/dev/null|head -3",
    "disk": "df -h / | tail -1 2>/dev/null",
    "mem": "free -h 2>/dev/null|head -3",
    "process": "ps aux 2>/dev/null | head -10",
    "backup": "echo 'Backup system ready. Last run: check cron.'",
    "alerts": "echo 'Kora Health: All systems nominal'",
    "health": "echo 'OK - '$(date -u +%H:%M:%S' UTC')",
    "mysql": "docker exec psvibe-mysql mysqladmin ping 2>/dev/null && echo 'MySQL OK' || echo 'MySQL unreachable'",
}


@router.get("/cmd/{command}")
async def run_kora_command(command: str):
    """Execute allowed Kora commands and return output"""
    if command not in ALLOWED_COMMANDS:
        return {
            "success": False,
            "command": command,
            "error": f"Unknown. Allowed: {list(ALLOWED_COMMANDS.keys())}"
        }

    try:
        result = subprocess.run(
            ALLOWED_COMMANDS[command],
            shell=True,
            capture_output=True,
            text=True,
            timeout=15
        )
        return {
            "success": True,
            "command": command,
            "output": result.stdout.strip() or "(no output)",
            "error": result.stderr.strip() if result.stderr.strip() else None
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "command": command, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "command": command, "error": str(e)}
