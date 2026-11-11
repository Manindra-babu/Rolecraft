from typing import Any, Callable

from app.core.config import get_settings
from app.core.logging import logger


def enqueue_task(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Dispatch a background task via Redis/RQ when TASK_RUNNER=rq and REDIS_URL is configured,
    or execute inline for local development without Docker/Redis.
    Important: Never pass raw API keys in queue payloads; pass user_id so the worker decrypts in memory.
    """
    settings = get_settings()
    if settings.TASK_RUNNER == "rq" and settings.REDIS_URL:
        try:
            from redis import Redis
            from rq import Queue

            conn = Redis.from_url(settings.REDIS_URL)
            q = Queue("resumeiq", connection=conn)
            job = q.enqueue(func, *args, **kwargs)
            return {"mode": "rq", "job_id": job.id}
        except Exception as exc:
            logger.warning("RQ enqueue failed (%s); falling back to inline execution", type(exc).__name__)
    return {"mode": "inline", "result": func(*args, **kwargs)}
