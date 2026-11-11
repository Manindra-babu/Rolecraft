import hashlib
import math
import re
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from app.services.llm.base import EmbeddingClient, LLMClient

T = TypeVar("T", bound=BaseModel)


class DemoLLMClient(LLMClient):
    """Deterministic offline LLM client used when DEMO_MODE=true."""

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: Type[T],
        operation: str,
        tier: str = "fast",
        system_instruction: Optional[str] = None,
        redact_pii: bool = True,
    ) -> T:
        if operation == "job_extraction":
            return schema.model_validate(self._demo_job_extraction(prompt))

        data: dict[str, Any] = {}
        for name, field in schema.model_fields.items():
            ann = field.annotation
            if ann is str:
                data[name] = ""
            elif ann is float:
                data[name] = 0.8
            elif ann is int:
                data[name] = 1
            elif ann is bool:
                data[name] = True
            elif hasattr(ann, "__origin__") and ann.__origin__ is list:
                data[name] = []
            elif hasattr(ann, "__origin__") and ann.__origin__ is dict:
                data[name] = {}
        return schema.model_validate(data)

    @staticmethod
    def _demo_job_extraction(prompt: str) -> dict[str, Any]:
        title = ""
        company = ""
        m_title = re.search(r"Title hint:\s*(.+)", prompt)
        if m_title:
            title = m_title.group(1).strip()
        m_comp = re.search(r"Company hint:\s*(.+)", prompt)
        if m_comp:
            company = m_comp.group(1).strip()
        if not title:
            body = prompt.split("JOB TEXT:", 1)[-1].strip()
            for line in body.splitlines():
                clean = line.strip()
                if clean and 2 <= len(clean.split()) <= 10:
                    title = clean
                    break
        return {
            "title": title,
            "company": company,
            "location": "",
            "experience_level": "",
            "responsibilities": [],
            "responsibilities_or_soft_requirements": [],
            "required_skills": [],
            "preferred_skills": [],
            "skills": [],
            "or_groups": [],
            "keywords": [],
        }


class DemoEmbeddingClient(EmbeddingClient):
    """Deterministic bag-of-characters hash embedding for offline DEMO_MODE."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        dims = 64
        for text in texts:
            vec = [0.0] * dims
            for token in re.findall(r"[a-z0-9+#.]+", text.lower()):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                idx = digest[0] % dims
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors
