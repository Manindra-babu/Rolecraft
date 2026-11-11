from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_valid_gemini_key
from app.core.rate_limit import rate_limiter
from app.db.session import get_db
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.github import (
    AnalyzedRepo,
    GitHubAnalysisResponse,
    GitHubAnalyzeRequest,
    RepoRecommendation,
)
from app.services.github.client import analyze_github_profile, generate_repo_bullets
from app.services.github.scorer import rank_repos_for_job

router = APIRouter(prefix="/github", tags=["github"])


class RecommendRequest(BaseModel):
    github_analysis_id: str
    job_posting_id: str
    resume_file_id: str | None = None


class GenerateProjectBulletsRequest(BaseModel):
    github_analysis_id: str
    repo_name: str


@router.post("/analyze", response_model=GitHubAnalysisResponse)
def analyze_github_endpoint(
    payload: GitHubAnalyzeRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> GitHubAnalysisResponse:
    rate_limiter.check(f"github:{current_user.id}", max_requests=15, window_seconds=60)
    try:
        return analyze_github_profile(
            payload.profile_url_or_username,
            other_links=payload.other_links,
            db=db,
            user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None


@router.post("/recommendations", response_model=list[RepoRecommendation])
def recommend_repos_endpoint(
    payload: RecommendRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RepoRecommendation]:
    analysis = (
        db.query(GitHubAnalysis)
        .filter(
            GitHubAnalysis.id == payload.github_analysis_id,
            GitHubAnalysis.user_id == current_user.id,
        )
        .first()
    )
    if not analysis:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GitHub analysis not found.")

    job = (
        db.query(JobPosting)
        .filter(JobPosting.id == payload.job_posting_id, JobPosting.user_id == current_user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    resume_text = ""
    if payload.resume_file_id:
        rf = (
            db.query(ResumeFile)
            .filter(ResumeFile.id == payload.resume_file_id, ResumeFile.user_id == current_user.id)
            .first()
        )
        if rf:
            resume_text = rf.raw_text
    if not resume_text:
        profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
        if profile:
            resume_text = " ".join(str(p.get("name", "")) for p in (profile.projects or []))

    repos = [AnalyzedRepo.model_validate(r) for r in analysis.repos_data]
    return rank_repos_for_job(
        repos,
        job_skills=list(job.required_skills) + list(job.preferred_skills),
        job_keywords=list(job.keywords),
        resume_text=resume_text,
    )


@router.post("/project-draft", response_model=list[str])
def draft_repo_bullets_endpoint(
    payload: GenerateProjectBulletsRequest,
    current_user: User = Depends(require_valid_gemini_key),
    db: Session = Depends(get_db),
) -> list[str]:
    analysis = (
        db.query(GitHubAnalysis)
        .filter(
            GitHubAnalysis.id == payload.github_analysis_id,
            GitHubAnalysis.user_id == current_user.id,
        )
        .first()
    )
    if not analysis:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GitHub analysis not found.")

    repos = [AnalyzedRepo.model_validate(r) for r in analysis.repos_data]
    target = next((r for r in repos if r.name == payload.repo_name), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found.")
    return generate_repo_bullets(target, db=db, user=current_user)
