"""Tests for Phase 6: FastAPI endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from outreach.api import app, get_db
from outreach.models import (
    Base,
    Draft,
    DraftKind,
    Professor,
    ProfessorStatus,
)


@pytest.fixture
def api_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    session = TestingSessionLocal()
    # Seed a professor
    prof = Professor(
        name="Dr. API Test",
        email="api@university.edu",
        university="API University",
        link="https://arxiv.org/abs/2301.99999",
        status=ProfessorStatus.DRAFTED.value,
    )
    session.add(prof)
    session.flush()

    draft = Draft(
        professor_id=prof.id,
        kind=DraftKind.INITIAL.value,
        sequence=1,
        subject="Initial Collaboration",
        body="Dear Dr. API, I read your paper on neural networks.",
        text_hash=Draft.compute_hash(
            "Initial Collaboration",
            "Dear Dr. API, I read your paper on neural networks.",
        ),
        is_active=1,
    )
    session.add(draft)
    session.commit()
    session.close()

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_list_professors(api_client):
    response = api_client.get("/api/professors")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "Dr. API Test"


def test_get_professor(api_client):
    response = api_client.get("/api/professors/1")
    assert response.status_code == 200
    assert response.json()["email"] == "api@university.edu"


def test_list_drafts(api_client):
    response = api_client.get("/api/drafts")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["subject"] == "Initial Collaboration"
    assert data[0]["is_approved"] is False


def test_approve_draft(api_client):
    response = api_client.post("/api/drafts/1/approve")
    assert response.status_code == 200
    data = response.json()
    assert data["is_approved"] is True
    assert data["approved_at"] is not None

    # Check professor status transitioned to APPROVED
    prof_resp = api_client.get("/api/professors/1")
    assert prof_resp.json()["status"] == "APPROVED"


def test_edit_draft_invalidates_approval(api_client):
    # First approve
    api_client.post("/api/drafts/1/approve")
    # Verify approved
    draft_resp = api_client.get("/api/drafts/1")
    assert draft_resp.json()["is_approved"] is True

    # Now edit
    edit_resp = api_client.put(
        "/api/drafts/1",
        json={"body": "Updated body that changes the content and hash."},
    )
    assert edit_resp.status_code == 200
    assert edit_resp.json()["is_approved"] is False

    # Check professor status went back to DRAFTED
    prof_resp = api_client.get("/api/professors/1")
    assert prof_resp.json()["status"] == "DRAFTED"


def test_reject_draft(api_client):
    response = api_client.post("/api/drafts/1/reject")
    assert response.status_code == 200

    prof_resp = api_client.get("/api/professors/1")
    assert prof_resp.json()["status"] == "NEEDS_HUMAN"


def test_status_summary(api_client):
    response = api_client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert "DRAFTED" in data["by_status"]


def test_mark_replied(api_client):
    # Advance professor to SENT first
    api_client.post("/api/drafts/1/approve")
    # Mark replied directly requires eligible transition: from SENT or FOLLOWUP_SENT
    # If currently APPROVED, mark replied will fail legal transitions
    response = api_client.post("/api/professors/1/mark-replied")
    assert response.status_code == 400
