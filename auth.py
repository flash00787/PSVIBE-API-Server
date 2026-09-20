"""PS VIBE Dashboard — JWT Authentication Module (DB-backed)"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import UploadFile, File, Request, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# --- Constants ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY env var is required — cannot start without it")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE = timedelta(hours=8)
REFRESH_TOKEN_EXPIRE = timedelta(days=7)

def _hash_pw(pw: str) -> str:
    import bcrypt
    return bcrypt.hashpw(pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def _verify_pw(pw: str, h: str) -> bool:
    import bcrypt
    return bool(bcrypt.checkpw(pw.encode('utf-8'), h.encode('utf-8')))

security = HTTPBearer(auto_error=False)


# --- Database-backed credentials ---
def _get_credentials_from_db() -> Dict[str, Dict[str, str]]:
    """Load all active users from dashboard_users table."""
    try:
        from mysql_db import query as _q
        rows = _q(
            "SELECT username, password_hash, role, display_name, photo_url FROM dashboard_users WHERE is_active = 1"
        )
        creds = {}
        for row in rows:
            creds[row["username"]] = {
                "password_hash": row["password_hash"],
                "photo_url": (row.get("photo_url") if isinstance(row, dict) else None) or "",
                "role": row["role"],
                "name": row.get("display_name") or row["username"],
            }
        return creds
    except Exception as e:
        logger.error(f"Failed to load credentials from DB: {e}")
        return {}

# Cache credentials (refreshed each call)
_creds_cache: Optional[Dict[str, Dict[str, Any]]] = None
_creds_cache_time: Optional[datetime] = None
_CREDS_CACHE_TTL: timedelta = timedelta(seconds=30)

def _auto_create_admin_if_empty() -> None:
    """If dashboard_users table is empty, create default admin user with a random password.
    The password is printed ONCE during setup — it MUST be changed after first login."""
    try:
        from mysql_db import query_one as _qo, execute as _e
        existing = _qo("SELECT COUNT(*) as cnt FROM dashboard_users")
        if not existing or existing["cnt"] == 0:
            import secrets, hashlib
            random_pw = secrets.token_hex(8)
            admin_hash = _hash_pw(random_pw)
            _e(
                "INSERT INTO dashboard_users (username, password_hash, role, display_name) VALUES (%s, %s, %s, %s)",
                ("admin", admin_hash, "admin", "Administrator"),
            )
            print(f"🔐 Default admin created! Username: admin, Password: {random_pw}")
            print("⚠️  CHANGE THIS PASSWORD IMMEDIATELY after first login!")
            logger.info("Default admin user created with random password")
    except Exception as e:
        logger.warning(f"Auto-create admin skipped: {e}")



def flush_credentials_cache() -> None:
    """Force refresh credentials cache (called after password changes)."""
    global _creds_cache, _creds_cache_time
    _creds_cache = None
    _creds_cache_time = None

def get_credentials() -> Dict[str, Dict[str, Any]]:
    global _creds_cache, _creds_cache_time
    now = datetime.now(timezone.utc)
    if _creds_cache is None or _creds_cache_time is None or (now - _creds_cache_time) > _CREDS_CACHE_TTL:
        _auto_create_admin_if_empty()
        _creds_cache = _get_credentials_from_db()
        _creds_cache_time = now
    return _creds_cache

# Keep _get_credentials for compatibility but deprecate
def _get_credentials() -> Dict[str, Dict[str, Any]]:
    """Deprecated — kept for backward compatibility only."""
    return get_credentials()


class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]

class RefreshRequest(BaseModel):
    refresh_token: str


# --- Core Functions ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _verify_pw(plain_password, hashed_password)

def _photo_url_for(username: str) -> str:
    """photo_url read directly from the DB — the credentials cache is built once and can be
    stale (owner 2026-09-21: after a re-login the avatar disappeared because the cached user
    row had no photo_url)."""
    try:
        from mysql_db import query_one as _q1
        row = _q1("SELECT photo_url FROM dashboard_users WHERE username = %s", (username,)) or {}
        return (row.get("photo_url") or "") if isinstance(row, dict) else ""
    except Exception:
        return ""


def get_user(username: str) -> Optional[Dict[str, Any]]:
    user = get_credentials().get(username)
    if user:
        return {**user, "username": username}
    return None

def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = get_user(username)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or ACCESS_TOKEN_EXPIRE)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(data: Dict[str, Any]) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + REFRESH_TOKEN_EXPIRE
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Dict[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    username = payload.get("sub")
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

async def get_staff_or_dashboard_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Dict[str, Any]:
    """Accept either a dashboard-user JWT (get_current_user) or a Mini App staff JWT (scope=staff-webapp)."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    if payload.get("scope") == "staff-webapp":
        return {"username": str(payload.get("sub")), "role": payload.get("role", "staff"), "source": "telegram-webapp"}
    username = payload.get("sub")
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def require_admin(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# --- Update last_login ---
def _update_last_login(username: str) -> None:
    try:
        from mysql_db import execute as _e
        _e("UPDATE dashboard_users SET last_login = NOW() WHERE username = %s", (username,))
    except Exception as e:
        logger.warning(f"Failed to update last_login: {e}")


# --- Routes ---

def register_auth_routes(app):

    @app.post("/auth/login", response_model=TokenResponse)
    async def login(request: LoginRequest):
        user = authenticate_user(request.username, request.password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        _update_last_login(user["username"])
        access_token = create_access_token(
            data={"sub": user["username"], "role": user["role"], "name": user["name"]}
        )
        refresh_token = create_refresh_token(data={"sub": user["username"]})
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user={
                "username": user["username"],
                "role": user["role"],
                "name": user["name"],
                "photo_url": _photo_url_for(user["username"])
            }
        )

    @app.post("/auth/refresh", response_model=TokenResponse)
    async def refresh(request: RefreshRequest):
        payload = decode_token(request.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        username = payload.get("sub")
        user = get_user(username)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        access_token = create_access_token(
            data={"sub": user["username"], "role": user["role"], "name": user["name"]}
        )
        new_refresh = create_refresh_token(data={"sub": user["username"]})
        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh,
            user={
                "username": user["username"],
                "role": user["role"],
                "name": user["name"],
                "photo_url": _photo_url_for(user["username"])
            }
        )

    @app.get("/auth/me")
    async def get_me(user: dict = Depends(get_current_user)):
        return {
            "username": user["username"],
            "role": user["role"],
            "name": user["name"],
            "photo_url": _photo_url_for(user["username"])
        }

    # ── Owner/staff profile (owner 2026-09-20 17:22: change password + upload picture) ──
    @app.post("/auth/change-password")
    async def change_password(body: dict = {}, user: dict = Depends(get_current_user)):
        from mysql_db import query_one as _q1, execute as _me
        cur = str((body or {}).get("current_password") or "")
        new = str((body or {}).get("new_password") or "")
        if len(new) < 6:
            raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
        row = _q1("SELECT password_hash FROM dashboard_users WHERE username = %s", (user["username"],))
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if not verify_password(cur, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is wrong")
        _me("UPDATE dashboard_users SET password_hash = %s WHERE username = %s", (hash_password(new), user["username"]))
        return {"success": True}

    # ── Preflight-free profile endpoints (owner 2026-09-20 17:44: the app's POST to /auth/*
    #    rejected at the device with a network error while everything else worked). These live
    #    under /api and accept the JWT in the BODY/FORM instead of the Authorization header, so
    #    the browser sends a CORS-simple request (text/plain or multipart) with no preflight.
    def _user_from_any(auth_header: str, token: str) -> dict:
        tk = str(token or "").strip()
        if not tk and auth_header and str(auth_header).lower().startswith("bearer "):
            tk = str(auth_header)[7:].strip()
        if not tk:
            raise HTTPException(status_code=401, detail="Not authenticated")
        payload = decode_token(tk)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        uname = payload.get("sub")
        row = get_user(uname)
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        return {**row, "username": uname}

    @app.post("/api/profile/password")
    async def api_profile_password(request: Request):
        # Body is parsed manually so the client can use a CORS-simple content type
        # (text/plain) and avoid the preflight that the phone was failing on.
        import json as _json
        auth_header = request.headers.get("authorization", "")
        try:
            raw = (await request.body()) or b""
        except Exception:
            raw = b""
        body = {}
        if raw:
            try:
                body = _json.loads(raw.decode("utf-8", "ignore")) or {}
            except Exception:
                try:
                    from urllib.parse import parse_qs
                    q = parse_qs(raw.decode("utf-8", "ignore"))
                    body = {k: (v[0] if isinstance(v, list) and v else v) for k, v in q.items()}
                except Exception:
                    body = {}
        if not isinstance(body, dict):
            body = {}
        user = _user_from_any(auth_header, body.get("token", ""))
        from mysql_db import query_one as _q1, execute as _me
        cur = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")
        if len(new) < 6:
            raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
        row = _q1("SELECT password_hash FROM dashboard_users WHERE username = %s", (user["username"],))
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if not verify_password(cur, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is wrong")
        _me("UPDATE dashboard_users SET password_hash = %s WHERE username = %s", (hash_password(new), user["username"]))
        return {"success": True}

    @app.post("/api/profile/photo")
    async def api_profile_photo(request: Request, file: UploadFile = File(...), token: str = ""):
        auth_header = request.headers.get("authorization", "")
        user = _user_from_any(auth_header, token)
        from mysql_db import execute as _me
        import os as _os
        ext = _os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(status_code=400, detail="Only PNG/JPG/WEBP images are allowed")
        data = await file.read()
        if not data or len(data) > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image must be smaller than 2 MB")
        d = _os.path.join(_os.path.dirname(__file__), "uploads", "avatars")
        _os.makedirs(d, exist_ok=True)
        fn = str(user["username"]).replace("/", "_") + ext
        with open(_os.path.join(d, fn), "wb") as f:
            f.write(data)
        url = "/uploads/avatars/" + fn
        _me("UPDATE dashboard_users SET photo_url = %s WHERE username = %s", (url, user["username"]))
        return {"success": True, "photo_url": url}

    @app.post("/auth/profile/photo")
    async def upload_profile_photo(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
        from mysql_db import execute as _me
        import os as _os
        ext = _os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(status_code=400, detail="Only PNG/JPG/WEBP images are allowed")
        data = await file.read()
        if not data or len(data) > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image must be smaller than 2 MB")
        d = _os.path.join(_os.path.dirname(__file__), "uploads", "avatars")
        _os.makedirs(d, exist_ok=True)
        fn = str(user["username"]).replace("/", "_") + ext
        with open(_os.path.join(d, fn), "wb") as f:
            f.write(data)
        url = "/uploads/avatars/" + fn
        _me("UPDATE dashboard_users SET photo_url = %s WHERE username = %s", (url, user["username"]))
        return {"success": True, "photo_url": url}

    @app.get("/dashboard.html")
    async def serve_dashboard_html():
        from starlette.responses import FileResponse
        fpath = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
        if os.path.isfile(fpath):
            resp = FileResponse(fpath, media_type="text/html")
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
