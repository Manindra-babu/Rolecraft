import json
import re
from pathlib import Path

import fitz
from docx import Document
from fastapi.testclient import TestClient

from app.schemas.job import StructuredJobFields
from app.schemas.resume import ParsedResumeStructure
from app.services.generation.optimizer import generate_tailored_changes
from app.services.generation.validator import validate_claim_against_evidence
from app.services.jobs.extractor import _enrich_fields_deterministically
from app.services.matching.normalizer import find_direct_and_implied_skills, normalize_skill
from app.services.matching.scorer import assess_skills_and_score
from app.services.resume.docx_editor import (
    apply_changes_to_docx_in_place,
    build_ats_docx_from_structure,
)
from app.services.resume.docx_parser import parse_docx_resume
from app.services.resume.pdf_editor import (
    PDF_REBUILD_NOTICE,
    apply_changes_to_pdf_in_place,
    build_style_cloned_pdf,
)
from app.services.resume.pdf_parser import parse_pdf_resume
from app.services.resume.section_parser import parse_lines_to_structure
from app.services.resume.theme_verifier import verify_pdf_theme_match

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MANINDRA_PDF = FIXTURES_DIR / "resumes" / "manindra_resume.pdf"
MANINDRA_DOCX = FIXTURES_DIR / "resumes" / "manindra_resume.docx"
PAYPAL_JOB_JSON = FIXTURES_DIR / "jobs" / "paypal_eightfold.json"


def _load_paypal_job() -> StructuredJobFields:
    data = json.loads(PAYPAL_JOB_JSON.read_text(encoding="utf-8"))
    seed = StructuredJobFields(
        title=data.get("title", ""),
        company=data.get("company", ""),
        location=data.get("location", ""),
        required_skills=list(data.get("required_skills") or []),
        preferred_skills=list(data.get("preferred_skills") or []),
        responsibilities=list(data.get("responsibilities") or []),
        or_groups=list(data.get("or_groups") or []),
        keywords=list(data.get("keywords") or []),
    )
    return _enrich_fields_deterministically(seed, data["raw_text"])


def test_item1_paypal_job_extraction_atomic_skills_and_or_groups() -> None:
    job = _load_paypal_job()
    all_skills = [s.lower() for s in (job.required_skills + job.preferred_skills)]

    for expected in ("http", "html", "dom", "ajax", "javascript", "css", "node.js", "python", "web services", "soa"):
        assert any(expected in s for s in all_skills), f"Missing expected atomic skill '{expected}' in {all_skills}"

    for bad_fragment in ("breaking", "rebuilding", "ajax)", "strong applied experience in building", "control paypal enables"):
        assert bad_fragment not in all_skills, f"Stray fragment '{bad_fragment}' leaked into skills: {all_skills}"

    assert len(job.responsibilities) >= 2
    assert any(
        {"node.js", "python"}.issubset({m.lower() for m in grp})
        for grp in job.or_groups
    ), f"Expected Node.js/Python OR group in {job.or_groups}"


def test_item2_evidence_matching_implications_and_categories() -> None:
    parsed, _, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # Implication map direct checks
    implied = find_direct_and_implied_skills("Built MERN application with Tailwind CSS and RESTful APIs using HTML5")
    for k in ("mongodb", "express", "react", "node.js", "javascript", "css", "rest", "http", "html"):
        assert k in implied, f"Missing implied skill {k} in {implied}"

    breakdown, assessments = assess_skills_and_score(
        resume=parsed,
        job_required_skills=job.required_skills,
        job_preferred_skills=job.preferred_skills,
        job_responsibilities=job.responsibilities,
        job_keywords=job.keywords,
        job_or_groups=job.or_groups,
        github_repos=[],
    )
    by_name = {normalize_skill(a.skill_name): a for a in assessments}

    for sk in ("python", "javascript", "css", "node.js", "rest", "html"):
        assert sk in by_name, f"Missing assessment for {sk}: {list(by_name.keys())}"
        assert by_name[sk].category == "demonstrated", (
            f"Expected '{sk}' in demonstrated, got {by_name[sk]}"
        )

    # Skills-list-only skills stay under_represented (insufficiently represented)
    for sk in ("java", "c++", "sql"):
        if sk in by_name:
            assert by_name[sk].category in {"under_represented", "demonstrated"}
            if sk == "sql":
                assert by_name[sk].category == "under_represented"

    # Possible adjacent skills never treated as demonstrated
    adjacent = [a for a in assessments if a.category == "needs_development"]
    for adj in adjacent:
        assert "adjacent" in adj.evidence_summary.lower() or "semantic" in adj.evidence_summary.lower()
        assert adj.category != "demonstrated"

    assert breakdown.overall_score >= 55.0


def test_item3_pdf_and_docx_parsing_completeness_and_cleanliness() -> None:
    pdf_parsed, pdf_elem, pdf_raw, _ = parse_pdf_resume(MANINDRA_PDF)
    docx_parsed, _, docx_raw, _, _, _ = parse_docx_resume(MANINDRA_DOCX)

    for label, p in (("PDF", pdf_parsed), ("DOCX", docx_parsed)):
        assert "MANINDRA" in p.full_name.upper(), f"{label} missing name: {p.full_name}"
        assert len(p.summary) >= 150, f"{label} dropped Summary"
        assert len(p.certifications) >= 3, f"{label} dropped Certifications: {p.certifications}"
        assert p.phone and p.phone not in p.location, f"{label} duplicated phone in location: {p.location}"
        assert any("linkedin.com" in (l.get("url") or "").lower() for l in p.links), f"{label} missing LinkedIn link"
        assert any("github.com" in (l.get("url") or "").lower() for l in p.links), f"{label} missing GitHub link"
        assert len(p.skill_categories) >= 4, f"{label} lost skill categories: {p.skill_categories}"
        assert not p.completeness_warnings, f"{label} completeness warnings non-empty: {p.completeness_warnings}"

        serialized = json.dumps(p.model_dump())
        assert "\uf0b7" not in serialized, f"{label} kept PUA glyph"
        assert "\u25af" not in serialized, f"{label} kept box glyph"
        for sk in p.skills:
            assert not sk.endswith("."), f"{label} left trailing period on skill '{sk}'"
        for cat, items in p.skill_categories.items():
            for it in items:
                assert not it.endswith("."), f"{label} left trailing period on category skill '{cat}: {it}'"

    # Completeness warning fires when a source section heading produces empty parsed output
    bad_parsed = parse_lines_to_structure(
        [
            {"id": "el_1", "text": "CERTIFICATIONS", "section": "certifications", "is_heading": True, "links": []},
            {"id": "el_2", "text": "\uf0b7", "section": "certifications", "is_heading": False, "links": []},
        ]
    )
    assert any("Certifications" in w for w in bad_parsed.completeness_warnings), bad_parsed.completeness_warnings


def test_item5_validator_integrity_and_implied_tech() -> None:
    parsed, _, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)

    # Allows true implied tech (MongoDB, Express.js, React, Node.js, REST)
    ok_res = validate_claim_against_evidence(
        new_text="Built scalable backend APIs using Node.js, Express.js, MongoDB, and REST.",
        original_text="Developed a full-stack application with MERN stack.",
        section="experience",
        resume=parsed,
        raw_resume_text=raw_text,
    )
    assert ok_res.status == "passed", ok_res.warnings

    # Blocks stray glyphs, duplicated prefix, missing category label, trailing period
    for bad_text, section, orig, expected_msg in [
        ("Python\uf0b7 FastAPI", "experience", "Built backend", "glyph"),
        ("Built services with \u25af box", "experience", "Built services", "glyph"),
        ("Detail- Detail-oriented engineer", "summary", "Detail-oriented", "duplicated"),
        ("C++, Python, Java", "skills", "Programming Languages: C++, Python, Java", "category"),
        ("Programming Languages: C++, Python.", "skills", "Programming Languages: C++, Python", "trailing period"),
    ]:
        res = validate_claim_against_evidence(
            new_text=bad_text,
            original_text=orig,
            section=section,
            resume=parsed,
            raw_resume_text=raw_text,
        )
        assert res.status == "blocked", f"Expected block for {bad_text}, got {res}"
        assert any(expected_msg in w.lower() for w in res.warnings), res.warnings


def test_item6_optimizer_typos_categories_and_no_auto_accept(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # Check optimizer directly (including typo fixes & category preservation)
    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[],
        approved_github_recs=[],
        confirmed_learning=[{"skill": "SOAP", "source": "official docs", "label": "Currently learning: SOAP (official docs)"}],
    )
    assert changes, "Expected tailored change proposals"
    assert all(ch["status"] == "pending" for ch in changes), "No change may be auto-accepted"

    skill_changes = [ch for ch in changes if ch["section"] == "skills"]
    for sc in skill_changes:
        orig_cat = sc["original_text"].split(":", 1)[0].strip()
        assert sc["new_text"].startswith(f"{orig_cat}:"), f"Lost category label in {sc}"

    combined_new = "\n".join(ch["new_text"] for ch in changes)
    # Typo fixes present when those typos exist in source PDF
    for fixed_tok in ("INTERN", "KNOWLEDGE", "RETRIEVAL", "causal", "constraints"):
        if re.search(r"INTER|KNOWLEDEGE|RETRIVAL|casual|constrains", raw_text):
            assert fixed_tok in combined_new, f"Expected typo fix '{fixed_tok}' in optimizer output"
    assert "Detail- Detail-" not in combined_new

    # User confirmed skill only included because confirmed_learning was passed
    assert any(ch["change_type"] == "user_confirmed_skill" for ch in changes)
    without_confirm = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[],
        approved_github_recs=[],
        confirmed_learning=[],
    )
    assert not any(ch["change_type"] == "user_confirmed_skill" for ch in without_confirm)

    # API test: GitHub auto-trigger + bulk-accept ONLY accepts reordered changes
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "batch@example.com", "full_name": "Manindra", "password": "StrongPassword123!"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    with MANINDRA_PDF.open("rb") as f:
        up = client.post(
            "/api/v1/resumes/upload",
            headers=headers,
            files={"file": ("manindra_resume.pdf", f, "application/pdf")},
        )
    assert up.status_code == 201, up.text

    job_resp = client.post(
        "/api/v1/jobs/extract-text",
        headers=headers,
        json={"raw_text": json.loads(PAYPAL_JOB_JSON.read_text(encoding="utf-8"))["raw_text"]},
    )
    assert job_resp.status_code == 200, job_resp.text

    app_resp = client.post(
        "/api/v1/applications",
        headers=headers,
        json={"resume_file_id": up.json()["id"], "job_posting_id": job_resp.json()["id"]},
    )
    assert app_resp.status_code == 201, app_resp.text
    app_data = app_resp.json()
    assert app_data["github_status"] and app_data["github_status"] != "no_github_link"

    gen = client.post(f"/api/v1/applications/{app_data['id']}/generate", headers=headers)
    assert gen.status_code == 200
    ver = gen.json()["versions"][0]
    assert all(c["status"] == "pending" for c in ver["changes"])

    bulk = client.post(
        f"/api/v1/applications/{app_data['id']}/versions/{ver['id']}/bulk-accept",
        headers=headers,
    )
    assert bulk.status_code == 200
    for c in bulk.json()["changes"]:
        if c["change_type"] != "reordered":
            assert c["status"] == "pending", f"Bulk accept touched non-reorder change: {c}"

    get_settings.cache_clear()


def test_strategy_b_c_d_pdf_theme_preservation_and_docx_fallback(tmp_path: Path) -> None:
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    tokens = elem_map["style_tokens"]

    # D.1: 0 accepted changes -> visually identical output (unchanged similarity ~100%)
    out_zero = tmp_path / "zero_changes.pdf"
    res_zero = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_zero,
        approved_changes=[],
        resume_structure=parsed,
    )
    assert res_zero.strategy_used == "inplace_exact"
    verify_zero = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_zero,
        changed_bboxes_by_page=res_zero.changed_bboxes_by_page,
        strategy_used=res_zero.strategy_used,
    )
    assert verify_zero.passed is True
    assert verify_zero.unchanged_similarity_score >= 99.9
    assert verify_zero.overall_similarity_score >= 99.9

    # D.2: 1 accepted text change -> ONLY edited bbox differs, links & drawings preserved
    edit_change = {
        "section": "summary",
        "change_type": "reworded",
        "target_location": {},
        "original_text": "software developer",
        "new_text": "full-stack developer",
        "edited_text": None,
    }
    # Use actual substring from first summary line or experience bullet if exact phrase differs
    if "software developer" not in raw_text.lower() and parsed.experience:
        orig_line = parsed.experience[0].bullets[0]
        edit_change = {
            "section": "experience",
            "change_type": "reworded",
            "target_location": {},
            "original_text": orig_line,
            "new_text": orig_line.replace("INTER", "INTERN").replace("casual", "causal"),
            "edited_text": None,
        }
    out_one = tmp_path / "one_change.pdf"
    res_one = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_one,
        approved_changes=[edit_change],
        resume_structure=parsed,
    )
    verify_one = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_one,
        changed_bboxes_by_page=res_one.changed_bboxes_by_page,
        strategy_used=res_one.strategy_used,
    )
    assert verify_one.passed is True, verify_one.blocked_reason
    assert verify_one.unchanged_similarity_score >= 97.0, verify_one
    assert verify_one.page_count_output == verify_one.page_count_original

    orig_doc = fitz.open(str(MANINDRA_PDF))
    out_doc = fitz.open(str(out_one))
    orig_uris = {l.get("uri") for l in orig_doc[0].get_links() if l.get("uri")}
    out_uris = {l.get("uri") for l in out_doc[0].get_links() if l.get("uri")}
    assert orig_uris <= out_uris, f"Lost hyperlinks: {orig_uris - out_uris}"
    assert "\u25af" not in out_doc[0].get_text("text")
    orig_doc.close()
    out_doc.close()

    # D.3: Mismatched layout gets BLOCKED by verifier
    bad_pdf = tmp_path / "broken.pdf"
    bad_doc = fitz.open()
    p = bad_doc.new_page()
    p.insert_text((72, 200), "Completely different plain text document")
    bad_doc.save(str(bad_pdf))
    bad_doc.close()
    verify_bad = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=bad_pdf,
        changed_bboxes_by_page={},
        strategy_used="inplace_pdf",
    )
    assert verify_bad.passed is False
    assert verify_bad.blocked_reason

    # Strategy C: Style-clone PDF fallback emits notice & preserves key tokens
    cloned_pdf = tmp_path / "cloned.pdf"
    cloned_res = build_style_cloned_pdf(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=cloned_pdf,
        resume_structure=parsed,
        approved_changes=[],
    )
    assert cloned_res.notice == PDF_REBUILD_NOTICE
    cdoc = fitz.open(str(cloned_pdf))
    ctext = cdoc[0].get_text("text")
    assert "Summary" in ctext or "SUMMARY" in ctext
    assert "Certifications" in ctext or "CERTIFICATIONS" in ctext
    cdoc.close()

    # Clean styled DOCX keeps Summary, Certifications, LinkedIn, category labels, no duplicate phone
    styled_docx = tmp_path / "styled.docx"
    build_ats_docx_from_structure(
        structure=parsed,
        approved_changes=[],
        output_docx_path=styled_docx,
        style_tokens=tokens,
    )
    ddoc = Document(str(styled_docx))
    dtext = "\n".join(para.text for para in ddoc.paragraphs)
    assert "SUMMARY" in dtext
    assert "CERTIFICATIONS" in dtext
    assert parsed.phone and dtext.count(parsed.phone) == 1
    assert any(cat in dtext for cat in parsed.skill_categories)
    assert "\u25af" not in dtext

    # Strategy A: In-place DOCX edit on manindra_resume.docx table layout
    if MANINDRA_DOCX.exists():
        docx_parsed, _, _, _, _, _ = parse_docx_resume(MANINDRA_DOCX)
        edited_docx = tmp_path / "edited_manindra.docx"
        if docx_parsed.experience and docx_parsed.experience[0].bullets:
            b0 = docx_parsed.experience[0].bullets[0]
            apply_changes_to_docx_in_place(
                source_docx_path=MANINDRA_DOCX,
                output_docx_path=edited_docx,
                approved_changes=[
                    {
                        "section": "experience",
                        "change_type": "reworded",
                        "target_location": {},
                        "original_text": b0,
                        "new_text": b0 + " (verified)",
                    }
                ],
            )
            assert edited_docx.exists()
