"""Tests for Phase 4: Senders, approval gate, idempotency, crash recovery."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from outreach.models import (
    Draft,
    DraftKind,
    Message,
    MessageStatus,
    Professor,
    ProfessorStatus,
)
from outreach.senders import (
    ApprovalGatedSender,
    ApprovalRequiredError,
    DryRunSender,
    DuplicateSendError,
    recover_sending_messages,
)


@pytest.fixture
def approved_professor(db_session: Session) -> tuple[Professor, Draft]:
    """Create a professor with an approved draft."""
    prof = Professor(
        name="Bob Test",
        email="bob@test.edu",
        university="Test University",
        status=ProfessorStatus.APPROVED.value,
    )
    db_session.add(prof)
    db_session.flush()

    draft = Draft(
        professor_id=prof.id,
        kind=DraftKind.INITIAL.value,
        sequence=1,
        subject="Test Subject",
        body="Test body content.",
        text_hash=Draft.compute_hash("Test Subject", "Test body content."),
        is_active=1,
    )
    draft.approve()
    db_session.add(draft)
    db_session.commit()
    return prof, draft


class TestApprovalGate:
    """Tests for the approval gate - the most critical safety check."""

    def test_send_without_approval_refused(self, db_session, tmp_outbox):
        """CRITICAL: Sending without approval must be refused."""
        prof = Professor(
            name="Test Prof",
            email="test@test.edu",
            university="Test U",
            status=ProfessorStatus.DRAFTED.value,
        )
        db_session.add(prof)
        db_session.flush()

        # Draft is NOT approved
        draft = Draft(
            professor_id=prof.id,
            kind=DraftKind.INITIAL.value,
            sequence=1,
            subject="Subject",
            body="Body",
            text_hash=Draft.compute_hash("Subject", "Body"),
            is_active=1,
        )
        db_session.add(draft)
        db_session.commit()

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        with pytest.raises(ApprovalRequiredError) as exc_info:
            gated.send_draft(draft, prof)
        assert "not been approved" in str(exc_info.value)

    def test_send_after_edit_refused(self, db_session, approved_professor, tmp_outbox):
        """CRITICAL: Editing after approval voids it; send must be refused."""
        prof, draft = approved_professor
        assert draft.is_approved  # Sanity check

        # Edit the draft body
        draft.body = "Changed body content."
        draft.refresh_hash()
        # Note: we do NOT re-approve

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        with pytest.raises(ApprovalRequiredError) as exc_info:
            gated.send_draft(draft, prof)
        assert "edited after approval" in str(exc_info.value)

    def test_send_after_edit_and_reapprove_works(self, db_session, approved_professor, tmp_outbox):
        """Editing then re-approving should allow send."""
        prof, draft = approved_professor

        # Edit and re-approve
        draft.body = "Changed body content."
        draft.refresh_hash()
        draft.approve()  # Re-approve with new hash
        db_session.commit()

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)
        message = gated.send_draft(draft, prof)
        assert message.status == MessageStatus.SENT.value

    def test_successful_dry_run_send(self, db_session, approved_professor, tmp_outbox):
        """Test successful send in dry run mode."""
        prof, draft = approved_professor

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        message = gated.send_draft(draft, prof)
        assert message.status == MessageStatus.SENT.value
        assert message.sent_at is not None

        # Check .eml file was created
        eml_files = list(tmp_outbox.glob("*.eml"))
        assert len(eml_files) == 1

    def test_bypass_attempt_at_sender_level(self, db_session, tmp_outbox):
        """Test that the approval gate cannot be bypassed even with direct sender access.

        This is the approval gate bypass test from the spec.
        The ApprovalGatedSender enforces checks INSIDE the wrapper,
        not just at the API layer.
        """
        prof = Professor(
            name="Bypass Test",
            email="bypass@test.edu",
            university="Test U",
            status=ProfessorStatus.DRAFTED.value,
        )
        db_session.add(prof)
        db_session.flush()

        # Unapproved draft
        draft = Draft(
            professor_id=prof.id,
            kind=DraftKind.INITIAL.value,
            sequence=1,
            subject="Bypass Subject",
            body="Bypass Body",
            text_hash=Draft.compute_hash("Bypass Subject", "Bypass Body"),
            is_active=1,
        )
        db_session.add(draft)
        db_session.commit()

        # Even though we have a valid sender, the gate refuses
        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        with pytest.raises(ApprovalRequiredError):
            gated.send_draft(draft, prof)

        # Verify no .eml file was created
        eml_files = list(tmp_outbox.glob("*.eml"))
        assert len(eml_files) == 0


class TestIdempotency:
    """Tests for idempotency keys and duplicate prevention."""

    def test_duplicate_send_refused(self, db_session, approved_professor, tmp_outbox):
        """Sending with the same idempotency key twice must fail."""
        prof, draft = approved_professor

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        # First send succeeds
        message = gated.send_draft(draft, prof)
        assert message.status == MessageStatus.SENT.value

        # Second send with same key must fail
        with pytest.raises(DuplicateSendError):
            gated.send_draft(draft, prof)

    def test_idempotency_key_format(self):
        """Test idempotency key format."""
        key = Message.make_idempotency_key(42, "initial", 1)
        assert key == "42:initial:1"

        key2 = Message.make_idempotency_key(42, "followup", 2)
        assert key2 == "42:followup:2"

    def test_sending_row_detected_on_recovery(self, db_session, approved_professor, tmp_outbox):
        """A SENDING row blocks re-send and raises DuplicateSendError."""
        prof, draft = approved_professor

        # Simulate a crash: manually insert a SENDING message
        idem_key = Message.make_idempotency_key(prof.id, draft.kind, draft.sequence)
        msg = Message(
            professor_id=prof.id,
            idempotency_key=idem_key,
            kind=draft.kind,
            sequence=draft.sequence,
            draft_id=draft.id,
            status=MessageStatus.SENDING.value,
            subject=draft.subject,
            body=draft.body,
        )
        db_session.add(msg)
        db_session.commit()

        sender = DryRunSender(outbox_dir=str(tmp_outbox))
        gated = ApprovalGatedSender(sender, db_session)

        # Attempting to send with existing SENDING key must fail
        with pytest.raises(DuplicateSendError) as exc_info:
            gated.send_draft(draft, prof)
        assert "SENDING" in str(exc_info.value)


class TestCrashRecovery:
    """Tests for crash recovery: SENDING rows become NEEDS_HUMAN."""

    def test_recover_sending_messages(self, db_session):
        """Stuck SENDING messages should be recovered to NEEDS_HUMAN."""
        prof = Professor(
            name="Crash Test",
            email="crash@test.edu",
            university="Test U",
            status=ProfessorStatus.APPROVED.value,
        )
        db_session.add(prof)
        db_session.flush()

        # Create a stuck SENDING message
        msg = Message(
            professor_id=prof.id,
            idempotency_key=f"{prof.id}:initial:1",
            kind=DraftKind.INITIAL.value,
            sequence=1,
            draft_id=1,  # Doesn't matter for this test
            status=MessageStatus.SENDING.value,
            subject="Test",
            body="Test",
        )
        db_session.add(msg)
        db_session.commit()

        actions = recover_sending_messages(db_session)
        assert len(actions) == 1
        assert actions[0]["action"] == "moved_to_needs_human"

        # Verify message is now FAILED
        db_session.refresh(msg)
        assert msg.status == MessageStatus.FAILED.value
        assert "crash" in msg.error.lower()

        # Verify professor is NEEDS_HUMAN
        db_session.refresh(prof)
        assert prof.status == ProfessorStatus.NEEDS_HUMAN.value

    def test_no_recovery_needed(self, db_session):
        """When no SENDING messages exist, recovery is a no-op."""
        actions = recover_sending_messages(db_session)
        assert len(actions) == 0
