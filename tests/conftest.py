"""Shared test fixtures."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from outreach.models import (
    Base,
    Paper,
    Professor,
    ProfessorStatus,
)


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def professor(db_session: Session) -> Professor:
    """Create a sample professor in NEW status."""
    prof = Professor(
        name="Alice Smith",
        email="alice@example.edu",
        university="Example University",
        link="https://arxiv.org/abs/2301.00001",
        status=ProfessorStatus.NEW.value,
    )
    db_session.add(prof)
    db_session.commit()
    return prof


@pytest.fixture
def paper(db_session: Session, professor: Professor) -> Paper:
    """Create a sample paper."""
    p = Paper(
        professor_id=professor.id,
        arxiv_id="2301.00001",
        title="Advances in Neural Network Pruning",
        authors="Alice Smith, Bob Jones",
        abstract="We present a novel approach to neural network pruning that reduces model size by 90% while maintaining 95% accuracy. Our method uses structured pruning combined with knowledge distillation.",
        introduction="Neural network pruning has become increasingly important as models grow larger. In this work, we propose a structured pruning approach that identifies and removes redundant neurons. Our key insight is that knowledge distillation can compensate for the accuracy loss from aggressive pruning.",
        pdf_url="https://arxiv.org/pdf/2301.00001",
        source="arxiv",
    )
    db_session.add(p)
    db_session.commit()
    return p


@pytest.fixture
def sample_profile() -> dict:
    return {
        "name": "Jane Doe",
        "year": "Junior",
        "college": "MIT",
        "email": "jane@mit.edu",
        "projects": [
            "Built a transformer-based code summarization tool using T5 (Python, PyTorch)",
            "Developed a distributed key-value store with Raft consensus (Go)",
            "Created CourtroomAI: LLM-as-judge for argument evaluation (Python, FastAPI)",
        ],
        "github": "https://github.com/janedoe",
        "linkedin": "https://linkedin.com/in/janedoe",
    }


@pytest.fixture
def tmp_outbox(tmp_path):
    """Temporary outbox directory."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    return outbox


@pytest.fixture
def tmp_profile(tmp_path, sample_profile):
    """Write sample profile to a temp file."""
    import yaml

    profile_path = tmp_path / "profile.yaml"
    with open(profile_path, "w") as f:
        yaml.dump(sample_profile, f)
    return str(profile_path)
