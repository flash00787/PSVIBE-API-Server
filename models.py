"""Pydantic response models for PS VIBE API Server."""
from pydantic import BaseModel
from typing import Optional, List, Any, Dict


class GameResponse(BaseModel):
    id: int
    name: str
    genre: str
    solo_multi: str
    disc_count: int
    consoles: List[str] = []
    status: Optional[str] = None


class ConfigResponse(BaseModel):
    base_rate: Any = None
    master_threshold: Any = None
    immortal_threshold: Any = None
    new_member_card_price: Any = None
    new_member_base_mins: Any = None
    console_multipliers: Any = None
    food_prices: Any = None
    food_costs: Any = None
    bonus_table: Any = None
    source: str = "mysql"


class MemberResponse(BaseModel):
    member_id: str
    name: str
    phone: Optional[str] = None
    tier: Optional[str] = None
    balance_mins: Optional[int] = 0


class BookingResponse(BaseModel):
    booking_id: Optional[int] = None
    console_id: Optional[str] = None
    member_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    status: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: Optional[float] = None
    services: Optional[Dict[str, str]] = None


class GenericResponse(BaseModel):
    mysql_connected: Optional[bool] = None
    data_source: Optional[str] = None
    success: bool = True
    message: Optional[str] = None
    data: Optional[Any] = None
    error: Optional[str] = None


class OkResponse(BaseModel):
    """Matches the ok() wrapper returned by most endpoints."""
    success: bool = True
    data: Any = None
    message: str = ""
