from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class UserRegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    gemini_key_mask: Optional[str] = None
    gemini_key_status: str = "missing"
    gemini_key_added_at: Optional[datetime] = None
    gemini_calls_month: int = 0
    gemini_calls_today: int = 0
    daily_limit: int = 100
    demo_mode: bool = False
    google_oauth_enabled: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
