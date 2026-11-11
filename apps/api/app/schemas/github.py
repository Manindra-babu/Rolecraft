from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RepoEvidenceSignals(BaseModel):
    has_tests: bool = False
    has_ci: bool = False
    has_dockerfile: bool = False
    has_readme: bool = False
    readme_length: int = 0
    file_count: int = 0
    total_commits: int = 0
    user_commits: int = 0
    user_commit_share: float = 0.0
    is_fork: bool = False
    is_empty: bool = False
    is_tutorial_like: bool = False


class AnalyzedRepo(BaseModel):
    name: str
    full_name: str
    html_url: str
    description: str = ""
    primary_language: str = ""
    languages: dict[str, int] = Field(default_factory=dict)
    topics: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    inferred_skills: list[str] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    commit_messages: list[str] = Field(default_factory=list)
    readme_excerpt: str = ""
    stars: int = 0
    pushed_at: Optional[str] = None
    signals: RepoEvidenceSignals = Field(default_factory=RepoEvidenceSignals)
    quality_score: float = 0.0  # 0 to 100
    quality_notes: list[str] = Field(default_factory=list)
    excluded_reason: Optional[str] = None


class DetectedGitHubSkill(BaseModel):
    skill: str
    repo_count: int
    main_language_count: int = 0
    evidence_level: str
    repos: list[str] = Field(default_factory=list)


class GitHubAnalyzeRequest(BaseModel):
    profile_url_or_username: str = Field(default="", max_length=300)
    other_links: list[str] = Field(default_factory=list)


class GitHubAnalysisResponse(BaseModel):
    id: Optional[str] = None
    skipped: bool = False
    github_username: str = ""
    profile_url: str = ""
    repos: list[AnalyzedRepo] = Field(default_factory=list)
    detected_skills: list[DetectedGitHubSkill] = Field(default_factory=list)
    other_links: list[str] = Field(default_factory=list)
    fetched_at: Optional[datetime] = None


class BulletEvidenceItem(BaseModel):
    bullet: str
    source: str
    detail: str


class RepoRecommendation(BaseModel):
    repo_name: str
    full_name: str
    html_url: str
    quality_score: float
    relevance_score: float
    matched_skills: list[str] = Field(default_factory=list)
    reason: str
    already_in_resume: bool = False
    draft_bullets: list[str] = Field(default_factory=list)
    bullet_evidence: list[BulletEvidenceItem] = Field(default_factory=list)
    decision: str = "pending"  # pending | approved | rejected
