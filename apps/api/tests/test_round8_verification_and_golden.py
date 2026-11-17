import json
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

import fitz
import numpy as np
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.core.security import encrypt_api_key
from app.models.application import Application, Change, ResumeVersion
from app.models.github import GitHubAnalysis
from app.models.job import JobPosting
from app.models.resume import ResumeFile
from app.models.user import MasterProfile, User
from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals
from app.schemas.job import StructuredJobFields
from app.services.generation.optimizer import generate_tailored_changes
from app.services.github.scorer import rank_repos_for_job
from app.services.jobs.extractor import extract_job_from_html_fixture
from app.services.resume.docx_editor import (
    apply_changes_to_docx_in_place,
    convert_docx_to_pdf_and_preview,
)
from app.services.resume.docx_parser import parse_docx_resume
from app.services.resume.pdf_editor import PdfEditResult, apply_changes_to_pdf_in_place
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.resume.theme_verifier import (
    verify_exported_pdf_completeness,
    verify_pdf_theme_match,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden"

SAMPLE_REPO = AnalyzedRepo(
    name="API-Gateway-Demo",
    full_name="alexrivera/API-Gateway-Demo",
    html_url="https://github.com/alexrivera/API-Gateway-Demo",
    description="High-throughput REST API gateway built with Python and FastAPI",
    primary_language="Python",
    languages={"Python": 8000, "TypeScript": 2000},
    dependencies=["fastapi", "uvicorn", "redis", "pytest"],
    inferred_skills=["Python", "FastAPI", "Redis", "Docker"],
    readme_excerpt="FastAPI gateway service with Redis rate limiting and PostgreSQL persistence.",
    signals=RepoEvidenceSignals(
        has_readme=True,
        file_count=18,
        total_commits=14,
        user_commits=14,
        user_commit_share=1.0,
        has_tests=True,
    ),
)

DEFAULT_LEARNING = [{"skill": "AJAX"}, {"skill": "Web Services"}, {"skill": "SOA"}]


def _load_jobs() -> dict[str, StructuredJobFields]:
    stripe = extract_job_from_html_fixture(
        (FIXTURES / "jobs/greenhouse_job.html").read_text(encoding="utf-8"),
        "https://boards.greenhouse.io/stripe/jobs/4918201",
    )[1]
    notion = extract_job_from_html_fixture(
        (FIXTURES / "jobs/lever_job.html").read_text(encoding="utf-8"),
        "https://jobs.lever.co/notion/8c3e12f4",
    )[1]
    paypal_raw = json.loads((FIXTURES / "jobs/paypal_eightfold.json").read_text(encoding="utf-8"))
    paypal = StructuredJobFields(
        title=paypal_raw.get("title", ""),
        company=paypal_raw.get("company", ""),
        location=paypal_raw.get("location", ""),
        required_skills=list(paypal_raw.get("required_skills") or []),
        preferred_skills=list(paypal_raw.get("preferred_skills") or []),
        responsibilities=list(paypal_raw.get("responsibilities") or []),
        keywords=list(paypal_raw.get("keywords") or []),
    )
    return {"Stripe": stripe, "Notion": notion, "PayPal": paypal}


def _accepted_changes_for_resume(
    resume,
    raw_text: str,
    element_map: dict,
    job: StructuredJobFields,
) -> list[dict]:
    recs = rank_repos_for_job(
        [SAMPLE_REPO],
        job_skills=job.required_skills,
        job_keywords=job.keywords,
        resume_text=raw_text,
    )
    for rec in recs:
        rec.decision = "approved"
    changes = generate_tailored_changes(
        resume=resume,
        raw_resume_text=raw_text,
        element_map=element_map,
        job=job,
        github_repos=[SAMPLE_REPO],
        approved_github_recs=recs,
        confirmed_learning=DEFAULT_LEARNING,
    )
    return [dict(c, status="accepted") for c in changes if c.get("change_type") != "relevance_trim"]


def _verify_pdf_run(
    source_pdf: Path,
    output_pdf: Path,
    res: PdfEditResult,
    applied: list[dict],
) -> tuple[float, tuple[bool, list[str]]]:
    sim = verify_pdf_theme_match(
        original_pdf_path=source_pdf,
        output_pdf_path=output_pdf,
        changed_bboxes_by_page=res.changed_bboxes_by_page,
        band_shifts_by_page=res.band_shifts_by_page,
        strategy_used=res.strategy_used,
    )
    comp = verify_exported_pdf_completeness(
        original_pdf_path=source_pdf,
        output_pdf_path=output_pdf,
        applied_changes=applied,
    )
    return sim.unchanged_similarity_score, comp


def _docx_text_and_links(docx_bytes: bytes) -> tuple[str, set[str]]:
    doc = Document(BytesIO(docx_bytes))
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    uris = {rel.target_ref for rel in doc.part.rels.values() if rel.reltype == RT.HYPERLINK}
    return "\n".join(parts), uris


def _seed_export_version(
    client: TestClient,
    db_session: Session,
    *,
    email: str,
    resume_path: Path,
    job: StructuredJobFields,
) -> tuple[dict[str, str], str, str, list[dict]]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Round8 Tester"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    user_id = reg.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    user = db_session.query(User).filter(User.id == user_id).first()
    assert user is not None
    user.encrypted_gemini_key = encrypt_api_key("test-gemini-key")
    user.gemini_key_status = "valid"
    db_session.add(user)

    mime = (
        "application/pdf"
        if resume_path.suffix.lower() == ".pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    up = client.post(
        "/api/v1/resumes/upload",
        headers=headers,
        files={"file": (resume_path.name, resume_path.read_bytes(), mime)},
    )
    assert up.status_code == 201, up.text
    resume_id = up.json()["id"]

    resume_row = db_session.query(ResumeFile).filter(ResumeFile.id == resume_id).first()
    assert resume_row is not None

    job_row = JobPosting(
        user_id=user_id,
        url="https://example.com/jobs/round8",
        company=job.company or "TargetCo",
        title=job.title or "Software Engineer Intern",
        raw_text=f"Required: {', '.join(job.required_skills)}. Keywords: {', '.join(job.keywords)}.",
        required_skills=list(job.required_skills),
        preferred_skills=list(job.preferred_skills),
        responsibilities=list(job.responsibilities),
        keywords=list(job.keywords),
    )
    db_session.add(job_row)

    gh = GitHubAnalysis(
        user_id=user_id,
        github_username="alexrivera",
        profile_url="https://github.com/alexrivera",
        repos_data=[SAMPLE_REPO.model_dump(mode="json")],
        detected_skills=["Python", "FastAPI", "Redis", "Docker"],
    )
    db_session.add(gh)

    profile = db_session.query(MasterProfile).filter(MasterProfile.user_id == user_id).first()
    if profile is None:
        profile = MasterProfile(user_id=user_id, confirmed_learning=DEFAULT_LEARNING)
    else:
        profile.confirmed_learning = DEFAULT_LEARNING
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(job_row)
    db_session.refresh(gh)

    if resume_path.suffix.lower() == ".pdf":
        parsed, el_map, raw_text, _ = parse_pdf_resume(resume_path)
    else:
        parsed, el_map, raw_text, _, _, _ = parse_docx_resume(resume_path)

    applied = _accepted_changes_for_resume(parsed, raw_text, el_map, job)
    for idx, chg in enumerate(applied, start=1):
        chg.setdefault("id", f"r8-chg-{idx}")

    app_row = Application(
        user_id=user_id,
        resume_file_id=resume_id,
        job_posting_id=job_row.id,
        github_analysis_id=gh.id,
        github_recommendations=[],
    )
    db_session.add(app_row)
    db_session.commit()
    db_session.refresh(app_row)

    ver_row = ResumeVersion(
        application_id=app_row.id,
        version_number=1,
    )
    db_session.add(ver_row)
    db_session.commit()
    db_session.refresh(ver_row)

    for c in applied:
        db_session.add(
            Change(
                resume_version_id=ver_row.id,
                section=c["section"],
                change_type=c["change_type"],
                target_location=dict(c.get("target_location") or {}),
                original_text=c.get("original_text") or "",
                new_text=c.get("new_text") or "",
                edited_text=c.get("edited_text"),
                reason=c.get("reason") or "",
                evidence_refs=list(c.get("evidence_refs") or []),
                validation_status=c.get("validation_status") or "passed",
                validation_warnings=list(c.get("validation_warnings") or []),
                status="accepted",
            )
        )
    db_session.commit()
    return headers, app_row.id, ver_row.id, applied


def _norm_pdf_text(text: str) -> str:
    return text.replace("\u2010", "-").replace("\xa0", " ")


def test_round8_pdf_custom_gpt_typos_exact(tmp_path: Path) -> None:
    jobs = _load_jobs()
    src = FIXTURES / "resumes/manindra_resume.pdf"
    parsed, el_map, raw_text, _ = parse_pdf_resume(src)
    generated = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=el_map,
        job=jobs["PayPal"],
        confirmed_learning=[],
    )
    changes = [
        dict(c, status="accepted")
        for c in generated
        if c.get("section") == "projects"
        and (
            "casual masking" in (c.get("original_text") or "")
            or "constrains" in (c.get("original_text") or "")
        )
    ]
    assert len(changes) == 2, f"Expected both Custom GPT typo fixes, got: {changes}"

    out_pdf = tmp_path / "manindra_typos.pdf"
    res = apply_changes_to_pdf_in_place(
        source_pdf_path=src,
        output_pdf_path=out_pdf,
        resume_structure=parsed,
        approved_changes=changes,
    )
    sim, comp = _verify_pdf_run(src, out_pdf, res, changes)
    doc = fitz.open(str(out_pdf))
    full_text = _norm_pdf_text(doc[0].get_text("text"))
    doc.close()

    assert "causal masking" in full_text
    assert "hardware constraints" in full_text
    assert "casual masking" not in full_text
    assert "hardware constrains" not in full_text
    assert res.overlap_passed is True, res.overlap_details
    assert comp == (True, []), comp
    assert sim >= 99.0


def test_round8_multi_format_and_sidebar_pipeline(tmp_path: Path) -> None:
    jobs = _load_jobs()

    # 1. Single-column PDF + two job URLs (Stripe, Notion)
    single_src = FIXTURES / "resumes/sample_resume.pdf"
    single_st, single_el, single_raw, _ = parse_pdf_resume(single_src)
    single_zero = tmp_path / "single_zero.pdf"
    single_z_res = apply_changes_to_pdf_in_place(
        source_pdf_path=single_src,
        output_pdf_path=single_zero,
        resume_structure=single_st,
        approved_changes=[],
    )
    sz_sim, sz_comp = _verify_pdf_run(single_src, single_zero, single_z_res, [])
    assert sz_sim >= 99.5
    assert single_z_res.overlap_passed is True
    assert sz_comp == (True, [])
    assert single_z_res.page_count_output == 1

    for jname in ("Stripe", "Notion"):
        app = _accepted_changes_for_resume(single_st, single_raw, single_el, jobs[jname])
        out_p = tmp_path / f"single_{jname}.pdf"
        res = apply_changes_to_pdf_in_place(
            source_pdf_path=single_src,
            output_pdf_path=out_p,
            resume_structure=single_st,
            approved_changes=app,
        )
        sim, comp = _verify_pdf_run(single_src, out_p, res, app)
        assert sim >= 98.0
        assert res.overlap_passed is True, res.overlap_details
        assert comp == (True, []), comp
        assert res.page_fit["margin_pt"] >= 12.0
        assert res.page_count_output == 1

    # 2. Sidebar Two-Column PDF (Alex Rivera) + Stripe & Notion
    side_src = FIXTURES / "resumes/sample_two_column.pdf"
    side_st, side_el, side_raw, _ = parse_pdf_resume(side_src)
    side_zero = tmp_path / "sidebar_zero.pdf"
    side_z_res = apply_changes_to_pdf_in_place(
        source_pdf_path=side_src,
        output_pdf_path=side_zero,
        resume_structure=side_st,
        approved_changes=[],
    )
    sdz_sim, sdz_comp = _verify_pdf_run(side_src, side_zero, side_z_res, [])
    assert sdz_sim >= 99.5
    assert side_z_res.overlap_passed is True
    assert sdz_comp == (True, [])

    orig_doc = fitz.open(str(side_src))
    orig_lines = [
        (ln["bbox"], "".join(s["text"] for s in ln["spans"]).strip())
        for blk in orig_doc[0].get_text("dict")["blocks"]
        if blk["type"] == 0
        for ln in blk["lines"]
    ]
    orig_edu_y = next(b[1] for b, t in orig_lines if t == "EDUCATION")
    orig_cert_y = next(b[1] for b, t in orig_lines if t == "CERTIFICATIONS")
    orig_taskflow_y = next(b[1] for b, t in orig_lines if "TASKFLOW - GITHUB" in t)
    orig_doc.close()

    for jname in ("Stripe", "Notion"):
        app = _accepted_changes_for_resume(side_st, side_raw, side_el, jobs[jname])
        out_p = tmp_path / f"sidebar_{jname}.pdf"
        res = apply_changes_to_pdf_in_place(
            source_pdf_path=side_src,
            output_pdf_path=out_p,
            resume_structure=side_st,
            approved_changes=app,
        )
        sim, comp = _verify_pdf_run(side_src, out_p, res, app)
        assert sim >= 98.0
        assert res.overlap_passed is True, res.overlap_details
        assert comp == (True, []), comp
        assert res.page_fit["margin_pt"] >= 12.0
        assert res.page_count_output == 1

        doc = fitz.open(str(out_p))
        txt = _norm_pdf_text(doc[0].get_text("text"))
        out_lines = [
            (ln["bbox"], _norm_pdf_text("".join(s["text"] for s in ln["spans"])).strip())
            for blk in doc[0].get_text("dict")["blocks"]
            if blk["type"] == 0
            for ln in blk["lines"]
        ]
        uris = {lnk.get("uri") for lnk in doc[0].get_links() if lnk.get("uri")}
        doc.close()

        assert sum(1 for ln in txt.splitlines() if ln.strip().upper() == "PROJECTS") == 1
        cur_lines = [(b, t) for b, t in out_lines if "Currently Learning" in t or "AJAX" in t]
        assert cur_lines, "Currently Learning missing from sidebar"
        assert all(b[2] <= 205.0 for b, _ in cur_lines), f"Sidebar overflowed divider x=210: {cur_lines}"

        new_edu_y = next(b[1] for b, t in out_lines if t == "EDUCATION")
        new_cert_y = next(b[1] for b, t in out_lines if t == "CERTIFICATIONS")
        assert new_edu_y > orig_edu_y and new_cert_y > orig_cert_y

        new_gw_lines = [(b, t) for b, t in out_lines if "API-GATEWAY-DEMO - GITHUB" in t]
        assert new_gw_lines, f"Added project missing in NAME - GITHUB format: {out_lines}"
        gw_bbox, _ = new_gw_lines[0]
        assert gw_bbox[0] >= 215.0 and gw_bbox[1] > orig_taskflow_y
        assert any("API-Gateway-Demo" in t and b[0] >= 215.0 and b[1] > gw_bbox[1] for b, t in out_lines)
        assert "https://github.com/alexrivera" in uris
        assert "https://github.com/alexrivera/taskflow" in uris
        assert SAMPLE_REPO.html_url in uris

    # Verify rewritten '- ' bullet in sidebar Experience preserves leading '- '
    exp_edit = [
        {
            "id": "r8-side-exp-bullet",
            "section": "experience",
            "change_type": "reworded",
            "original_text": "Built backend Python and PostgreSQL data services for telemetry ingestion.",
            "new_text": "Engineered backend Python, FastAPI, and PostgreSQL data services for telemetry ingestion.",
            "status": "accepted",
        }
    ]
    side_bullet_pdf = tmp_path / "sidebar_bullet_edit.pdf"
    sb_res = apply_changes_to_pdf_in_place(
        source_pdf_path=side_src,
        output_pdf_path=side_bullet_pdf,
        resume_structure=side_st,
        approved_changes=exp_edit,
    )
    assert sb_res.overlap_passed is True
    sb_comp = verify_exported_pdf_completeness(
        original_pdf_path=side_src,
        output_pdf_path=side_bullet_pdf,
        applied_changes=exp_edit,
    )
    assert sb_comp == (True, []), sb_comp
    with fitz.open(str(side_bullet_pdf)) as sb_doc:
        sb_txt = _norm_pdf_text(sb_doc[0].get_text("text"))
    assert "- Engineered backend Python, FastAPI, and PostgreSQL data services" in sb_txt

    # 3. Word-made DOCX (manindra_resume.docx) across Stripe, Notion, PayPal
    dx_src = FIXTURES / "resumes/manindra_resume.docx"
    dx_st, dx_el, dx_raw, _, _, _ = parse_docx_resume(dx_src)
    dx_zero = tmp_path / "docx_zero.docx"
    assert apply_changes_to_docx_in_place(source_docx_path=dx_src, output_docx_path=dx_zero, approved_changes=[]) == []
    for jname in ("Stripe", "Notion", "PayPal"):
        app = _accepted_changes_for_resume(dx_st, dx_raw, dx_el, jobs[jname])
        dx_out = tmp_path / f"docx_{jname}.docx"
        warnings = apply_changes_to_docx_in_place(
            source_docx_path=dx_src,
            output_docx_path=dx_out,
            approved_changes=app,
        )
        assert warnings == []
        txt, uris = _docx_text_and_links(dx_out.read_bytes())
        assert "causal masking" in txt
        assert "hardware constraints" in txt
        assert "Currently Learning" in txt
        assert SAMPLE_REPO.html_url in uris

    # 4. Manindra Two-Column PDF zero-change + Stripe
    mp_src = FIXTURES / "resumes/manindra_resume.pdf"
    mp_st, mp_el, mp_raw, _ = parse_pdf_resume(mp_src)
    mp_zero = tmp_path / "manindra_zero.pdf"
    mp_z_res = apply_changes_to_pdf_in_place(
        source_pdf_path=mp_src,
        output_pdf_path=mp_zero,
        resume_structure=mp_st,
        approved_changes=[],
    )
    mz_sim, mz_comp = _verify_pdf_run(mp_src, mp_zero, mp_z_res, [])
    assert mz_sim >= 99.5 and mp_z_res.overlap_passed is True and mz_comp == (True, [])

    mp_app = _accepted_changes_for_resume(mp_st, mp_raw, mp_el, jobs["Stripe"])
    mp_out = tmp_path / "manindra_stripe.pdf"
    mp_res = apply_changes_to_pdf_in_place(
        source_pdf_path=mp_src,
        output_pdf_path=mp_out,
        resume_structure=mp_st,
        approved_changes=mp_app,
    )
    ma_sim, ma_comp = _verify_pdf_run(mp_src, mp_out, mp_res, mp_app)
    assert ma_sim >= 98.0
    assert mp_res.overlap_passed is True, mp_res.overlap_details
    assert ma_comp == (True, []), ma_comp
    assert mp_res.page_fit["margin_pt"] >= 12.0
    assert mp_res.page_count_output == 1


def test_round8_exports_match_preview_and_links(client: TestClient, db_session: Session) -> None:
    jobs = _load_jobs()
    cases = [
        (
            "r8-manindra-pdf@example.com",
            FIXTURES / "resumes/manindra_resume.pdf",
            jobs["PayPal"],
            {"github.com/Manindra-babu", "linkedin.com", SAMPLE_REPO.html_url},
            True,
        ),
        (
            "r8-sidebar-pdf@example.com",
            FIXTURES / "resumes/sample_two_column.pdf",
            jobs["Notion"],
            {"https://github.com/alexrivera", "https://github.com/alexrivera/taskflow", SAMPLE_REPO.html_url},
            True,
        ),
        (
            "r8-manindra-docx@example.com",
            FIXTURES / "resumes/manindra_resume.docx",
            jobs["Stripe"],
            {"github.com/Manindra-babu", "linkedin.com", SAMPLE_REPO.html_url},
            False,
        ),
    ]

    for email, rpath, job, expected_link_substrings, is_pdf_source in cases:
        headers, app_id, ver_id, _ = _seed_export_version(
            client, db_session, email=email, resume_path=rpath, job=job
        )
        exp = client.post(f"/api/v1/applications/{app_id}/versions/{ver_id}/export", headers=headers)
        assert exp.status_code == 200, f"{rpath.name}: {exp.text}"
        exp_data = exp.json()
        assert exp_data["export_blocked"] is False, exp_data

        pdf_resp = client.get(f"/api/v1/applications/{app_id}/versions/{ver_id}/download/pdf", headers=headers)
        docx_resp = client.get(f"/api/v1/applications/{app_id}/versions/{ver_id}/download/docx", headers=headers)
        prev_resp = client.get(f"/api/v1/applications/{app_id}/versions/{ver_id}/download/preview", headers=headers)
        assert pdf_resp.status_code == 200
        assert docx_resp.status_code == 200
        assert prev_resp.status_code == 200

        pdf_doc = fitz.open(stream=pdf_resp.content, filetype="pdf")
        prev_img = Image.open(BytesIO(prev_resp.content)).convert("RGB")
        pdf_uris = [lnk.get("uri") or "" for page in pdf_doc for lnk in page.get_links()]

        # Rendered PDF page 0 matches preview PNG scale & pixel layout
        scale = prev_img.width / float(pdf_doc[0].rect.width)
        p0_pix = pdf_doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        p0_img = Image.frombytes("RGB", [p0_pix.width, p0_pix.height], p0_pix.samples)
        assert abs(p0_img.width - prev_img.width) <= 2
        if is_pdf_source:
            assert pdf_doc.page_count == 1
            assert abs(p0_img.height - prev_img.height) <= 2
            crop_prev = prev_img.resize(p0_img.size) if prev_img.size != p0_img.size else prev_img
            diff = np.abs(np.asarray(p0_img, dtype=np.int16) - np.asarray(crop_prev, dtype=np.int16)).max(axis=2)
            assert float((diff <= 5).mean()) >= 0.999
            for sub in expected_link_substrings:
                assert any(sub in uri for uri in pdf_uris), f"Missing PDF link {sub} in {pdf_uris}"
        else:
            # DOCX preview reflects the fallback PyMuPDF render when LibreOffice is absent
            assert p0_img.height <= prev_img.height + 2
        pdf_doc.close()

        # Verify DOCX export preserves content and hyperlinks
        docx_txt, docx_uris = _docx_text_and_links(docx_resp.content)
        assert "API-Gateway-Demo" in docx_txt or "API-GATEWAY-DEMO" in docx_txt
        for sub in expected_link_substrings:
            assert any(sub in uri for uri in docx_uris), f"Missing DOCX link {sub} in {docx_uris}"


def _compare_against_golden(actual_png: Path, golden_png: Path) -> tuple[float, float]:
    img_a = Image.open(actual_png).convert("RGB")
    img_g = Image.open(golden_png).convert("RGB")
    assert img_a.size == img_g.size, f"Size mismatch vs {golden_png.name}: {img_a.size} != {img_g.size}"
    arr_a = np.asarray(img_a, dtype=np.int16)
    arr_g = np.asarray(img_g, dtype=np.int16)
    max_diff = np.abs(arr_a - arr_g).max(axis=2)
    pixel_match = float((max_diff <= 8).mean())
    fg_mask = (arr_g.min(axis=2) < 245) | (arr_a.min(axis=2) < 245)
    fg_match = float((max_diff[fg_mask] <= 12).mean()) if fg_mask.any() else 1.0
    return pixel_match, fg_match


def test_round8_golden_image_regression(tmp_path: Path) -> None:
    jobs = _load_jobs()
    for name, pdf_file, job in (
        ("manindra", FIXTURES / "resumes/manindra_resume.pdf", jobs["PayPal"]),
        ("single", FIXTURES / "resumes/sample_resume.pdf", jobs["Stripe"]),
        ("sidebar", FIXTURES / "resumes/sample_two_column.pdf", jobs["Notion"]),
    ):
        st, el, raw, _ = parse_pdf_resume(pdf_file)

        z_pdf = tmp_path / f"{name}_z.pdf"
        apply_changes_to_pdf_in_place(
            source_pdf_path=pdf_file,
            output_pdf_path=z_pdf,
            resume_structure=st,
            approved_changes=[],
        )
        z_png = tmp_path / f"{name}_z.png"
        with fitz.open(str(z_pdf)) as doc:
            doc[0].get_pixmap(dpi=120, alpha=False).save(str(z_png))
        px_z, fg_z = _compare_against_golden(z_png, GOLDEN_DIR / f"{name}_zero.png")
        assert px_z >= 0.985 and fg_z >= 0.98, f"{name}_zero shifted: px={px_z:.4f}, fg={fg_z:.4f}"

        app = _accepted_changes_for_resume(st, raw, el, job)
        a_pdf = tmp_path / f"{name}_a.pdf"
        apply_changes_to_pdf_in_place(
            source_pdf_path=pdf_file,
            output_pdf_path=a_pdf,
            resume_structure=st,
            approved_changes=app,
        )
        a_png = tmp_path / f"{name}_a.png"
        with fitz.open(str(a_pdf)) as doc:
            doc[0].get_pixmap(dpi=120, alpha=False).save(str(a_png))
        px_a, fg_a = _compare_against_golden(a_png, GOLDEN_DIR / f"{name}_accepted.png")
        assert px_a >= 0.985 and fg_a >= 0.98, f"{name}_accepted shifted: px={px_a:.4f}, fg={fg_a:.4f}"

    dx_in = FIXTURES / "resumes/manindra_resume.docx"
    dx_st, dx_el, dx_raw, _, _, _ = parse_docx_resume(dx_in)
    dx_z = tmp_path / "dx_z.docx"
    apply_changes_to_docx_in_place(source_docx_path=dx_in, output_docx_path=dx_z, approved_changes=[])
    dx_z_png = tmp_path / "dx_z.png"
    convert_docx_to_pdf_and_preview(dx_z, tmp_path / "dx_z.pdf", dx_z_png)
    px_dz, fg_dz = _compare_against_golden(dx_z_png, GOLDEN_DIR / "docx_zero.png")
    assert px_dz >= 0.985 and fg_dz >= 0.98, f"docx_zero shifted: px={px_dz:.4f}, fg={fg_dz:.4f}"

    dx_app = _accepted_changes_for_resume(dx_st, dx_raw, dx_el, jobs["Stripe"])
    dx_a = tmp_path / "dx_a.docx"
    apply_changes_to_docx_in_place(source_docx_path=dx_in, output_docx_path=dx_a, approved_changes=dx_app)
    dx_a_png = tmp_path / "dx_a.png"
    convert_docx_to_pdf_and_preview(dx_a, tmp_path / "dx_a.pdf", dx_a_png)
    px_da, fg_da = _compare_against_golden(dx_a_png, GOLDEN_DIR / "docx_accepted.png")
    assert px_da >= 0.985 and fg_da >= 0.98, f"docx_accepted shifted: px={px_da:.4f}, fg={fg_da:.4f}"
