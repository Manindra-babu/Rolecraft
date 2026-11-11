from typing import Optional

from pydantic import BaseModel, Field


class SubScoreExplanation(BaseModel):
    name: str
    score: float
    weight: float
    explanation: str
    matched_items: list[str] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)


class CompatibilityBreakdown(BaseModel):
    overall_score: float
    required_skills: SubScoreExplanation
    preferred_skills: SubScoreExplanation
    experience_relevance: SubScoreExplanation
    keyword_coverage: SubScoreExplanation
    project_relevance: SubScoreExplanation


class SkillAssessmentItem(BaseModel):
    skill_name: str
    category: str  # demonstrated | under_represented | github_only | needs_development | missing
    importance: str = "required"  # required | preferred
    evidence_summary: str
    confidence: float
    or_group: Optional[str] = None
    or_group_satisfied: bool = False


class LearningResource(BaseModel):
    title: str
    provider: str
    url: str = ""
    verified_url: bool = False
    note: str = ""


class LearningStep(BaseModel):
    id: str
    title: str
    description: str
    estimated_hours: int = 4


class LearningPlanDetail(BaseModel):
    id: Optional[str] = None
    skill_name: str
    category: str
    technologies: list[str] = Field(default_factory=list)
    ordered_steps: list[LearningStep] = Field(default_factory=list)
    resources: list[LearningResource] = Field(default_factory=list)
    project_ideas: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    how_to_demonstrate: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    user_confirmed_learning: bool = False
    confirmation_note: str = ""
