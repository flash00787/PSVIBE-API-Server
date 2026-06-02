"""PS VIBE Dashboard — JWT Authentication Module"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# --- Constants ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "psvibe-dashboard-secret-key-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE = timedelta(minutes=30)
REFRESH_TOKEN_EXPIRE = timedelta(days=7)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


# --- Credentials (Lazy-loaded) ---
_creds_cache = None

def _get_credentials():
    return {
        "admin": {
            "password_hash": pwd_context.hash("admin123"),
            "role": "admin",
            "name": "Admin"
        },
        "staff": {
            "password_hash": pwd_context.hash("staff123"),
            "role": "staff",
            "name": "Staff"
        }
    }

def get_credentials():
    global _creds_cache
    if _creds_cache is None:
        _creds_cache = _get_credentials()
    return _creds_cache


class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict

class RefreshRequest(BaseModel):
    refresh_token: str


# --- Core Functions ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_user(username: str) -> Optional[dict]:
    user = get_credentials().get(username)
    if user:
        return {**user, "username": username}
    return None

def authenticate_user(username: str, password: str) -> Optional[dict]:
    user = get_user(username)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or ACCESS_TOKEN_EXPIRE)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + REFRESH_TOKEN_EXPIRE
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
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

async def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


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
