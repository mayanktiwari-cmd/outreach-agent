"""Tests for Phase 7: Follow-up scheduler with fake clock."""

from __future__ import annotations

import datetime

import pytest

from outreach.followups import due_followups, mark_replied
from outreach.models import (
    Draft,
    DraftKind,
    Message,
    MessageStatus,
    Professor,
    ProfessorStatus,
)
from outreach.statemachine import transition


@pytest.fixture
def sent_professor(db_session) -> Professor:
    """Create a professor in SENT status with a sent message."""
    prof = Professor(
        name="Follow Up Test",
        email="followup@test.edu",
        university="Test University",
        status=ProfessorStatus.NEW.value,
    )
    db_session.add(prof)
    db_session.flush()

    # Walk through the state machine
    transition(db_session, prof, ProfessorStatus.PAPER_FETCHED)
    transition(db_session, prof, ProfessorStatus.DRAFTED)
    transition(db_session, prof, ProfessorStatus.APPROVED)
    transition(db_session, prof, ProfessorStatus.SENT)

    # Create the sent message and original draft
    draft = Draft(
        professor_id=prof.id,
        kind=DraftKind.INITIAL.value,
        sequence=1,
        subject="Initial Email",
        body="Initial email body.",
        text_hash=Draft.compute_hash("Initial Email", "Initial email body."),
        is_active=1,
    )
    draft.approve()
    db_session.add(draft)

    msg = Message(
        professor_id=prof.id,
        idempotency_key=f"{prof.id}:initial:1",
        kind=DraftKind.INITIAL.value,
        sequence=1,
        draft_id=1,
        status=MessageStatus.SENT.value,
        subject="Initial Email",
        body="Initial email body.",
        sent_at=datetime.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(msg)
    db_session.commit()
    return prof


class TestFollowupScheduler:
    """Test follow-up scheduling with fake clock."""

    def test_no_followup_before_interval(self, db_session, sent_professor):
        """No follow-up due if interval hasn't passed."""
        # Check 3 days after send (before 7-day interval)
        now = datetime.datetime(2024, 1, 4, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=7)
        assert len(due) == 0

    def test_followup_after_interval(self, db_session, sent_professor):
        """Follow-up is due after the interval passes."""
        # Check 8 days after send (after 7-day interval)
        now = datetime.datetime(2024, 1, 9, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=7)
        assert len(due) == 1
        assert due[0]["professor_id"] == sent_professor.id
        assert due[0]["followup_number"] == 1

    def test_no_followup_after_reply(self, db_session, sent_professor):
        """No follow-up if professor has replied."""
        transition(db_session, sent_professor, ProfessorStatus.REPLIED)
        db_session.commit()

        now = datetime.datetime(2024, 1, 15, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=7)
        assert len(due) == 0

    def test_no_followup_after_max(self, db_session, sent_professor):
        """No follow-up after max follow-ups sent."""
        # Create 2 follow-up messages (the max)
        for i in range(1, 3):
            msg = Message(
                professor_id=sent_professor.id,
                idempotency_key=f"{sent_professor.id}:followup:{i}",
                kind=DraftKind.FOLLOWUP.value,
                sequence=i,
                draft_id=1,
                status=MessageStatus.SENT.value,
                subject=f"Follow-up {i}",
                body=f"Follow-up {i} body.",
                sent_at=datetime.datetime(2024, 1, 1 + 7 * i, 12, 0, 0),
            )
            db_session.add(msg)

        # Move to FOLLOWUP_SENT
        transition(db_session, sent_professor, ProfessorStatus.FOLLOWUP_DRAFTED)
        transition(db_session, sent_professor, ProfessorStatus.FOLLOWUP_APPROVED)
        transition(db_session, sent_professor, ProfessorStatus.FOLLOWUP_SENT)
        db_session.commit()

        now = datetime.datetime(2024, 2, 1, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=7, max_followups=2)
        assert len(due) == 0

    def test_followup_respects_custom_interval(self, db_session, sent_professor):
        """Custom interval is respected."""
        # 5 days after, with 3-day interval
        now = datetime.datetime(2024, 1, 6, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=3)
        assert len(due) == 1

    def test_multiple_professors_mixed(self, db_session, sent_professor):
        """Multiple professors: some due, some not."""
        # Add another professor who sent recently
        prof2 = Professor(
            name="Recent Sender",
            email="recent@test.edu",
            university="Test U",
            status=ProfessorStatus.NEW.value,
        )
        db_session.add(prof2)
        db_session.flush()
        transition(db_session, prof2, ProfessorStatus.PAPER_FETCHED)
        transition(db_session, prof2, ProfessorStatus.DRAFTED)
        transition(db_session, prof2, ProfessorStatus.APPROVED)
        transition(db_session, prof2, ProfessorStatus.SENT)

        msg2 = Message(
            professor_id=prof2.id,
            idempotency_key=f"{prof2.id}:initial:1",
            kind=DraftKind.INITIAL.value,
            sequence=1,
            draft_id=1,
            status=MessageStatus.SENT.value,
            subject="Recent Email",
            body="Recent email body.",
            sent_at=datetime.datetime(2024, 1, 8, 12, 0, 0),  # Sent recently
        )
        db_session.add(msg2)
        db_session.commit()

        # Check at day 10: first professor should be due, second should not
        now = datetime.datetime(2024, 1, 10, 12, 0, 0)
        due = due_followups(db_session, now, interval_days=7)
        assert len(due) == 1
        assert due[0]["professor_id"] == sent_professor.id


class TestMarkReplied:
    def test_mark_replied_success(self, db_session, sent_professor):
        result = mark_replied(db_session, sent_professor.id)
        assert result is True
        db_session.refresh(sent_professor)
        assert sent_professor.status == ProfessorStatus.REPLIED.value

    def test_mark_replied_nonexistent(self, db_session):
        result = mark_replied(db_session, 999)
        assert result is False

    def test_mark_replied_wrong_status(self, db_session):
        """Can't mark as replied from NEW."""
        prof = Professor(
            name="New Prof",
            email="new@test.edu",
            university="Test U",
            status=ProfessorStatus.NEW.value,
        )
        db_session.add(prof)
        db_session.commit()

        result = mark_replied(db_session, prof.id)
        assert result is False
