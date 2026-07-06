"""PS VIBE Dashboard — JWT Authentication Module (DB-backed)"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, status
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
            "SELECT username, password_hash, role, display_name FROM dashboard_users WHERE is_active = 1"
        )
        creds = {}
        for row in rows:
            creds[row["username"]] = {
                "password_hash": row["password_hash"],
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
                "name": user["name"]
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
                "name": user["name"]
            }
        )

    @app.get("/auth/me")
    async def get_me(user: dict = Depends(get_current_user)):
        return {
            "username": user["username"],
            "role": user["role"],
            "name": user["name"]
        }

    @app.get("/dashboard.html")
    async def serve_dashboard_html():
        from starlette.responses import FileResponse
        fpath = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
        if os.path.isfile(fpath):
            return FileResponse(fpath, media_type="text/html")
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
