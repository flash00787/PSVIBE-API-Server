"""PS VIBE API Server — Pydantic Models"""
from typing import Optional, Any
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    sheets_connected: bool = True
    timestamp: str = ""


class SuccessResponse(BaseModel):
    success: bool = True
    data: Optional[Any] = None
    message: str = ""


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    detail: str = ""
