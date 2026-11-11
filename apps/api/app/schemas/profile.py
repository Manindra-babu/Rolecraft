from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class GeminiKeySaveRequest(BaseModel):
    api_key: str = Field(min_length=10, max_length=256)


class GeminiKeyStatusResponse(BaseModel):
    has_key: bool
    status: str  # missing | valid | needs_attention
    masked_key: Optional[str] = None
    added_at: Optional[datetime] = None
    calls_month: int = 0
    calls_today: int = 0
    daily_limit: int = 100
    demo_mode: bool = False


class MasterProfileUpdate(BaseModel):
    full_name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=50)
    location: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=5000)
    links: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    confirmed_learning: list[dict[str, Any]] = Field(default_factory=list)


class MasterProfileResponse(MasterProfileUpdate):
    id: str
    user_id: str
    updated_at: datetime

    model_config = {"from_attributes": True}
