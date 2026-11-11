from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.github import RepoRecommendation
from app.schemas.matching import CompatibilityBreakdown, LearningPlanDetail, SkillAssessmentItem


class ApplicationCreateRequest(BaseModel):
    resume_file_id: str
    job_posting_id: str
    github_analysis_id: Optional[str] = None


class ApplicationStatusUpdateRequest(BaseModel):
    status: str = Field(pattern="^(Draft|Applied|Interview|Offer|Rejected)$")
    notes: str = Field(default="", max_length=2000)


class GitHubDecisionItem(BaseModel):
    repo_name: str
    decision: str = Field(pattern="^(approved|rejected|pending)$")


class GitHubDecisionsRequest(BaseModel):
    decisions: list[GitHubDecisionItem]


class ChangeDecisionRequest(BaseModel):
    status: str = Field(pattern="^(accepted|rejected|edited|pending)$")
    edited_text: Optional[str] = None


class ChangeResponse(BaseModel):
    id: str
    section: str
    change_type: str
    target_location: dict[str, Any]
    original_text: str
    new_text: str
    edited_text: Optional[str] = None
    reason: str
    evidence_refs: list[dict[str, Any]]
    validation_status: str
    validation_warnings: list[str]
    status: str

    model_config = {"from_attributes": True}


class ResumeVersionResponse(BaseModel):
    id: str
    version_number: int
    has_docx: bool = False
    has_pdf: bool = False
    has_preview: bool = False
    page_count_original: Optional[int] = None
    page_count_output: Optional[int] = None
    overflow_detected: bool = False
    overflow_suggestions: list[str] = Field(default_factory=list)
    changes: list[ChangeResponse] = Field(default_factory=list)
    created_at: datetime


class ApplicationSummaryResponse(BaseModel):
    id: str
    status: str
    job_title: str = ""
    company: str = ""
    resume_filename: str = ""
    compatibility_before: Optional[float] = None
    compatibility_after: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class ApplicationDetailResponse(BaseModel):
    id: str
    status: str
    notes: str
    resume_file_id: Optional[str] = None
    resume_filename: str = ""
    resume_file_type: str = "docx"
    job_posting_id: Optional[str] = None
    job_title: str = ""
    company: str = ""
    github_analysis_id: Optional[str] = None
    compatibility_before: Optional[float] = None
    compatibility_after: Optional[float] = None
    score_breakdown: Optional[CompatibilityBreakdown] = None
    skill_assessments: list[SkillAssessmentItem] = Field(default_factory=list)
    github_recommendations: list[RepoRecommendation] = Field(default_factory=list)
    learning_plans: list[LearningPlanDetail] = Field(default_factory=list)
    versions: list[ResumeVersionResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
