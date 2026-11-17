import fitz
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.application import Application, Change, ResumeVersion
from app.models.job import JobPosting
from app.models.resume import ResumeFile
from app.models.user import User
from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation
from app.schemas.job import StructuredJobFields
from app.services.generation.optimizer import (
    check_section_item_loss_guard,
    generate_tailored_changes,
)
from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
from app.services.resume.pdf_parser import parse_pdf_resume
from tests.test_round11_verification import _make_manindra_repos

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANINDRA_PDF = FIXTURES / "resumes/manindra_resume.pdf"


def test_round14_certifications_never_trimmed_all_preserved_in_order(tmp_path: Path):
    """
    Requirement 1: Certifications must NEVER be removed or trimmed automatically.
    Always keep all of them (CISCO, RedHat x3, IBM, Microsoft x2) in the same order.
    """
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    assert len(parsed.certifications) == 4
    assert any("cisco" in c.lower() for c in parsed.certifications)
    assert any("redhat" in c.lower() for c in parsed.certifications)
    assert any("ibm" in c.lower() for c in parsed.certifications)
    assert any("microsoft" in c.lower() for c in parsed.certifications)

    job = StructuredJobFields(
        title="Full Stack Software Engineer",
        company="Stripe",
        required_skills=["React", "FastAPI", "Python", "JavaScript", "SQL"],
        preferred_skills=[],
        responsibilities=[],
        keywords=["React", "FastAPI"],
    )

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        page_overflows=False,
    )

    # 1. Zero certification trim changes proposed
    cert_trims = [c for c in changes if c.get("section") == "certifications" and c.get("change_type") == "relevance_trim"]
    assert len(cert_trims) == 0, f"Certifications must never have trim changes proposed: {cert_trims}"

    # 2. When exported, all 4 certifications appear in the output PDF text in exact order
    out_pdf = tmp_path / "cert_test.pdf"
    for c in changes:
        c["status"] = "accepted"

    res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        resume_structure=parsed,
        approved_changes=changes,
        tighten_spacing=True,
    )
    assert res.page_count_output == 1

    doc = fitz.open(str(out_pdf))
    out_text = doc[0].get_text("text")
    doc.close()

    assert "CISCO - C Essentials 1" in out_text
    assert "RedHat - Red Hat OpenShift" in out_text
    assert "IBM - Python for Data Science" in out_text
    assert "Microsoft - Excel Associate" in out_text

    # Order verification
    cisco_idx = out_text.index("CISCO")
    redhat_idx = out_text.index("RedHat")
    ibm_idx = out_text.index("IBM")
    ms_idx = out_text.index("Microsoft")
    assert cisco_idx < redhat_idx < ibm_idx < ms_idx, "Certifications order must be preserved!"


def test_round14_education_bullets_not_trimmed_without_overflow(tmp_path: Path):
    """
    Requirement 2: Education bullets were removed without notice.
    Only trim content when the page overflows, and list every removed line in the diff report.
    """
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = StructuredJobFields(
        title="Software Engineer",
        company="TechCorp",
        required_skills=["Python", "React", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=[],
    )

    # Case A: Normal generation when resume fits on 1 page (page_overflows=False)
    changes_normal = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        page_overflows=False,
    )
    edu_trims_normal = [c for c in changes_normal if c.get("section") == "education" and c.get("change_type") == "relevance_trim"]
    assert len(edu_trims_normal) == 0, f"No education bullets should be trimmed when page does not overflow: {edu_trims_normal}"

    out_pdf = tmp_path / "edu_test.pdf"
    for c in changes_normal:
        c["status"] = "accepted"

    res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        resume_structure=parsed,
        approved_changes=changes_normal,
        tighten_spacing=True,
    )
    assert res.page_count_output == 1

    doc = fitz.open(str(out_pdf))
    out_text = doc[0].get_text("text")
    doc.close()

    assert "weekly technical workshops" in out_text.lower()
    assert "google-hosted hackathon" in out_text.lower()
    assert "technical documentation" in out_text.lower()

    # Case B: When page overflows, trim suggestions are listed explicitly with status='pending'
    changes_overflow = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        page_overflows=True,
    )
    edu_trims_overflow = [c for c in changes_overflow if c.get("section") == "education" and c.get("change_type") == "relevance_trim"]
    assert len(edu_trims_overflow) > 0, "Trims should be proposed when page overflows"
    for et in edu_trims_overflow:
        assert et["status"] == "pending", f"Trims must be pending for user approval: {et}"
        assert et["original_text"], f"Every trimmed line must be listed: {et}"
        assert "diff report" in et["reason"].lower() or "approval" in et["reason"].lower()


def test_round14_guard_warns_and_blocks_export_if_certifications_lost(client: TestClient, db_session: Session):
    """
    Requirement 5: Add a guard: after generation, compare the tailored resume against
    the original and warn if any section lost items (certifications, education, experience), before exporting.
    """
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)

    # 1. Guard detects removed certifications
    simulated_changes_lost_cert = [
        {
            "section": "certifications",
            "change_type": "relevance_trim",
            "original_text": "CISCO - C Essentials 1",
            "new_text": "",
            "status": "accepted",
        }
    ]
    warns = check_section_item_loss_guard(resume=parsed, changes=simulated_changes_lost_cert)
    assert any("certifications" in w for w in warns), warns
    assert any("lost 1 item" in w for w in warns), warns

    # 2. Guard detects removed education
    simulated_changes_lost_edu = [
        {
            "section": "education",
            "change_type": "relevance_trim",
            "original_text": "Participated in weekly technical workshops...",
            "new_text": "",
            "status": "accepted",
        }
    ]
    warns_edu = check_section_item_loss_guard(resume=parsed, changes=simulated_changes_lost_edu)
    assert any("education" in w for w in warns_edu), warns_edu

    # 3. Guard detects removed experience
    simulated_changes_lost_exp = [
        {
            "section": "experience",
            "change_type": "relevance_trim",
            "original_text": "Built and deployed responsive full-stack web applications...",
            "new_text": "",
            "status": "accepted",
        }
    ]
    warns_exp = check_section_item_loss_guard(resume=parsed, changes=simulated_changes_lost_exp)
    assert any("experience" in w for w in warns_exp), warns_exp

    # 4. Guard passes with 0 warnings on valid changes
    valid_changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=StructuredJobFields(title="Developer", company="Corp", required_skills=["Python"], preferred_skills=[], responsibilities=[], keywords=[]),
        page_overflows=False,
    )
    clean_warns = check_section_item_loss_guard(resume=parsed, changes=valid_changes)
    assert len(clean_warns) == 0, f"Expected 0 guard warnings on clean run, got: {clean_warns}"
