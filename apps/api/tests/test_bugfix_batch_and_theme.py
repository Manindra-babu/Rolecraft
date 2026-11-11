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
from app.services.resume.theme_verifier import (
    verify_exported_pdf_completeness,
    verify_pdf_theme_match,
)

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


def test_round3_inplace_2col_github_grid_completeness_and_overflow(tmp_path: Path) -> None:
    from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # 1. Exact reading order of Education hackathon achievement (never swapped)
    assert parsed.education
    edu_details = " ".join(parsed.education[0].details)
    assert (
        "Top 10 Qualifier - Google-Hosted Hackathon: Cleared multiple screening levels "
        "demonstrating strong emergency response network system."
    ) in edu_details
    assert "Google-Hosted Hackathon: Cleared multiple screening levels demonstrating strong emergency — Top 10 Qualifier" not in edu_details

    # 2. Validator rejects leading commas and generic GitHub metadata filler
    bad_skill = validate_claim_against_evidence(
        new_text="Tools: , Microsoft Excel, Linux",
        original_text="Tools: Microsoft Excel, Linux",
        resume=parsed,
        raw_resume_text=raw_text,
        section="skills",
    )
    assert bad_skill.status == "blocked"
    bad_filler = validate_claim_against_evidence(
        new_text="Added automated test coverage to verify core module behavior.",
        original_text="",
        resume=parsed,
        raw_resume_text=raw_text,
        section="projects",
    )
    assert bad_filler.status == "blocked"

    gh_repos = [
        AnalyzedRepo(
            name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            description="Interactive AI-powered roadmap generator and decision intelligence platform",
            primary_language="JavaScript",
            languages={"JavaScript": 4000, "Python": 3000, "HTML": 500, "CSS": 500},
            dependencies=["react", "fastapi"],
            inferred_skills=["JavaScript", "Python", "React", "FastAPI", "HTML", "CSS"],
            readme_excerpt="Decision-IQ uses FastAPI and dynamic node canvas graphs for learning path tracking.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=39, total_commits=15, user_commits=15, user_commit_share=1.0),
        ),
        AnalyzedRepo(
            name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            description="Autonomous surplus food rescue agent powered by LangGraph, Google Gemini and FastAPI",
            primary_language="TypeScript",
            languages={"TypeScript": 3000, "Python": 3000, "HTML": 400, "CSS": 400},
            dependencies=["fastapi", "langgraph"],
            inferred_skills=["TypeScript", "Python", "FastAPI", "HTML", "CSS"],
            readme_excerpt="Food-safety validation, NGO matching and human approval gating using FastAPI and LangGraph.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=79, total_commits=11, user_commits=11, user_commit_share=1.0),
        ),
        AnalyzedRepo(
            name="AI_Deck",
            full_name="Manindra-babu/AI_Deck",
            html_url="https://github.com/Manindra-babu/AI_Deck",
            description="AI presentation deck generator built with React and Python",
            primary_language="TypeScript",
            languages={"TypeScript": 2000, "Python": 1500, "HTML": 300, "CSS": 300},
            dependencies=["react"],
            inferred_skills=["TypeScript", "Python", "React", "HTML", "CSS"],
            readme_excerpt="Generates slide decks with speaker notes and layout templates.",
            signals=RepoEvidenceSignals(has_readme=True, file_count=64, total_commits=12, user_commits=12, user_commit_share=1.0),
        ),
    ]
    recs2 = [
        RepoRecommendation(
            repo_name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            quality_score=95,
            relevance_score=87.4,
            matched_skills=["React", "JavaScript", "HTML", "Python"],
            reason="AI roadmap generator",
            decision="approved",
            draft_bullets=[
                "Built an interactive AI-powered roadmap generator and decision intelligence platform using JavaScript, Python, HTML, CSS, and FastAPI featuring dynamic node canvas graphs and learning path tracking."
            ],
            bullet_evidence=[BulletEvidenceItem(bullet="Built...", source="README + dependencies", detail="README.md")],
        ),
        RepoRecommendation(
            repo_name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            quality_score=95,
            relevance_score=87.4,
            matched_skills=["JavaScript", "HTML", "Python", "CSS"],
            reason="Autonomous food rescue agent",
            decision="approved",
            draft_bullets=[
                "Engineered an autonomous surplus food rescue agent powered by TypeScript, Python, FastAPI, LangGraph, and Gemini with food-safety validation and NGO matching."
            ],
            bullet_evidence=[BulletEvidenceItem(bullet="Engineered...", source="README + dependencies", detail="README.md")],
        ),
    ]

    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=gh_repos,
        approved_github_recs=recs2,
    )
    assert all(c["status"] == "pending" for c in changes)
    assert any(c["section"] == "summary" for c in changes)
    for c in changes:
        assert c["validation_status"] != "blocked", f"Unexpected block: {c}"
        c["status"] = "accepted"

    # 3. Full in-place 2-col export stays 1 page, preserves all untouched text & hyperlinks, adds 2 GITHUB links
    out_full = tmp_path / "full_optimized.pdf"
    res_full = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_full,
        approved_changes=changes,
        resume_structure=parsed,
        tighten_spacing=True,
    )
    assert res_full.strategy_used == "inplace_pdf"
    assert res_full.page_count_original == 1
    assert res_full.page_count_output == 1
    assert res_full.overflow_detected is False

    verify_full = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_full,
        changed_bboxes_by_page=res_full.changed_bboxes_by_page,
        band_shifts_by_page=res_full.band_shifts_by_page,
        strategy_used=res_full.strategy_used,
    )
    assert verify_full.passed is True, verify_full.blocked_reason
    assert verify_full.unchanged_similarity_score >= 97.0

    comp_ok, comp_errs = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_full,
        applied_changes=changes,
    )
    assert comp_ok is True, comp_errs

    out_doc = fitz.open(str(out_full))
    out_text = out_doc[0].get_text("text")
    assert "DECISION-IQ - GITHUB" in out_text
    assert "SURPLUS-FOOD-DONATION-MATCHER - GITHUB" in out_text
    assert "https://github.com/Manindra-babu/Decision-IQ" not in out_text
    assert "core module behavior" not in out_text.lower()
    out_uris = {l.get("uri") for l in out_doc[0].get_links() if l.get("uri")}
    assert "https://github.com/Manindra-babu/Decision-IQ" in out_uris
    assert "https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher" in out_uris
    # 2-col verification: Surplus-Food-Donation-Matcher placed right of Decision-IQ on similar Y
    tdict = out_doc[0].get_text("dict")
    title_x: dict[str, float] = {}
    for blk in tdict.get("blocks", []):
        if blk.get("type") == 0:
            for ln in blk.get("lines", []):
                txt = "".join(s.get("text", "") for s in ln["spans"])
                if "DECISION-IQ" in txt:
                    title_x["left"] = float(ln["bbox"][0])
                elif "SURPLUS-FOOD" in txt:
                    title_x["right"] = float(ln["bbox"][0])
    assert title_x.get("right", 0) > title_x.get("left", 999) + 150.0, f"Expected 2-column placement: {title_x}"
    out_doc.close()

    # 4. 3-project one-page constraint check -> overflow flagged with 3 concrete options
    recs3 = recs2 + [
        RepoRecommendation(
            repo_name="AI_Deck",
            full_name="Manindra-babu/AI_Deck",
            html_url="https://github.com/Manindra-babu/AI_Deck",
            quality_score=86,
            relevance_score=80.0,
            matched_skills=["TypeScript", "Python", "React", "HTML"],
            reason="AI deck generator",
            decision="approved",
            draft_bullets=["Built AI_Deck using React, TypeScript, Python, and HTML to generate slide decks."],
            bullet_evidence=[BulletEvidenceItem(bullet="Built...", source="README", detail="README.md")],
        ),
    ]
    changes3 = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=gh_repos,
        approved_github_recs=recs3,
    )
    for c in changes3:
        c["status"] = "accepted"
    out_ov = tmp_path / "overflow3.pdf"
    res_ov = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_ov,
        approved_changes=changes3,
        resume_structure=parsed,
        tighten_spacing=True,
    )
    assert res_ov.overflow_detected is True
    assert res_ov.page_count_output == 2
    assert len(res_ov.overflow_suggestions) >= 3
    joined_opts = " ".join(res_ov.overflow_suggestions).lower()
    assert "trim" in joined_opts and "drop" in joined_opts and "spacing" in joined_opts

    # 5. Completeness check blocks if content dropped
    dropped_pdf = tmp_path / "dropped.pdf"
    ddoc2 = fitz.open()
    p = ddoc2.new_page()
    p.insert_text((72, 100), "SUMMARY\nOnly summary present")
    ddoc2.save(str(dropped_pdf))
    ddoc2.close()
    comp_fail, errs_fail = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=dropped_pdf,
        applied_changes=[],
    )
    assert comp_fail is False and errs_fail


def test_zero_change_single_span_cache_and_similarity_gate(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.api.applications import EXPORT_CODE_VERSION, compute_export_cache_key
    from app.models.application import Application, Change, ResumeVersion
    from app.models.job import JobPosting
    from app.models.resume import ResumeFile
    from app.models.user import User

    # 1. Step 4 gate blocks ANY strategy (even style_clone_fallback) when unchanged similarity < 95%
    cloned_pdf = tmp_path / "fallback_plain.pdf"
    build_style_cloned_pdf(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=cloned_pdf,
        resume_structure=parse_pdf_resume(MANINDRA_PDF)[0],
        approved_changes=[],
    )
    verify_fallback = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=cloned_pdf,
        changed_bboxes_by_page={},
        strategy_used="style_clone_fallback",
    )
    assert verify_fallback.passed is False
    assert verify_fallback.unchanged_similarity_score < 95.0
    assert verify_fallback.blocked_reason and "95.0%" in verify_fallback.blocked_reason

    # 2. Step 3 single-span word fix ("RETRIVAL" -> "RETRIEVAL") redacts only that word
    one_pdf = tmp_path / "one_word_fix.pdf"
    one_res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=one_pdf,
        approved_changes=[
            {
                "section": "projects",
                "change_type": "reworded",
                "target_location": {},
                "original_text": "RETRIVAL",
                "new_text": "RETRIEVAL",
                "status": "accepted",
            }
        ],
        resume_structure=parse_pdf_resume(MANINDRA_PDF)[0],
    )
    assert one_res.strategy_used == "inplace_pdf"
    one_verify = verify_pdf_theme_match(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=one_pdf,
        changed_bboxes_by_page=one_res.changed_bboxes_by_page,
        band_shifts_by_page=one_res.band_shifts_by_page,
        strategy_used=one_res.strategy_used,
    )
    assert one_verify.passed is True
    assert one_verify.unchanged_similarity_score >= 99.0
    one_doc = fitz.open(str(one_pdf))
    one_text = one_doc[0].get_text("text")
    assert "RETRIEVAL" in one_text
    assert "RETRIVAL" not in one_text
    assert "KNOWLEDEGE BASE" in one_text
    assert any("github.com" in (lk.get("uri") or "").lower() for lk in one_doc[0].get_links())
    one_doc.close()

    # 3. End-to-end API cache key, invalidation, byte-for-byte 0-change export, and 422 gate
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "gate@example.com", "password": "StrongPassword123!", "full_name": "Gate Test"},
    )
    assert reg.status_code == 201
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user = db_session.query(User).filter(User.email == "gate@example.com").first()
    assert user is not None

    pdf_bytes = MANINDRA_PDF.read_bytes()
    parsed, elem_map, raw_text, page_count = parse_pdf_resume(MANINDRA_PDF)
    rf = ResumeFile(
        user_id=user.id,
        filename="manindra_resume.pdf",
        file_type="pdf",
        file_size=len(pdf_bytes),
        storage_path=str(tmp_path / "missing_ephemeral.pdf"),
        file_bytes=pdf_bytes,
        layout_type="pdf",
        page_count=page_count,
        parsed_structure=parsed.model_dump(mode="json"),
        element_map=elem_map,
        raw_text=raw_text,
    )
    jp = JobPosting(
        user_id=user.id,
        title="Software Engineer Intern",
        company="PayPal",
        raw_text="Python HTML JavaScript",
        required_skills=["Python", "HTML", "JavaScript"],
        preferred_skills=[],
        responsibilities=[],
        keywords=[],
    )
    db_session.add_all([rf, jp])
    db_session.flush()
    app_row = Application(
        user_id=user.id,
        resume_file_id=rf.id,
        job_posting_id=jp.id,
        status="Draft",
        compatibility_before=75.0,
    )
    db_session.add(app_row)
    db_session.flush()
    ver = ResumeVersion(
        application_id=app_row.id,
        version_number=1,
        page_count_original=1,
        export_cache_key="legacy-stale-key",
        preview_image_bytes=b"STALE_PNG",
        output_pdf_bytes=b"STALE_PDF",
        similarity_score=20.16,
    )
    db_session.add(ver)
    db_session.flush()
    ch = Change(
        resume_version_id=ver.id,
        section="projects",
        change_type="reworded",
        target_location={},
        original_text="RETRIVAL",
        new_text="RETRIEVAL",
        reason="Fix typo",
        validation_status="passed",
        status="pending",
    )
    db_session.add(ch)
    db_session.commit()

    # Stale preview from legacy code version must NOT be served
    stale_dl = client.get(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/download/preview", headers=headers)
    assert stale_dl.status_code == 404

    # Step 2: Export with 0 accepted changes -> byte-for-byte original + >= 98% similarity + no-store
    exp_zero = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    assert exp_zero.status_code == 200, exp_zero.text
    z_data = exp_zero.json()
    assert z_data["similarity_score"] >= 98.0
    assert z_data["unchanged_similarity_score"] >= 98.0
    assert z_data["export_cache_key"].startswith(f"{EXPORT_CODE_VERSION}:")
    dl_zero = client.get(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/download/pdf", headers=headers)
    assert dl_zero.status_code == 200
    assert dl_zero.content == pdf_bytes
    assert "no-store" in dl_zero.headers.get("cache-control", "").lower()

    # Accepting a change invalidates previous export cache immediately
    patch_res = client.patch(
        f"/api/v1/applications/{app_row.id}/changes/{ch.id}",
        json={"status": "accepted"},
        headers=headers,
    )
    assert patch_res.status_code == 200
    dl_after_patch = client.get(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/download/pdf", headers=headers)
    assert dl_after_patch.status_code == 404

    # Export with mocked degraded layout (<95%) -> HTTP 422 blocked and download blocked
    import app.api.applications as apps_mod

    monkeypatch.setattr(
        apps_mod,
        "apply_changes_to_pdf_in_place",
        lambda **kwargs: build_style_cloned_pdf(
            source_pdf_path=kwargs["source_pdf_path"],
            output_pdf_path=kwargs["output_pdf_path"],
            resume_structure=kwargs["resume_structure"],
            approved_changes=kwargs["approved_changes"],
        ),
    )
    blocked_exp = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    assert blocked_exp.status_code == 422
    detail = blocked_exp.json()["detail"]
    assert detail["unchanged_similarity_score"] < 95.0
    assert compute_export_cache_key([], source_file_type="pdf").startswith(f"{EXPORT_CODE_VERSION}:")
    dl_blocked = client.get(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/download/pdf", headers=headers)
    assert dl_blocked.status_code == 409
