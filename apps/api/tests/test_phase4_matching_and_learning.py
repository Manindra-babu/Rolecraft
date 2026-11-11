from app.schemas.github import AnalyzedRepo, RepoEvidenceSignals
from app.schemas.resume import ParsedExperienceItem, ParsedResumeStructure
from app.services.learning.roadmap import build_learning_plan
from app.services.llm.demo_client import DemoEmbeddingClient
from app.services.matching.normalizer import normalize_skill, skills_match_via_alias
from app.services.matching.scorer import assess_skills_and_score


def test_skill_normalization_and_aliases() -> None:
    assert skills_match_via_alias("Postgres", "PostgreSQL")
    assert skills_match_via_alias("REST APIs", "RESTful services")
    assert skills_match_via_alias("JS", "JavaScript")
    assert normalize_skill("K8s") == "kubernetes"


def test_five_category_skill_assessment_and_explainable_score() -> None:
    resume = ParsedResumeStructure(
        full_name="Priya Sharma",
        experience=[
            ParsedExperienceItem(
                company="CloudScale",
                title="Intern",
                bullets=["Built RESTful services using Python and Postgres with unit tests."],
            )
        ],
        skills=["Python", "PostgreSQL", "Git", "Redis"],  # Redis is in Skills only -> under_represented
    )
    gh_repo = AnalyzedRepo(
        name="docker-ci-demo",
        full_name="priya/docker-ci-demo",
        html_url="https://github.com/priya/docker-ci-demo",
        inferred_skills=["Docker", "Python"],
        signals=RepoEvidenceSignals(has_dockerfile=True, file_count=10, user_commits=10, total_commits=10),
        quality_score=80.0,
    )

    breakdown, assessments = assess_skills_and_score(
        resume=resume,
        job_required_skills=["Python", "PostgreSQL", "REST APIs", "Redis", "Docker", "Kubernetes"],
        job_preferred_skills=[],
        job_responsibilities=["Design REST APIs in Python and PostgreSQL"],
        job_keywords=["Python", "PostgreSQL", "Docker", "Kubernetes"],
        github_repos=[gh_repo],
        embedder=DemoEmbeddingClient(),
    )

    by_skill = {a.skill_name: a for a in assessments}
    assert by_skill["Python"].category == "demonstrated"
    assert by_skill["PostgreSQL"].category == "demonstrated"
    assert by_skill["REST APIs"].category == "demonstrated"
    assert by_skill["Redis"].category == "under_represented"
    assert by_skill["Docker"].category == "github_only"
    assert by_skill["Kubernetes"].category == "missing"

    assert 0.0 <= breakdown.overall_score <= 100.0
    assert "Matched:" in breakdown.required_skills.explanation
    assert "Missing:" in breakdown.required_skills.explanation


def test_learning_plan_resources_and_unverified_handling() -> None:
    docker_plan = build_learning_plan(
        skill_name="Docker",
        category="missing",
        job_title="Backend Engineer",
    )
    assert docker_plan.resources
    assert all(r.verified_url for r in docker_plan.resources)
    assert "docs.docker.com" in docker_plan.resources[0].url

    obscure_plan = build_learning_plan(
        skill_name="CustomProprietaryToolXYZ",
        category="missing",
        job_title="Backend Engineer",
    )
    assert obscure_plan.resources[0].verified_url is False
    assert obscure_plan.resources[0].url == ""
