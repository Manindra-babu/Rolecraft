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

    skill_changes = [
        ch for ch in changes
        if ch["section"] == "skills" and ch["change_type"] in {"reordered", "reworded"}
    ]
    for sc in skill_changes:
        orig_cat = sc["original_text"].split(":", 1)[0].strip()
        assert sc["new_text"].startswith(f"{orig_cat}:"), f"Lost category label in {sc}"
    learning_changes = [ch for ch in changes if ch["change_type"] == "user_confirmed_skill"]
    assert len(learning_changes) == 1
    assert learning_changes[0]["section"] == "skills"
    assert learning_changes[0]["new_text"].startswith("Currently Learning:")

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
        if c.get("change_type") != "relevance_trim":
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


def test_round4_skills_redraw_overlap_gate_and_currently_learning(tmp_path: Path) -> None:
    from app.services.resume.theme_verifier import check_pdf_text_overlaps

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # Negative check: synthetic PDF with overlapping spans must fail check_pdf_text_overlaps
    overlap_pdf = tmp_path / "synthetic_overlap.pdf"
    odoc = fitz.open()
    opage = odoc.new_page(width=612, height=792)
    opage.insert_text((72, 200), "Web Technologies: HTML5, CSS, JavaScript, MERN", fontsize=10.5)
    opage.insert_text((88, 201), "Currently Learning: AJAX, Web Services, SOA", fontsize=10.5)
    odoc.save(str(overlap_pdf))
    odoc.close()
    ov_fail, ov_details = check_pdf_text_overlaps(output_pdf_path=overlap_pdf)
    assert ov_fail is False
    assert len(ov_details) >= 1

    # Positive check: Task 1 whole-block Skills redraw + Task 2 single consolidated Currently Learning line
    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        confirmed_learning=[
            {"skill": "AJAX", "note": "MDN Fetch course"},
            {"skill": "Web Services"},
            {"skill": "SOA"},
        ],
    )
    uc_changes = [c for c in changes if c.get("change_type") == "user_confirmed_skill"]
    assert len(uc_changes) == 1
    assert uc_changes[0]["section"] == "skills"
    assert uc_changes[0]["new_text"] == "Currently Learning: AJAX, Web Services, SOA"

    # Evidence-backed Web Technologies expansion should spell out MERN + REST APIs without fabricating Java/C++/SQL
    wt_changes = [
        c for c in changes
        if c["section"] == "skills" and "web technologies" in str(c.get("original_text", "")).lower()
    ]
    assert wt_changes, f"Expected Web Technologies enhancement: {changes}"
    wt_new = wt_changes[0]["new_text"]
    for tok in ("Node.js", "Express.js", "MongoDB", "React"):
        assert tok in wt_new
    # No fabricated claims for unverified/missing skills
    skills_blob = " ".join(c.get("new_text", "") for c in changes if c["section"] == "skills")
    assert "Kubernetes" not in skills_blob

    for c in changes:
        if c["change_type"] != "relevance_trim":
            c["status"] = "accepted"

    out_pdf = tmp_path / "round4_skills_and_learning.pdf"
    res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        approved_changes=changes,
        resume_structure=parsed,
    )
    assert res.overlap_passed is True, res.overlap_details
    assert res.overlap_details == []

    # Verify both columns + bold category labels + standalone Currently Learning line exist in exported PDF
    edoc = fitz.open(str(out_pdf))
    epage = edoc[0]
    etxt = epage.get_text("text")
    assert "Currently Learning: AJAX, Web Services, SOA" in etxt
    assert "Databases: SQL." in etxt
    assert "CS Fundamentals:" in etxt
    # Never merged into Web Technologies proficient line
    wt_line = [ln for ln in etxt.splitlines() if "Web Technologies:" in ln]
    assert wt_line and "AJAX" not in wt_line[0]
    edoc.close()


def test_round4_relevance_typos_github_sentences_and_learning_api(
    client: TestClient,
    db_session,
    tmp_path: Path,
) -> None:
    from app.models.application import Application, ResumeVersion
    from app.models.learning import LearningPlan
    from app.models.job import JobPosting
    from app.models.resume import ResumeFile
    from app.models.user import User
    from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation
    from app.services.generation.relevance import score_resume_relevance
    from app.services.github.scorer import build_factual_repo_bullets_with_evidence

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # 1. Task 4: Relevance Review scores blocks & honors AI-interest toggle
    rel = score_resume_relevance(resume=parsed, job=job)
    assert rel
    assert any(r["section"] == "certifications" and r["tier"] == "low" for r in rel)
    assert any(r["section"] == "experience" and r["tier"] == "high" for r in rel)

    changes_no_ai = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        keep_ai_interest=False,
    )
    changes_with_ai = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        keep_ai_interest=True,
    )
    sum_no_ai = next(c for c in changes_no_ai if c["section"] == "summary" and len(c["new_text"]) > 40)
    sum_with_ai = next(c for c in changes_with_ai if c["section"] == "summary" and len(c["new_text"]) > 40)
    assert "Detail- Detail-oriented" not in sum_no_ai["new_text"]
    assert "Artificial Intelligence" in sum_with_ai["new_text"]
    assert "Artificial Intelligence" not in sum_no_ai["new_text"]

    # 2. Task 5: Factual repo bullets are complete sentences mentioning frontend (no mid-sentence cutoffs)
    gh_repo = AnalyzedRepo(
        name="Surplus-Food-Donation-Matcher",
        full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
        html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
        description="Autonomous surplus food rescue agent powered by LangGraph, Google Gemini and FastAPI",
        primary_language="TypeScript",
        languages={"TypeScript": 4500, "Python": 4000, "HTML": 1000, "CSS": 800},
        dependencies=["react", "fastapi", "langgraph"],
        inferred_skills=["TypeScript", "Python", "React", "FastAPI", "HTML", "CSS"],
        readme_excerpt="Surplus food rescue agent using React UI and FastAPI backend with rate limiting and food-safety validation.",
        signals=RepoEvidenceSignals(has_readme=True, file_count=79, total_commits=11, user_commits=11, user_commit_share=1.0),
    )
    bullets, bullets_ev = build_factual_repo_bullets_with_evidence(gh_repo)
    assert bullets and bullets_ev
    first_bullet = bullets[0]
    assert first_bullet.endswith((".", "!", "?"))
    assert "API rate." not in first_bullet
    assert "React" in first_bullet or "TypeScript/HTML/CSS" in first_bullet

    # 3. Task 6: All 5 typo fixes + INTER + 2 GitHub projects + relevance trims fit on 1 page cleanly
    recs = [
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
                "Built an interactive React and FastAPI roadmap generator with dynamic node canvas graphs and learning path tracking."
            ],
            bullet_evidence=[BulletEvidenceItem(bullet="Built...", source="README + dependencies", detail="README.md")],
        ),
        RepoRecommendation(
            repo_name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            quality_score=95,
            relevance_score=87.4,
            matched_skills=["TypeScript", "React", "HTML", "Python"],
            reason="Autonomous food rescue agent",
            decision="approved",
            draft_bullets=[first_bullet],
            bullet_evidence=bullets_ev,
        ),
    ]
    full_changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[gh_repo],
        approved_github_recs=recs,
        confirmed_learning=[
            {"skill": "AJAX", "note": "MDN Fetch"},
            {"skill": "Web Services"},
            {"skill": "SOA"},
        ],
    )
    all_orig_new = " ".join(
        f"{c.get('original_text','')} -> {c.get('new_text','')}" for c in full_changes
    )
    for fixed in ("KNOWLEDGE", "RETRIEVAL", "causal masking", "constraints", "Detail-oriented", "INTERN"):
        assert fixed in all_orig_new, f"Missing typo fix {fixed}"
    assert any(c.get("change_type") == "relevance_trim" for c in full_changes)

    for c in full_changes:
        c["status"] = "accepted"

    out_fit = tmp_path / "round4_all_accepted_fit.pdf"
    res_fit = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_fit,
        approved_changes=full_changes,
        resume_structure=parsed,
        tighten_spacing=True,
    )
    assert res_fit.overlap_passed is True, res_fit.overlap_details
    assert res_fit.page_fit.get("passed") is True, res_fit.page_fit
    assert res_fit.overflow_detected is False

    # 4. Task 2 & 3 API persistence: LearningPlan note + application detail relevance_review
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r4user@example.com", "password": "StrongPassword123!", "full_name": "R4 User"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    user = db_session.query(User).filter(User.email == "r4user@example.com").first()
    assert user is not None

    pdf_bytes = MANINDRA_PDF.read_bytes()
    resume_row = ResumeFile(
        user_id=user.id,
        filename="manindra_resume.pdf",
        file_type="pdf",
        file_size=len(pdf_bytes),
        storage_path=str(MANINDRA_PDF),
        file_bytes=pdf_bytes,
        layout_type="pdf",
        page_count=1,
        parsed_structure=parsed.model_dump(mode="json"),
        element_map=elem_map,
        raw_text=raw_text,
    )
    job_row = JobPosting(
        user_id=user.id,
        title=job.title or "Software Engineer Intern",
        company=job.company or "PayPal",
        raw_text="JavaScript HTML DOM AJAX Node.js Web Services SOA React Java C++",
        required_skills=list(job.required_skills),
        preferred_skills=list(job.preferred_skills),
        responsibilities=list(job.responsibilities),
        keywords=list(job.keywords),
    )
    db_session.add_all([resume_row, job_row])
    db_session.flush()

    app_row = Application(
        user_id=user.id,
        resume_file_id=resume_row.id,
        job_posting_id=job_row.id,
        status="Draft",
        compatibility_before=75.0,
    )
    db_session.add(app_row)
    db_session.flush()

    lp1 = LearningPlan(
        user_id=user.id,
        application_id=app_row.id,
        skill_name="AJAX",
        category="missing",
        plan_data={
            "technologies": ["AJAX"],
            "ordered_steps": [],
            "resources": [],
            "how_to_demonstrate": "Build async fetch flow",
        },
    )
    lp2 = LearningPlan(
        user_id=user.id,
        application_id=app_row.id,
        skill_name="Java",
        category="under_represented",
        plan_data={
            "technologies": ["Java"],
            "ordered_steps": [],
            "resources": [],
            "how_to_demonstrate": "Build a Spring Boot service or coursework repo.",
        },
    )
    ver = ResumeVersion(application_id=app_row.id, version_number=1, page_count_original=1)
    db_session.add_all([lp1, lp2, ver])
    db_session.commit()
    db_session.refresh(lp1)

    upd = client.put(
        f"/api/v1/learning/{lp1.id}/progress",
        json={
            "completed_steps": ["s1"],
            "user_confirmed_learning": True,
            "confirmation_note": "MDN Fetch & AJAX Course",
            "application_id": app_row.id,
        },
        headers=headers,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["confirmation_note"] == "MDN Fetch & AJAX Course"

    detail = client.get(f"/api/v1/applications/{app_row.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    d_json = detail.json()
    assert len(d_json["relevance_review"]) >= 5
    latest_ver = d_json["versions"][-1]
    uc_rows = [c for c in latest_ver["changes"] if c["change_type"] == "user_confirmed_skill"]
    assert len(uc_rows) == 1
    assert uc_rows[0]["section"] == "skills"
    assert uc_rows[0]["new_text"] == "Currently Learning: AJAX"


def test_round5_completeness_cisco_pagefit_styling_wording_accuracy(client, db_session, tmp_path: Path) -> None:
    """
    Round 5 comprehensive regression test covering:
      1. Never delete CISCO/RedHat/Microsoft or Education bullets without explicit approval; completeness gate blocks unapproved drops.
      2. Protect 'CISCO - C Essentials 1' whenever C or C++ is in job requirements.
      3. Page-fit gate blocks margin < 12pt or overflow and returns 3 structured options with before/after previews + API choice.
      4. Summary preserves run-level Italic + BoldItalic fonts (never flattened to plain).
      5. Wording quality (no REST/HTTP or PyTorch/Python redundancy, 'responsive' kept, grammatical 'an interactive...') + MERN/Firebase accuracy prompt.
      6. Skills bullets match original dot size (>= 10.5pt) and indentation with 0 overlaps.
    """
    import fitz
    from app.models.application import Application, Change, ResumeVersion
    from app.models.job import JobPosting
    from app.models.resume import ResumeFile
    from app.models.user import User
    from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation
    from app.services.generation.optimizer import generate_tailored_changes
    from app.services.generation.relevance import score_resume_relevance
    from app.services.github.scorer import build_factual_repo_bullets_with_evidence
    from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
    from app.services.resume.theme_verifier import verify_exported_pdf_completeness

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # 1. CISCO C Essentials 1 must be protected when C++ is required
    rel_items = score_resume_relevance(resume=parsed, job=job)
    cisco_items = [r for r in rel_items if "cisco" in str(r.get("text", "")).lower() and "c essentials" in str(r.get("text", "")).lower()]
    assert len(cisco_items) == 1, rel_items
    assert cisco_items[0]["recommended_action"] == "keep", cisco_items[0]
    assert float(cisco_items[0]["score"]) >= 0.60, cisco_items[0]

    # 2. Generate tailored changes -> no redundancy, 'responsive' kept, MERN prompt flagged, 0 auto-accepted removals
    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
    )
    all_prop_text = " ".join(c.get("new_text", "") for c in changes)
    assert "(HTTP/REST)" not in all_prop_text
    assert "PyTorch (Python)" not in all_prop_text
    assert all(c.get("status", "pending") == "pending" for c in changes)
    # Never propose removing CISCO C Essentials while C++ is required
    cisco_trims = [c for c in changes if c.get("change_type") == "relevance_trim" and "cisco" in c.get("original_text", "").lower()]
    assert len(cisco_trims) == 0, cisco_trims

    # Check 'responsive' preserved on first internship bullet & MERN prompt flagged
    exp_bullets = [c for c in changes if c.get("section") == "experience" and c.get("change_type") == "reworded"]
    first_bul = next((c for c in exp_bullets if "responsive" in c.get("original_text", "").lower()), None)
    assert first_bul is not None, exp_bullets
    assert "responsive" in first_bul["new_text"].lower(), first_bul
    assert "Did you use MongoDB at this internship (the next line says Firebase)?" in str(
        (first_bul.get("target_location") or {}).get("accuracy_prompt") or ""
    )
    assert first_bul.get("validation_status") == "flagged"
    assert (first_bul.get("target_location") or {}).get("requires_confirmation") is True
    assert "Express.js, React" in str((first_bul.get("target_location") or {}).get("without_mongodb") or "")

    # 3. GitHub factual bullets grammar: proper indefinite article & no '/' slash stack
    diq_repo = AnalyzedRepo(
        name="Decision-IQ",
        full_name="Manindra-babu/Decision-IQ",
        html_url="https://github.com/Manindra-babu/Decision-IQ",
        description="Interactive AI-powered roadmap generator and decision intelligence platform featuring dynamic node canvas graphs",
        primary_language="JavaScript",
        languages={"JavaScript": 6000, "Python": 3000, "CSS": 1000},
        dependencies=["react", "fastapi"],
        inferred_skills=["React", "FastAPI", "Python", "JavaScript", "HTML"],
        readme_excerpt="Interactive AI-powered roadmap generator featuring dynamic node canvas graphs and learning path tracking.",
        signals=RepoEvidenceSignals(has_readme=True, file_count=39, total_commits=15, user_commits=15, user_commit_share=1.0, has_tests=True),
    )
    bullets_diq, _ = build_factual_repo_bullets_with_evidence(diq_repo)
    assert bullets_diq[0].startswith("Built Decision-IQ, an interactive AI-powered roadmap generator"), bullets_diq
    assert "FastAPI / JavaScript" not in bullets_diq[0]

    # 4. Run-level styling preservation in Summary & Skills bullet size/indent
    summary_ch = next(
        (c for c in changes if c.get("section") == "summary" and len(c.get("original_text", "")) > 40),
        None,
    )
    skills_ch = [c for c in changes if c.get("section") == "skills" and c.get("change_type") in {"reordered", "reworded"}]
    styled_only = [dict(c, status="accepted") for c in ([summary_ch] if summary_ch else []) + skills_ch[:2]]
    out_styled = tmp_path / "round5_styled.pdf"
    res_styled = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_styled,
        approved_changes=styled_only,
        resume_structure=parsed,
        relevance_items=rel_items,
    )
    assert res_styled.overlap_passed is True, res_styled.overlap_details
    assert res_styled.page_fit.get("passed") is True, res_styled.page_fit
    assert float(res_styled.page_fit.get("margin_pt", 0)) >= 12.0

    # Verify BOTH BoldItalic and Italic runs are preserved in Summary, and ALL certs/Education bullets remain intact
    sdoc = fitz.open(str(out_styled))
    spage = sdoc[0]
    td = spage.get_text("dict")
    summary_fonts: list[tuple[str, int, str]] = []
    skill_bullet_sizes: list[float] = []
    full_out_txt = spage.get_text("text")
    for blk in td.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            y0 = float(ln["bbox"][1])
            for sp in ln.get("spans", []):
                if 115.0 <= y0 <= 180.0 and sp.get("text", "").strip():
                    summary_fonts.append((str(sp.get("font", "")), int(sp.get("flags", 0)), sp.get("text", "")))
                if 595.0 <= y0 <= 675.0 and sp.get("text", "").strip() == "•":
                    skill_bullet_sizes.append(float(sp.get("size", 0.0)))
    sdoc.close()

    assert any((fl & 16) or "bold" in fn.lower() for fn, fl, _ in summary_fonts), summary_fonts
    assert any((fl & 2) or "italic" in fn.lower() for fn, fl, _ in summary_fonts), summary_fonts
    assert skill_bullet_sizes and min(skill_bullet_sizes) >= 10.5, skill_bullet_sizes
    for must_keep in ("C Essentials 1", "Red Hat OpenShift", "Python for Data Science", "Excel Associate", "100+ peers", "technical documentation"):
        assert must_keep in full_out_txt, f"Missing unapproved removal: {must_keep}"

    # Completeness gate: removing CISCO or Education bullet without accepted removal MUST fail
    comp_doc = fitz.open(str(out_styled))
    cp0 = comp_doc[0]
    for hit in cp0.search_for("Red Hat OpenShift"):
        cp0.add_redact_annot(hit, fill=(1, 1, 1))
    cp0.apply_redactions()
    missing_cert_pdf = tmp_path / "missing_cert.pdf"
    comp_doc.save(str(missing_cert_pdf))
    comp_doc.close()
    ok_missing, errs_missing = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=missing_cert_pdf,
        applied_changes=styled_only,
    )
    assert ok_missing is False
    assert any("red hat" in e.lower() for e in errs_missing), errs_missing

    # 5. End-to-end API checks: MERN accuracy prompt enforcement + Page-fit <12pt/overflow 422 gate & 3 preview options
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r5user@example.com", "password": "StrongPassword123!", "full_name": "R5 User"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    user = db_session.query(User).filter(User.email == "r5user@example.com").first()
    pdf_bytes = MANINDRA_PDF.read_bytes()
    resume_row = ResumeFile(
        user_id=user.id,
        filename="manindra_resume.pdf",
        file_type="pdf",
        file_size=len(pdf_bytes),
        storage_path=str(MANINDRA_PDF),
        file_bytes=pdf_bytes,
        layout_type="pdf",
        page_count=1,
        parsed_structure=parsed.model_dump(mode="json"),
        element_map=elem_map,
        raw_text=raw_text,
    )
    job_row = JobPosting(
        user_id=user.id,
        title=job.title or "Software Engineer Intern",
        company=job.company or "PayPal",
        raw_text="JavaScript React Node.js C++ REST APIs",
        required_skills=list(job.required_skills),
        preferred_skills=list(job.preferred_skills),
        responsibilities=list(job.responsibilities),
        keywords=list(job.keywords),
    )
    db_session.add_all([resume_row, job_row])
    db_session.flush()
    app_row = Application(
        user_id=user.id,
        resume_file_id=resume_row.id,
        job_posting_id=job_row.id,
        status="Draft",
        compatibility_before=75.0,
    )
    db_session.add(app_row)
    db_session.flush()
    ver = ResumeVersion(application_id=app_row.id, version_number=1, page_count_original=1)
    db_session.add(ver)
    db_session.flush()

    mern_ch = Change(
        resume_version_id=ver.id,
        section="experience",
        change_type="reworded",
        target_location=first_bul["target_location"],
        original_text=first_bul["original_text"],
        new_text=first_bul["new_text"],
        reason=first_bul["reason"],
        evidence_refs=[],
        validation_status="flagged",
        validation_warnings=["Confirm MongoDB usage before applying"],
        status="pending",
    )
    proj1_ch = Change(
        resume_version_id=ver.id,
        section="projects",
        change_type="added_from_github",
        target_location={"repo_name": "Decision-IQ", "html_url": "https://github.com/Manindra-babu/Decision-IQ", "skills": ["React", "FastAPI"]},
        original_text="",
        new_text="Decision-IQ | React, JavaScript, Python | https://github.com/Manindra-babu/Decision-IQ\n• Built Decision-IQ, an interactive AI-powered roadmap generator featuring dynamic node canvas graphs.\n• Added automated unit tests for core components.",
        reason="Relevant project",
        evidence_refs=[],
        validation_status="passed",
        validation_warnings=[],
        status="accepted",
    )
    proj2_ch = Change(
        resume_version_id=ver.id,
        section="projects",
        change_type="added_from_github",
        target_location={"repo_name": "Surplus-Food-Donation-Matcher", "html_url": "https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher", "skills": ["React", "Python"]},
        original_text="",
        new_text="Surplus-Food-Donation-Matcher | React, Python, FastAPI | https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher\n• Built Surplus-Food-Donation-Matcher, an AI matching agent with validation and human approval gating.\n• Added integration test coverage across key routes.",
        reason="Relevant project",
        evidence_refs=[],
        validation_status="passed",
        validation_warnings=[],
        status="accepted",
    )
    db_session.add_all([mern_ch, proj1_ch, proj2_ch])
    db_session.commit()
    db_session.refresh(mern_ch)

    # Cannot accept MERN expansion without answering accuracy confirmation
    bad_acc = client.patch(
        f"/api/v1/applications/{app_row.id}/changes/{mern_ch.id}",
        json={"status": "accepted"},
        headers=headers,
    )
    assert bad_acc.status_code == 400
    assert "MongoDB" in bad_acc.json()["detail"]

    # Answering No (Firebase used) rewrites without MongoDB cleanly
    no_mongo = client.patch(
        f"/api/v1/applications/{app_row.id}/changes/{mern_ch.id}",
        json={"status": "accepted", "accuracy_confirmed": False},
        headers=headers,
    )
    assert no_mongo.status_code == 200, no_mongo.text
    assert no_mongo.json()["status"] == "edited"
    assert "MongoDB" not in (no_mongo.json()["edited_text"] or "")
    assert "Express.js, React" in (no_mongo.json()["edited_text"] or "")

    # Exporting 2 added projects with 0 removals triggers page-fit block (422) + 3 preview options
    exp_block = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    assert exp_block.status_code == 422, exp_block.text
    det = exp_block.json()["detail"]
    assert det["page_fit"]["passed"] is False
    opts = det["page_fit"].get("options") or []
    assert [o["id"] for o in opts] == ["shorten_projects", "condense_education", "drop_lowest_relevance"]
    assert all("previews" in o for o in opts)

    # Choosing 'drop_lowest_relevance' trims lowest-relevance item(s) cleanly and if still tight, 'shorten_projects' fits 1 page >= 12pt margin
    pick = client.post(
        f"/api/v1/applications/{app_row.id}/versions/{ver.id}/page-fit-option",
        json={"option_id": "drop_lowest_relevance"},
        headers=headers,
    )
    assert pick.status_code == 200, pick.text

    exp_retry = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    if exp_retry.status_code == 422:
        client.post(
            f"/api/v1/applications/{app_row.id}/versions/{ver.id}/page-fit-option",
            json={"option_id": "shorten_projects"},
            headers=headers,
        )
        exp_retry = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    assert exp_retry.status_code == 200, exp_retry.text
    ok_json = exp_retry.json()
    assert ok_json["page_fit"]["passed"] is True
    assert float(ok_json["page_fit"]["margin_pt"]) >= 12.0
    assert ok_json["overlap_passed"] is True
    assert ok_json["export_blocked"] is False


def test_round6_no_lost_content_richer_projects_skills_spacing(tmp_path: Path, client, db_session):
    """
    Round 6 regression test (fail-first):
      1. Zero accepted changes and typical non-removal edits preserve ALL Certifications (w/ links & bold labels) and all Education bullets.
      2. Removing a multi-link block (RedHat) never clips adjacent Certification lines (IBM/CISCO).
      3. Richer Decision-IQ & Surplus-Food-Donation-Matcher descriptions include React/FastAPI (+ LangGraph/Gemini) in 2-3 grammatical lines with evidence.
      4. Skills ordering keeps CSS next to HTML5/JavaScript and C next to C++, and generates pending 'casual masking' -> 'causal masking'.
      5. Section-to-divider-rule gaps stay >= original gaps.
      6. Completeness failure exposes structured `missing_blocks` on export block response.
    """
    import fitz
    from app.models.application import Application, ResumeVersion
    from app.models.job import JobPosting
    from app.models.resume import ResumeFile
    from app.models.user import User
    from app.schemas.github import AnalyzedRepo, BulletEvidenceItem, RepoEvidenceSignals, RepoRecommendation
    from app.services.generation.optimizer import generate_tailored_changes
    from app.services.generation.relevance import score_resume_relevance
    from app.services.github.scorer import build_factual_repo_bullets_with_evidence
    from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
    from app.services.resume.theme_verifier import verify_exported_pdf_completeness

    assert MANINDRA_PDF.exists()
    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()
    rel_items = score_resume_relevance(resume=parsed, job=job)

    # Richer GitHub repo cards for Decision-IQ and Surplus-Food-Donation-Matcher
    diq_repo = AnalyzedRepo(
        name="Decision-IQ",
        full_name="Manindra-babu/Decision-IQ",
        html_url="https://github.com/Manindra-babu/Decision-IQ",
        description="Interactive AI-powered roadmap generator and decision intelligence platform featuring dynamic node canvas graphs",
        primary_language="JavaScript",
        languages={"JavaScript": 6200, "Python": 3400, "CSS": 900},
        dependencies=["react", "fastapi", "uvicorn"],
        inferred_skills=["React", "FastAPI", "Python", "JavaScript", "HTML"],
        readme_excerpt="Interactive AI-powered roadmap generator with React Flow canvas and FastAPI backend services.",
        signals=RepoEvidenceSignals(
            has_readme=True, file_count=39, total_commits=15, user_commits=15, user_commit_share=1.0, has_tests=True
        ),
    )
    sfdm_repo = AnalyzedRepo(
        name="Surplus-Food-Donation-Matcher",
        full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
        html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
        description="AI-powered surplus food matching agent connecting donors with shelters using multi-step human-in-the-loop validation",
        primary_language="Python",
        languages={"Python": 5500, "JavaScript": 2800},
        dependencies=["react", "fastapi", "langgraph", "google-generativeai"],
        inferred_skills=["React", "FastAPI", "LangGraph", "Gemini", "Python"],
        readme_excerpt="AI food matching workflow orchestrated with LangGraph and Google Gemini over a React frontend and FastAPI API.",
        signals=RepoEvidenceSignals(
            has_readme=True, file_count=28, total_commits=12, user_commits=12, user_commit_share=1.0, has_tests=True
        ),
    )

    b_diq, ev_diq = build_factual_repo_bullets_with_evidence(diq_repo)
    b_sfdm, ev_sfdm = build_factual_repo_bullets_with_evidence(sfdm_repo)
    assert "react" in b_diq[0].lower() and "fastapi" in b_diq[0].lower(), b_diq
    assert all(k in b_sfdm[0].lower() for k in ("react", "fastapi", "langgraph", "gemini")), b_sfdm
    assert ev_diq and ev_sfdm

    recs_r6 = [
        RepoRecommendation(
            repo_name="Decision-IQ",
            full_name="Manindra-babu/Decision-IQ",
            html_url="https://github.com/Manindra-babu/Decision-IQ",
            quality_score=95,
            relevance_score=90.0,
            matched_skills=["React", "FastAPI", "Python", "JavaScript"],
            reason="Full-stack React + FastAPI roadmap platform",
            decision="approved",
            draft_bullets=b_diq,
            bullet_evidence=ev_diq,
        ),
        RepoRecommendation(
            repo_name="Surplus-Food-Donation-Matcher",
            full_name="Manindra-babu/Surplus-Food-Donation-Matcher",
            html_url="https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher",
            quality_score=95,
            relevance_score=89.0,
            matched_skills=["React", "FastAPI", "LangGraph", "Gemini", "Python"],
            reason="Multi-agent workflow with React, FastAPI, LangGraph, and Gemini",
            decision="approved",
            draft_bullets=b_sfdm,
            bullet_evidence=ev_sfdm,
        ),
    ]
    changes = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[diq_repo, sfdm_repo],
        approved_github_recs=recs_r6,
    )

    # Skills ordering constraints + pending casual->causal typo check
    # Round 7: C, C++, Java, Python should NOT be unnecessarily reordered; keep canonical C, C++ order.
    lang_ch = next(
        (c for c in changes if c.get("section") == "skills" and "Programming Languages:" in (c.get("new_text") or "")),
        None,
    )
    web_ch = next(
        (c for c in changes if c.get("section") == "skills" and "Web Technologies:" in (c.get("new_text") or "")),
        None,
    )
    assert lang_ch is None, f"Unexpected Programming Languages reorder: {lang_ch}"
    assert web_ch is not None
    assert "html5, css, javascript" in web_ch["new_text"].lower() or "css, html5" in web_ch["new_text"].lower(), web_ch["new_text"]

    casual_typo = next(
        (c for c in changes if "casual masking" in (c.get("original_text") or "").lower()),
        None,
    )
    assert casual_typo is not None and casual_typo.get("status", "pending") == "pending"
    assert "causal masking" in (casual_typo.get("new_text") or "").lower()

    # 1. Zero accepted changes: ALL original text + all Certifications + all Education bullets preserved
    zero_out = tmp_path / "round6_zero.pdf"
    res_zero = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=zero_out,
        approved_changes=[],
        resume_structure=parsed,
        relevance_items=rel_items,
    )
    comp_zero_ok, comp_zero_errs = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=zero_out,
        applied_changes=[],
    )
    assert comp_zero_ok, comp_zero_errs
    assert res_zero.overlap_passed is True

    # 2. Typical accepted set (NO removals accepted, WITH 2 rich projects + Summary + Exp + Typos + Skills + Currently Learning)
    typical_approved: list[dict] = []
    for c in changes:
        if c.get("change_type") == "relevance_trim":
            continue
        if (c.get("target_location") or {}).get("requires_confirmation"):
            # Simulate user confirming Firebase (No MongoDB)
            cc = dict(c)
            cc["status"] = "edited"
            cc["edited_text"] = (c.get("target_location") or {}).get("without_mongodb") or c["new_text"]
            cc["validation_status"] = "passed"
            cc["target_location"] = {**(c.get("target_location") or {}), "accuracy_confirmed": False}
            typical_approved.append(cc)
        else:
            typical_approved.append(dict(c, status="accepted"))

    typical_approved.append({
        "section": "skills",
        "change_type": "user_confirmed_skill",
        "original_text": "",
        "new_text": "Currently Learning: AJAX, Web Services, SOA",
        "reason": "User confirmed currently learning.",
        "status": "accepted",
        "validation_status": "passed",
        "target_location": {"mode": "currently_learning", "skills": ["AJAX", "Web Services", "SOA"]},
    })

    # Make sure the two rich repo additions use our fuller 2-3 line descriptions
    for cc in typical_approved:
        if cc.get("change_type") == "added_from_github":
            rname = (cc.get("target_location") or {}).get("repo_name", "")
            if "Decision-IQ" in rname:
                cc["new_text"] = f"Decision-IQ | React, FastAPI | https://github.com/Manindra-babu/Decision-IQ\n• {b_diq[0]}"
            elif "Surplus-Food" in rname:
                cc["new_text"] = f"Surplus-Food-Donation-Matcher | React, FastAPI, LangGraph, Gemini | https://github.com/Manindra-babu/Surplus-Food-Donation-Matcher\n• {b_sfdm[0]}"

    out_typical = tmp_path / "round6_typical.pdf"
    res_typ = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_typical,
        approved_changes=typical_approved,
        resume_structure=parsed,
        tighten_spacing=True,
        relevance_items=rel_items,
    )
    assert res_typ.overlap_passed is True, res_typ.overlap_details
    assert res_typ.page_count_output == 1, res_typ.page_fit
    assert float((res_typ.page_fit or {}).get("margin_pt", -1)) >= 12.0, res_typ.page_fit

    comp_typ_ok, comp_typ_errs = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_typical,
        applied_changes=typical_approved,
    )
    assert comp_typ_ok, comp_typ_errs

    od = fitz.open(str(out_typical))
    op = od[0]
    out_txt = op.get_text("text")
    out_uris = {str(lk.get("uri") or "") for lk in (op.get_links() or [])}
    for req_txt in (
        "C Essentials 1",
        "Red Hat OpenShift",
        "Red Hat System Administration I & II",
        "Python for Data Science",
        "Excel Associate",
        "Power Platform Fundamentals",
        "100+ peers",
        "technical documentation",
        "Currently Learning:",
        "AJAX, Web Services, SOA",
        "causal masking",
        "Decision-IQ",
        "Surplus-Food-Donation-Matcher",
        "LangGraph",
        "Gemini",
    ):
        assert req_txt.lower() in out_txt.lower(), f"Missing expected text '{req_txt}' in output"

    # Check every original Certification URI is preserved
    orig_d = fitz.open(str(MANINDRA_PDF))
    orig_p = orig_d[0]
    for lk in orig_p.get_links() or []:
        u = str(lk.get("uri") or "")
        if u:
            assert u in out_uris, f"Lost hyperlink: {u}"

    # Verify section-to-divider-rule gap >= original gap (within 0.5pt)
    def _section_end_to_rule_gaps(page_obj: fitz.Page) -> list[float]:
        from app.services.resume.pdf_editor import _detect_horizontal_rules

        pw = float(page_obj.rect.width)
        rules = _detect_horizontal_rules(page_obj, pw)
        line_y1s: list[float] = []
        for blk in page_obj.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                if "".join(s.get("text", "") for s in ln.get("spans", [])).strip():
                    line_y1s.append(float(ln["bbox"][3]))
        gaps: list[float] = []
        prev_r1 = 0.0
        for r0, r1 in rules:
            below_prev = [y1 for y1 in line_y1s if prev_r1 <= y1 <= r0 + 0.5]
            if below_prev:
                gaps.append(round(r0 - max(below_prev), 2))
            prev_r1 = r1
        return gaps

    orig_gaps = _section_end_to_rule_gaps(orig_p)
    out_gaps = _section_end_to_rule_gaps(op)
    orig_d.close()
    od.close()
    assert len(out_gaps) == len(orig_gaps), (orig_gaps, out_gaps)
    for idx_g, (og, ng) in enumerate(zip(orig_gaps, out_gaps)):
        assert ng >= og - 0.5, f"Rule {idx_g} gap shrank from {og}pt to {ng}pt"

    # 3. Single multi-link removal (RedHat) when explicitly removed by user must NOT clip IBM or CISCO
    redhat_line = next((c for c in parsed.certifications if "redhat" in c.lower()), "RedHat - Red Hat OpenShift , Red Hat System Administration I & II")
    redhat_trim = next(
        (c for c in changes if c.get("change_type") == "relevance_trim" and "redhat" in (c.get("original_text") or "").lower()),
        None,
    ) or {
        "section": "certifications",
        "change_type": "relevance_trim",
        "original_text": redhat_line,
        "new_text": "",
        "status": "pending",
        "reason": "User explicit removal of RedHat certification",
    }
    assert redhat_trim is not None
    rh_out = tmp_path / "round6_only_rh_removed.pdf"
    rh_approved = [dict(redhat_trim, status="accepted")]
    apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=rh_out,
        approved_changes=rh_approved,
        resume_structure=parsed,
        relevance_items=rel_items,
    )
    rh_ok, rh_errs = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=rh_out,
        applied_changes=rh_approved,
    )
    assert rh_ok, rh_errs
    rhd = fitz.open(str(rh_out))
    rh_txt = rhd[0].get_text("text")
    rhd.close()
    assert "Python for Data Science" in rh_txt and "C Essentials 1" in rh_txt and "Excel Associate" in rh_txt

    # 4. Completeness block exposes structured missing_blocks in 422 response
    from unittest.mock import patch

    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "r6user@example.com", "password": "StrongPassword123!", "full_name": "R6 User"},
    )
    assert reg.status_code == 201
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    user = db_session.query(User).filter(User.email == "r6user@example.com").first()
    pdf_bytes = MANINDRA_PDF.read_bytes()
    resume_row = ResumeFile(
        user_id=user.id,
        filename="manindra_resume.pdf",
        file_type="pdf",
        file_size=len(pdf_bytes),
        storage_path=str(MANINDRA_PDF),
        file_bytes=pdf_bytes,
        layout_type="pdf",
        page_count=1,
        parsed_structure=parsed.model_dump(mode="json"),
        element_map=elem_map,
        raw_text=raw_text,
    )
    job_row = JobPosting(
        user_id=user.id,
        title="Software Engineer Intern",
        company="PayPal",
        raw_text="JavaScript React Node.js C++ REST APIs",
        required_skills=list(job.required_skills),
        preferred_skills=list(job.preferred_skills),
        responsibilities=list(job.responsibilities),
        keywords=list(job.keywords),
    )
    db_session.add_all([resume_row, job_row])
    db_session.flush()
    app_row = Application(
        user_id=user.id,
        resume_file_id=resume_row.id,
        job_posting_id=job_row.id,
        status="Draft",
        compatibility_before=75.0,
    )
    db_session.add(app_row)
    db_session.flush()
    ver = ResumeVersion(application_id=app_row.id, version_number=1, page_count_original=1)
    db_session.add(ver)
    db_session.commit()

    with patch(
        "app.api.applications.verify_exported_pdf_completeness",
        return_value=(
            False,
            ["Completeness check failed: original content missing without accepted removal ('IBM - Python for Data Science')."],
        ),
    ):
        comp_blk_res = client.post(f"/api/v1/applications/{app_row.id}/versions/{ver.id}/export", headers=headers)
    assert comp_blk_res.status_code == 422, comp_blk_res.text
    det_c = comp_blk_res.json()["detail"]
    missing_list = det_c.get("missing_blocks") or (det_c.get("page_fit") or {}).get("missing_blocks") or []
    assert any("IBM" in str(m) for m in missing_list), det_c


def test_round7_typos_evidence_summary_docx_and_multiformat_regression(tmp_path: Path, db_session) -> None:
    """
    Round 7 verification & multi-format regression suite:
      1. Pending Custom GPT typos ('casual masking'->'causal masking', 'constrains'->'constraints') apply cleanly in PDF & DOCX.
      2. Decision-IQ uses only verified repo evidence (no hallucinated Node.js) and shows per-technology evidence.
      3. Programming Languages keeps canonical 'C, C++, Java, Python' without unnecessary reordering.
      4. Summary attributes internship + academic projects + hackathons, drops generic filler, and keeps AI/LLM toggle.
      5. Exported PDF and DOCX match (1 page PDF, working links, overlaps=0, completeness=True, Currently Learning inside Skills).
      6. Multi-resume + multi-job regression across Word DOCX, single-col PDF, two-col sidebar PDF, and 2 job URLs.
    """
    from unittest.mock import MagicMock, patch
    import fitz
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    from app.models.user import User
    from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals
    from app.services.generation.optimizer import generate_tailored_changes
    from app.services.github.scorer import build_factual_repo_bullets_with_evidence, rank_repos_for_job
    from app.services.jobs.extractor import extract_job_from_url
    from app.services.resume.docx_editor import (
        _iter_all_paragraphs,
        apply_changes_to_docx_in_place,
        build_ats_docx_from_structure,
    )
    from app.services.resume.docx_parser import parse_docx_resume
    from app.services.resume.pdf_editor import apply_changes_to_pdf_in_place
    from app.services.resume.theme_verifier import verify_exported_pdf_completeness

    parsed, elem_map, raw_text, _ = parse_pdf_resume(MANINDRA_PDF)
    job = _load_paypal_job()

    # 1 & 2. Decision-IQ evidence-only tech selection (prose "node canvas" must NOT trigger Node.js) + per-tech evidence
    diq_repo = AnalyzedRepo(
        name="Decision-IQ",
        full_name="Manindra-babu/Decision-IQ",
        html_url="https://github.com/Manindra-babu/Decision-IQ",
        description="Interactive AI-powered roadmap generator and decision intelligence platform featuring dynamic node canvas graphs",
        primary_language="JavaScript",
        languages={"JavaScript": 6200, "Python": 3400, "CSS": 900},
        dependencies=["react", "fastapi", "uvicorn"],
        inferred_skills=["React", "FastAPI", "Python", "JavaScript", "HTML"],
        readme_excerpt="Interactive AI-powered roadmap generator with React Flow canvas and FastAPI backend services.",
        signals=RepoEvidenceSignals(
            has_readme=True, file_count=39, total_commits=15, user_commits=15, user_commit_share=1.0, has_tests=True
        ),
    )
    b_diq, ev_diq = build_factual_repo_bullets_with_evidence(diq_repo)
    assert "node.js" not in b_diq[0].lower(), f"Hallucinated Node.js in Decision-IQ: {b_diq}"
    for req_tech in ("React", "FastAPI", "Python", "JavaScript"):
        assert req_tech.lower() in b_diq[0].lower(), b_diq
        assert f"{req_tech.lower()}:" in ev_diq[0].detail.lower(), ev_diq[0].detail

    recs = rank_repos_for_job([diq_repo], job_skills=job.required_skills, job_keywords=job.keywords, resume_text=raw_text)
    for r in recs:
        r.decision = "approved"

    changes_no_ai = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[diq_repo],
        approved_github_recs=recs,
        confirmed_learning=[{"skill": "AJAX"}, {"skill": "Web Services"}, {"skill": "SOA"}],
        keep_ai_interest=False,
    )
    changes_with_ai = generate_tailored_changes(
        resume=parsed,
        raw_resume_text=raw_text,
        element_map=elem_map,
        job=job,
        github_repos=[diq_repo],
        approved_github_recs=recs,
        keep_ai_interest=True,
    )

    # Custom GPT typos are pending
    casual_ch = next((c for c in changes_no_ai if "casual masking" in (c.get("original_text") or "").lower()), None)
    constrains_ch = next((c for c in changes_no_ai if "constrains" in (c.get("original_text") or "").lower()), None)
    assert casual_ch is not None and casual_ch["status"] == "pending" and "causal masking" in casual_ch["new_text"].lower()
    assert constrains_ch is not None and constrains_ch["status"] == "pending" and "constraints" in constrains_ch["new_text"].lower()

    # 3. Skills restore C, C++, Java, Python (no reorder change emitted for Programming Languages)
    prog_lang_ch = [
        c for c in changes_no_ai if c.get("section") == "skills" and "Programming Languages:" in (c.get("new_text") or "")
    ]
    assert prog_lang_ch == [], prog_lang_ch

    # 4. Summary attribution + no generic filler + toggleable AI/LLM interest
    sum_no_ai = next(c for c in changes_no_ai if c.get("section") == "summary" and len(c.get("new_text") or "") > 40)
    sum_with_ai = next(c for c in changes_with_ai if c.get("section") == "summary" and len(c.get("new_text") or "") > 40)
    assert "through an internship, academic projects, and hackathons" in sum_no_ai["new_text"]
    assert "Demonstrates strong problem-solving" not in sum_no_ai["new_text"]
    assert "Artificial Intelligence" not in sum_no_ai["new_text"]
    assert "Artificial Intelligence" in sum_with_ai["new_text"]
    assert "Artificial Intelligence" in str((sum_no_ai.get("target_location") or {}).get("alt_with_ai_interest") or "")

    # 5. Export PDF + DOCX verification (links, page count, completeness, overlaps, C/C++ preserved)
    approved = []
    for c in changes_no_ai:
        if c.get("change_type") == "relevance_trim":
            continue
        cc = dict(c)
        if (cc.get("target_location") or {}).get("requires_confirmation"):
            cc["status"] = "edited"
            cc["edited_text"] = cc["target_location"]["without_mongodb"]
        else:
            cc["status"] = "accepted"
        approved.append(cc)

    out_pdf = tmp_path / "r7_manindra.pdf"
    out_docx = tmp_path / "r7_manindra.docx"
    pdf_res = apply_changes_to_pdf_in_place(
        source_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        approved_changes=approved,
        resume_structure=parsed,
    )
    assert pdf_res.page_count_output == 1
    assert pdf_res.overlap_passed is True, pdf_res.overlap_details
    assert (pdf_res.page_fit or {}).get("passed") is True, pdf_res.page_fit
    comp_ok, comp_errs = verify_exported_pdf_completeness(
        original_pdf_path=MANINDRA_PDF,
        output_pdf_path=out_pdf,
        applied_changes=approved,
    )
    assert comp_ok, comp_errs

    pd = fitz.open(str(out_pdf))
    ptxt = pd[0].get_text("text")
    puris = {str(lk.get("uri") or "") for lk in (pd[0].get_links() or [])}
    pd.close()
    assert "https://github.com/Manindra-babu/Decision-IQ" in puris
    assert "causal masking" in ptxt and "constraints" in ptxt
    assert "C, C++, Java, Python" in ptxt

    build_ats_docx_from_structure(
        structure=parsed,
        approved_changes=approved,
        output_docx_path=out_docx,
        style_tokens=elem_map.get("style_tokens"),
    )
    dx = Document(str(out_docx))
    dx_txt = "\n".join(p.text for p in _iter_all_paragraphs(dx))
    dx_uris = {r.target_ref for r in dx.part.rels.values() if r.reltype == RT.HYPERLINK}
    assert "https://github.com/Manindra-babu/Decision-IQ" in dx_uris
    assert "causal masking" in dx_txt and "constraints" in dx_txt
    assert "C, C++, Java, Python" in dx_txt
    assert "Currently Learning: AJAX, Web Services, SOA" in dx_txt
    assert "through an internship, academic projects, and hackathons" in dx_txt
    assert "Demonstrates strong problem-solving" not in dx_txt

    # Word DOCX in-place editing parity on manindra_resume.docx
    manindra_docx = MANINDRA_PDF.parent / "manindra_resume.docx"
    st_dx, el_dx, raw_dx, _, _, _ = parse_docx_resume(manindra_docx)
    chg_dx = generate_tailored_changes(
        resume=st_dx,
        raw_resume_text=raw_dx,
        element_map=el_dx,
        job=job,
        github_repos=[diq_repo],
        approved_github_recs=recs,
        confirmed_learning=[{"skill": "AJAX"}],
    )
    app_dx = [dict(c, status="accepted") for c in chg_dx if c.get("change_type") != "relevance_trim"]
    out_in_dx = tmp_path / "r7_inplace.docx"
    dx_warns = apply_changes_to_docx_in_place(
        source_docx_path=manindra_docx,
        output_docx_path=out_in_dx,
        approved_changes=app_dx,
    )
    assert dx_warns == [], dx_warns
    dx2 = Document(str(out_in_dx))
    dx2_txt = "\n".join(p.text for p in _iter_all_paragraphs(dx2))
    dx2_uris = {r.target_ref for r in dx2.part.rels.values() if r.reltype == RT.HYPERLINK}
    assert "causal masking" in dx2_txt and "constraints" in dx2_txt
    assert "https://github.com/Manindra-babu/Decision-IQ" in dx2_uris

    # 6. Multi-resume x Multi-job regression using 3 other resumes + 2 job URLs
    jobs_dir = MANINDRA_PDF.parent.parent / "jobs"
    url_fixtures = {
        "https://boards.greenhouse.io/stripe/jobs/4918201": (jobs_dir / "greenhouse_job.html").read_text(encoding="utf-8"),
        "https://jobs.lever.co/notion/8c3e12f4": (jobs_dir / "lever_job.html").read_text(encoding="utf-8"),
    }

    from app.services.llm.demo_client import DemoLLMClient
    user_r7 = User(email="r7@example.com", hashed_password="x", full_name="R7")
    db_session.add(user_r7)
    db_session.commit()

    extracted_jobs = []
    for j_url, html_body in url_fixtures.items():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_resp.raise_for_status = MagicMock()
        with (
            patch("app.services.jobs.extractor.validate_public_url"),
            patch("app.services.jobs.extractor._check_robots_allowed", return_value=True),
            patch("app.services.jobs.extractor.httpx.get", return_value=mock_resp),
            patch("app.services.jobs.extractor.get_llm_client_for_user", return_value=DemoLLMClient()),
        ):
            jp = extract_job_from_url(j_url, db=db_session, user=user_r7)
        from app.schemas.job import StructuredJobFields
        extracted_jobs.append(
            (
                j_url,
                StructuredJobFields(
                    title=jp.title,
                    company=jp.company,
                    required_skills=list(jp.required_skills),
                    preferred_skills=list(jp.preferred_skills),
                    responsibilities=list(jp.responsibilities),
                    keywords=list(jp.keywords),
                ),
            )
        )

    sample_repo = AnalyzedRepo(
        name="API-Gateway-Demo",
        full_name="testuser/API-Gateway-Demo",
        html_url="https://github.com/testuser/API-Gateway-Demo",
        description="High-throughput REST API gateway built with Python and FastAPI",
        primary_language="Python",
        languages={"Python": 8000, "TypeScript": 2000},
        dependencies=["fastapi", "uvicorn", "redis", "pytest"],
        inferred_skills=["Python", "FastAPI", "Redis", "Docker"],
        readme_excerpt="FastAPI gateway service with Redis rate limiting and PostgreSQL persistence.",
        signals=RepoEvidenceSignals(
            has_readme=True, file_count=18, total_commits=14, user_commits=14, user_commit_share=1.0, has_tests=True
        ),
    )

    single_pdf = MANINDRA_PDF.parent / "sample_resume.pdf"
    two_col_pdf = MANINDRA_PDF.parent / "sample_two_column.pdf"
    assert single_pdf.exists() and two_col_pdf.exists() and manindra_docx.exists()

    # Single-column PDF + Word DOCX must export cleanly across both job URLs
    for j_url, j_fields in extracted_jobs:
        # 1) Single-column PDF
        sp_st, sp_el, sp_raw, _ = parse_pdf_resume(single_pdf)
        sp_recs = rank_repos_for_job([sample_repo], job_skills=j_fields.required_skills, job_keywords=j_fields.keywords, resume_text=sp_raw)
        for r in sp_recs:
            r.decision = "approved"
        sp_chg = generate_tailored_changes(
            resume=sp_st, raw_resume_text=sp_raw, element_map=sp_el, job=j_fields,
            github_repos=[sample_repo], approved_github_recs=sp_recs,
        )
        sp_app = [dict(c, status="accepted") for c in sp_chg if c.get("change_type") != "relevance_trim"]
        sp_out = tmp_path / f"single_{len(sp_app)}_{abs(hash(j_url)) % 9999}.pdf"
        sp_res = apply_changes_to_pdf_in_place(
            source_pdf_path=single_pdf, output_pdf_path=sp_out, approved_changes=sp_app, resume_structure=sp_st
        )
        assert sp_res.page_count_output == 1, (j_url, sp_res.page_fit)
        assert sp_res.overlap_passed is True, (j_url, sp_res.overlap_details)
        ok_sp, errs_sp = verify_exported_pdf_completeness(
            original_pdf_path=single_pdf, output_pdf_path=sp_out, applied_changes=sp_app
        )
        assert ok_sp, (j_url, errs_sp)

        # 2) Word DOCX
        dx_recs = rank_repos_for_job([sample_repo], job_skills=j_fields.required_skills, job_keywords=j_fields.keywords, resume_text=raw_dx)
        for r in dx_recs:
            r.decision = "approved"
        dx_c = generate_tailored_changes(
            resume=st_dx, raw_resume_text=raw_dx, element_map=el_dx, job=j_fields,
            github_repos=[sample_repo], approved_github_recs=dx_recs,
        )
        dx_a = [dict(c, status="accepted") for c in dx_c if c.get("change_type") != "relevance_trim"]
        dx_out = tmp_path / f"word_{abs(hash(j_url)) % 9999}.docx"
        assert apply_changes_to_docx_in_place(source_docx_path=manindra_docx, output_docx_path=dx_out, approved_changes=dx_a) == []

    # 3) Two-column vertical-sidebar PDF: verify completeness/overlaps & document column-divider boundary limitation
    tc_st, tc_el, tc_raw, _ = parse_pdf_resume(two_col_pdf)
    tc_recs = rank_repos_for_job([sample_repo], job_skills=extracted_jobs[0][1].required_skills, job_keywords=extracted_jobs[0][1].keywords, resume_text=tc_raw)
    for r in tc_recs:
        r.decision = "approved"
    tc_chg = generate_tailored_changes(
        resume=tc_st, raw_resume_text=tc_raw, element_map=tc_el, job=extracted_jobs[0][1],
        github_repos=[sample_repo], approved_github_recs=tc_recs,
    )
    tc_app = [dict(c, status="accepted") for c in tc_chg if c.get("change_type") != "relevance_trim"]
    tc_out = tmp_path / "two_col_out.pdf"
    tc_res = apply_changes_to_pdf_in_place(
        source_pdf_path=two_col_pdf, output_pdf_path=tc_out, approved_changes=tc_app, resume_structure=tc_st
    )
    assert tc_res.page_count_output == 1
    assert tc_res.overlap_passed is True, tc_res.overlap_details
    tc_ok, tc_errs = verify_exported_pdf_completeness(
        original_pdf_path=two_col_pdf, output_pdf_path=tc_out, applied_changes=tc_app
    )
    assert tc_ok, tc_errs
