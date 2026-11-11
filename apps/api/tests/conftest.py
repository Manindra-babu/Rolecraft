import os
from collections.abc import Generator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///./.pytest_bootstrap.db"
os.environ["SECRET_KEY"] = "test-secret-key-at-least-32-characters-long"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["DEMO_MODE"] = "false"

from app.core.config import get_settings  # noqa: E402
from app.core.rate_limit import rate_limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
import app.models  # noqa: E402, F401


@pytest.fixture()
def db_session(tmp_path: Path) -> Generator[Session, None, None]:
    db_file = tmp_path / "test_resumeiq.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session, tmp_path: Path) -> Generator[TestClient, None, None]:
    get_settings.cache_clear()
    rate_limiter.reset()
    os.environ["UPLOAD_DIR"] = str(tmp_path / "uploads")
    os.environ["OUTPUT_DIR"] = str(tmp_path / "outputs")

    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
