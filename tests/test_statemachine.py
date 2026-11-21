"""Tests for Phase 1: State machine, models, and DB setup."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from outreach.models import Base, Event, Professor, ProfessorStatus
from outreach.statemachine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    is_legal,
    transition,
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
        status=ProfessorStatus.NEW,
    )
    db_session.add(prof)
    db_session.commit()
    return prof


class TestLegalTransitions:
    """Test that legal transitions are accepted."""

    def test_new_to_paper_fetched(self, db_session, professor):
        transition(db_session, professor, ProfessorStatus.PAPER_FETCHED)
        assert professor.status == ProfessorStatus.PAPER_FETCHED

    def test_full_happy_path(self, db_session, professor):
        """Walk through the entire happy path for initial email."""
        statuses = [
            ProfessorStatus.PAPER_FETCHED,
            ProfessorStatus.DRAFTED,
            ProfessorStatus.APPROVED,
            ProfessorStatus.SENT,
        ]
        for s in statuses:
            transition(db_session, professor, s)
        assert professor.status == ProfessorStatus.SENT

    def test_followup_path(self, db_session, professor):
        """Walk through the follow-up path after SENT."""
        for s in [
            ProfessorStatus.PAPER_FETCHED,
            ProfessorStatus.DRAFTED,
            ProfessorStatus.APPROVED,
            ProfessorStatus.SENT,
            ProfessorStatus.FOLLOWUP_DRAFTED,
            ProfessorStatus.FOLLOWUP_APPROVED,
            ProfessorStatus.FOLLOWUP_SENT,
        ]:
            transition(db_session, professor, s)
        assert professor.status == ProfessorStatus.FOLLOWUP_SENT

    def test_sent_to_replied(self, db_session, professor):
        transition(db_session, professor, ProfessorStatus.PAPER_FETCHED)
        transition(db_session, professor, ProfessorStatus.DRAFTED)
        transition(db_session, professor, ProfessorStatus.APPROVED)
        transition(db_session, professor, ProfessorStatus.SENT)
        transition(db_session, professor, ProfessorStatus.REPLIED)
        assert professor.status == ProfessorStatus.REPLIED

    def test_failure_and_recovery(self, db_session, professor):
        """Test failure then recovery path."""
        transition(db_session, professor, ProfessorStatus.FAILED_FETCH)
        assert professor.status == ProfessorStatus.FAILED_FETCH
        # Recovery: back to NEW
        transition(db_session, professor, ProfessorStatus.NEW)
        assert professor.status == ProfessorStatus.NEW

    def test_needs_human_and_recovery(self, db_session, professor):
        transition(db_session, professor, ProfessorStatus.NEEDS_HUMAN)
        transition(db_session, professor, ProfessorStatus.NEW)
        assert professor.status == ProfessorStatus.NEW


class TestIllegalTransitions:
    """Test that illegal transitions are rejected."""

    def test_new_to_sent_is_illegal(self, db_session, professor):
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition(db_session, professor, ProfessorStatus.SENT)
        assert "NEW" in str(exc_info.value)
        assert "SENT" in str(exc_info.value)

    def test_new_to_approved_is_illegal(self, db_session, professor):
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.APPROVED)

    def test_sent_to_drafted_is_illegal(self, db_session, professor):
        """Can't go from SENT back to DRAFTED (must go through FOLLOWUP)."""
        for s in [
            ProfessorStatus.PAPER_FETCHED,
            ProfessorStatus.DRAFTED,
            ProfessorStatus.APPROVED,
            ProfessorStatus.SENT,
        ]:
            transition(db_session, professor, s)
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.DRAFTED)

    def test_replied_is_terminal(self, db_session, professor):
        """REPLIED is terminal - can't transition out."""
        for s in [
            ProfessorStatus.PAPER_FETCHED,
            ProfessorStatus.DRAFTED,
            ProfessorStatus.APPROVED,
            ProfessorStatus.SENT,
            ProfessorStatus.REPLIED,
        ]:
            transition(db_session, professor, s)
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.NEW)

    def test_closed_is_terminal(self, db_session, professor):
        transition(db_session, professor, ProfessorStatus.CLOSED)
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.NEW)

    def test_new_to_drafted_skips_paper_fetch(self, db_session, professor):
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.DRAFTED)


class TestEventLogging:
    """Test that every transition creates an event record."""

    def test_event_created_on_transition(self, db_session, professor):
        transition(db_session, professor, ProfessorStatus.PAPER_FETCHED)
        db_session.commit()

        events = db_session.query(Event).filter_by(professor_id=professor.id).all()
        assert len(events) == 1
        assert events[0].from_status == ProfessorStatus.NEW.value
        assert events[0].to_status == ProfessorStatus.PAPER_FETCHED.value

    def test_event_detail_stored(self, db_session, professor):
        transition(
            db_session,
            professor,
            ProfessorStatus.PAPER_FETCHED,
            detail={"paper_id": "2301.00001"},
        )
        db_session.commit()

        event = db_session.query(Event).filter_by(professor_id=professor.id).first()
        assert '"paper_id"' in event.detail

    def test_multiple_events_for_multiple_transitions(self, db_session, professor):
        transitions = [
            ProfessorStatus.PAPER_FETCHED,
            ProfessorStatus.DRAFTED,
            ProfessorStatus.APPROVED,
            ProfessorStatus.SENT,
        ]
        for s in transitions:
            transition(db_session, professor, s)
        db_session.commit()

        events = db_session.query(Event).filter_by(professor_id=professor.id).all()
        assert len(events) == 4

    def test_no_event_on_illegal_transition(self, db_session, professor):
        with pytest.raises(IllegalTransitionError):
            transition(db_session, professor, ProfessorStatus.SENT)
        # Flush shouldn't have happened, but check anyway
        events = db_session.query(Event).filter_by(professor_id=professor.id).all()
        assert len(events) == 0


class TestTransitionTable:
    """Meta tests on the transition table itself."""

    def test_no_self_transitions(self):
        """No transition from a status to itself."""
        for from_s, to_s in LEGAL_TRANSITIONS:
            assert from_s != to_s, f"Self-transition found: {from_s}"

    def test_terminal_states_have_no_outgoing(self):
        """REPLIED and CLOSED should have no outgoing transitions."""
        terminal = {ProfessorStatus.REPLIED, ProfessorStatus.CLOSED}
        for from_s, to_s in LEGAL_TRANSITIONS:
            assert from_s not in terminal, (
                f"Terminal state {from_s} has outgoing transition to {to_s}"
            )

    def test_all_statuses_reachable(self):
        """Every status should be reachable from some other status."""
        reachable = {to_s for _, to_s in LEGAL_TRANSITIONS}
        # NEW is the initial state, doesn't need to be reachable via transition
        all_statuses = set(ProfessorStatus) - {ProfessorStatus.NEW}
        unreachable = all_statuses - reachable
        assert not unreachable, f"Unreachable statuses: {unreachable}"


class TestIsLegal:
    def test_legal_returns_true(self):
        assert is_legal(ProfessorStatus.NEW, ProfessorStatus.PAPER_FETCHED) is True

    def test_illegal_returns_false(self):
        assert is_legal(ProfessorStatus.NEW, ProfessorStatus.SENT) is False
