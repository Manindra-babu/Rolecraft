from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ElementLocation(BaseModel):
    kind: str  # body_paragraph | table_cell_paragraph | textbox_paragraph
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    style_name: str = ""
    is_bullet: bool = False
    section: str = "other"


class ParsedExperienceItem(BaseModel):
    company: str = ""
    title: str = ""
    dates: str = ""
    location: str = ""
    bullets: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)


class ParsedProjectItem(BaseModel):
    name: str = ""
    tech_stack: list[str] = Field(default_factory=list)
    url: str = ""
    bullets: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)


class ParsedEducationItem(BaseModel):
    institution: str = ""
    degree: str = ""
    dates: str = ""
    details: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)


class ParsedResumeStructure(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    education: list[ParsedEducationItem] = Field(default_factory=list)
    experience: list[ParsedExperienceItem] = Field(default_factory=list)
    projects: list[ParsedProjectItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    skill_categories: dict[str, list[str]] = Field(default_factory=dict)
    achievements: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    sections_order: list[str] = Field(default_factory=list)
    completeness_warnings: list[str] = Field(default_factory=list)


class ResumeUploadResponse(BaseModel):
    id: str
    filename: str
    file_type: str
    file_size: int
    layout_type: str
    layout_support: str  # full | partial | analysis_only
    format_notice: Optional[str] = None
    page_count: Optional[int] = None
    parsed_structure: ParsedResumeStructure
    element_count: int
    created_at: datetime


class ResumeConfirmRequest(BaseModel):
    parsed_structure: ParsedResumeStructure
