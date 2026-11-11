from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SkillRequirementItem(BaseModel):
    name: str
    importance: Literal["required", "preferred"] = "required"
    or_group: Optional[str] = None


class StructuredJobFields(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    experience_level: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    responsibilities_or_soft_requirements: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    skills: list[SkillRequirementItem] = Field(default_factory=list)
    or_groups: list[list[str]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class JobExtractUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=1000)


class JobExtractTextRequest(BaseModel):
    url: Optional[str] = Field(default=None, max_length=1000)
    raw_text: str = Field(min_length=40, max_length=60000)
    title_hint: Optional[str] = None
    company_hint: Optional[str] = None


class JobPostingUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=200)
    experience_level: str = Field(default="", max_length=100)
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    or_groups: list[list[str]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class JobPostingResponse(BaseModel):
    id: Optional[str] = None
    status: str = "extracted"  # extracted | fallback_required
    fallback_reason: Optional[str] = None
    url: Optional[str] = None
    normalized_url: Optional[str] = None
    extraction_method: str = "manual"
    confidence: float = 0.0
    title: str = ""
    company: str = ""
    location: str = ""
    experience_level: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    responsibilities_or_soft_requirements: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    skills: list[SkillRequirementItem] = Field(default_factory=list)
    or_groups: list[list[str]] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    raw_text: str = ""
    fetched_at: Optional[datetime] = None
    cached: bool = False
