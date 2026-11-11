from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.application import Application
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.learning import LearningPlan
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User

router = APIRouter(prefix="/privacy", tags=["privacy"])


@router.get("/export")
def export_user_data(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Export all personal data stored for the authenticated user (excludes encrypted keys/passwords)."""
    profile = db.query(MasterProfile).filter(MasterProfile.user_id == current_user.id).first()
    resumes = db.query(ResumeFile).filter(ResumeFile.user_id == current_user.id).all()
    jobs = db.query(JobPosting).filter(JobPosting.user_id == current_user.id).all()
    gh = db.query(GitHubAnalysis).filter(GitHubAnalysis.user_id == current_user.id).all()
    apps = db.query(Application).filter(Application.user_id == current_user.id).all()
    plans = db.query(LearningPlan).filter(LearningPlan.user_id == current_user.id).all()

    return {
        "account": {
            "id": current_user.id,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "gemini_key_mask": current_user.gemini_key_mask,
            "created_at": current_user.created_at.isoformat(),
        },
        "master_profile": {
            "full_name": profile.full_name if profile else "",
            "email": profile.email if profile else "",
            "summary": profile.summary if profile else "",
            "skills": profile.skills if profile else [],
            "experience": profile.experience if profile else [],
            "projects": profile.projects if profile else [],
            "education": profile.education if profile else [],
            "confirmed_learning": profile.confirmed_learning if profile else [],
        },
        "resumes": [
            {"id": r.id, "filename": r.filename, "file_type": r.file_type, "layout_type": r.layout_type}
            for r in resumes
        ],
        "job_postings": [
            {"id": j.id, "url": j.url, "title": j.title, "company": j.company}
            for j in jobs
        ],
        "github_analyses": [
            {"id": g.id, "username": g.github_username, "repo_count": len(g.repos_data)}
            for g in gh
        ],
        "applications": [
            {
                "id": a.id,
                "status": a.status,
                "compatibility_before": a.compatibility_before,
                "compatibility_after": a.compatibility_after,
            }
            for a in apps
        ],
        "learning_plans": [
            {"id": p.id, "skill_name": p.skill_name, "category": p.category}
            for p in plans
        ],
    }


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account_and_all_data(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Permanently delete the user account, encrypted Gemini key, uploaded files, outputs, and all records."""
    settings = get_settings()

    # Delete user files on disk
    resumes = db.query(ResumeFile).filter(ResumeFile.user_id == current_user.id).all()
    for r in resumes:
        Path(r.storage_path).unlink(missing_ok=True)
    upload_user_dir = Path(settings.UPLOAD_DIR) / current_user.id
    if upload_user_dir.exists():
        shutil.rmtree(upload_user_dir, ignore_errors=True)

    output_user_dir = Path(settings.OUTPUT_DIR) / current_user.id
    if output_user_dir.exists():
        shutil.rmtree(output_user_dir, ignore_errors=True)

    # Delete dependent records without cascade relationship on User
    db.query(LearningPlan).filter(LearningPlan.user_id == current_user.id).delete()
    db.query(GitHubAnalysis).filter(GitHubAnalysis.user_id == current_user.id).delete()
    db.query(JobPosting).filter(JobPosting.user_id == current_user.id).delete()
    db.delete(current_user)
    db.commit()
