from pydantic import BaseModel, Field

from app.schemas.matching import LearningPlanDetail, LearningResource, LearningStep
from app.services.llm.base import LLMClient
from app.services.llm.demo_client import DemoLLMClient
from app.services.matching.normalizer import normalize_skill

VERIFIED_RESOURCE_CATALOG: dict[str, list[LearningResource]] = {
    "docker": [
        LearningResource(
            title="Docker Official Getting Started Guide",
            provider="Docker Docs",
            url="https://docs.docker.com/get-started/",
            verified_url=True,
            note="Official hands-on container and Dockerfile guide",
        ),
        LearningResource(
            title="Docker Compose Documentation",
            provider="Docker Docs",
            url="https://docs.docker.com/compose/",
            verified_url=True,
        ),
    ],
    "kubernetes": [
        LearningResource(
            title="Learn Kubernetes Basics",
            provider="Kubernetes Official Docs",
            url="https://kubernetes.io/docs/tutorials/kubernetes-basics/",
            verified_url=True,
        ),
    ],
    "fastapi": [
        LearningResource(
            title="FastAPI Official Tutorial — User Guide",
            provider="FastAPI Docs",
            url="https://fastapi.tiangolo.com/tutorial/",
            verified_url=True,
        ),
    ],
    "postgresql": [
        LearningResource(
            title="PostgreSQL Official Tutorial",
            provider="PostgreSQL Docs",
            url="https://www.postgresql.org/docs/current/tutorial.html",
            verified_url=True,
        ),
    ],
    "redis": [
        LearningResource(
            title="Redis Official Documentation & Data Types",
            provider="Redis Docs",
            url="https://redis.io/docs/latest/",
            verified_url=True,
        ),
    ],
    "typescript": [
        LearningResource(
            title="The TypeScript Handbook",
            provider="TypeScript Official Docs",
            url="https://www.typescriptlang.org/docs/handbook/intro.html",
            verified_url=True,
        ),
    ],
    "react": [
        LearningResource(
            title="Learn React — Official Documentation",
            provider="React Docs",
            url="https://react.dev/learn",
            verified_url=True,
        ),
    ],
    "next.js": [
        LearningResource(
            title="Next.js App Router Foundations",
            provider="Next.js Docs",
            url="https://nextjs.org/learn",
            verified_url=True,
        ),
    ],
    "aws": [
        LearningResource(
            title="AWS Skill Builder — Cloud Practitioner Essentials",
            provider="AWS Training",
            url="https://explore.skillbuilder.aws/learn",
            verified_url=True,
        ),
    ],
    "pytest": [
        LearningResource(
            title="pytest Official Getting Started Guide",
            provider="pytest Docs",
            url="https://docs.pytest.org/en/stable/getting-started.html",
            verified_url=True,
        ),
    ],
    "ci/cd": [
        LearningResource(
            title="GitHub Actions Quickstart",
            provider="GitHub Docs",
            url="https://docs.github.com/en/actions/quickstart",
            verified_url=True,
        ),
    ],
}

VALUABLE_CERTS: dict[str, list[str]] = {
    "aws": ["AWS Certified Cloud Practitioner (optional for entry-level cloud roles)"],
    "kubernetes": ["Certified Kubernetes Application Developer (CKAD) after hands-on experience"],
}


class GeneratedRoadmapSchema(BaseModel):
    technologies: list[str] = Field(default_factory=list)
    ordered_steps: list[LearningStep] = Field(default_factory=list)
    project_ideas: list[str] = Field(default_factory=list)
    how_to_demonstrate: str = ""


def build_learning_plan(
    *,
    skill_name: str,
    category: str,
    job_title: str,
    llm: LLMClient | None = None,
) -> LearningPlanDetail:
    """
    Generate an evidence-backed learning plan for categories 4 ('needs_development')
    and 5 ('missing'). Never fabricates URLs.
    """
    norm = normalize_skill(skill_name)
    resources = list(VERIFIED_RESOURCE_CATALOG.get(norm, []))
    if not resources:
        resources.append(
            LearningResource(
                title=f"Official {skill_name} Documentation",
                provider="Official Docs",
                url="",
                verified_url=False,
                note="Link not pre-verified — search for the official documentation directly.",
            )
        )

    certs = list(VALUABLE_CERTS.get(norm, []))

    default_steps = [
        LearningStep(
            id=f"{norm}-1",
            title=f"Core concepts of {skill_name}",
            description=f"Read the official {skill_name} getting-started documentation and understand core primitives.",
            estimated_hours=4,
        ),
        LearningStep(
            id=f"{norm}-2",
            title=f"Build a small hands-on {skill_name} exercise",
            description=f"Apply {skill_name} inside a standalone repository with automated tests and a clear README.",
            estimated_hours=6,
        ),
        LearningStep(
            id=f"{norm}-3",
            title=f"Document and ship on GitHub",
            description=f"Push the working {skill_name} project to GitHub with clear setup instructions so it can serve as verified resume evidence.",
            estimated_hours=3,
        ),
    ]
    default_projects = [
        f"Build a focused microservice or CLI tool demonstrating {skill_name} with tests and documentation."
    ]
    default_demo = (
        f"After completing a working project using {skill_name} and pushing tests/docs to GitHub, "
        f"add it as a verified project on your resume. Until then, only list it under 'Currently learning' if actively studying."
    )

    if llm is not None and not isinstance(llm, DemoLLMClient):
        try:
            prompt = (
                f"Create a concise, practical 3-step learning plan for '{skill_name}' "
                f"for a candidate targeting '{job_title}'. "
                "Do NOT include external URLs; focus on concepts, ordered steps, 1-2 realistic hands-on project ideas, "
                "and how to demonstrate the skill honestly after building it."
            )
            generated = llm.generate_structured(
                prompt=prompt,
                schema=GeneratedRoadmapSchema,
                operation="learning_plan",
                tier="fast",
                redact_pii=False,
            )
            if generated.ordered_steps:
                default_steps = generated.ordered_steps[:5]
            if generated.project_ideas:
                default_projects = generated.project_ideas[:3]
            if generated.how_to_demonstrate:
                default_demo = generated.how_to_demonstrate
        except Exception:
            pass

    return LearningPlanDetail(
        skill_name=skill_name,
        category=category,
        technologies=[skill_name],
        ordered_steps=default_steps,
        resources=resources,
        project_ideas=default_projects,
        certifications=certs,
        how_to_demonstrate=default_demo,
    )
