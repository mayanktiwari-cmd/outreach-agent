"""State machine for professor outreach status transitions.

Every transition is explicitly enumerated. Illegal transitions raise ValueError.
Every transition is recorded in the events table.
"""

from __future__ import annotations

import datetime
import json
from typing import Optional

from sqlalchemy.orm import Session

from outreach.models import Event, Professor, ProfessorStatus

# ---------------------------------------------------------------------------
# Legal transitions: frozenset of (from_status, to_status)
# ---------------------------------------------------------------------------

LEGAL_TRANSITIONS: frozenset[tuple[ProfessorStatus, ProfessorStatus]] = frozenset(
    [
        # Happy path: initial email
        (ProfessorStatus.NEW, ProfessorStatus.PAPER_FETCHED),
        (ProfessorStatus.PAPER_FETCHED, ProfessorStatus.DRAFTED),
        (ProfessorStatus.DRAFTED, ProfessorStatus.APPROVED),
        (ProfessorStatus.APPROVED, ProfessorStatus.SENT),
        # Happy path: follow-ups
        (ProfessorStatus.SENT, ProfessorStatus.FOLLOWUP_DRAFTED),
        (ProfessorStatus.FOLLOWUP_SENT, ProfessorStatus.FOLLOWUP_DRAFTED),
        (ProfessorStatus.FOLLOWUP_DRAFTED, ProfessorStatus.FOLLOWUP_APPROVED),
        (ProfessorStatus.FOLLOWUP_APPROVED, ProfessorStatus.FOLLOWUP_SENT),
        # Terminal states from sent
        (ProfessorStatus.SENT, ProfessorStatus.REPLIED),
        (ProfessorStatus.SENT, ProfessorStatus.CLOSED),
        (ProfessorStatus.FOLLOWUP_SENT, ProfessorStatus.REPLIED),
        (ProfessorStatus.FOLLOWUP_SENT, ProfessorStatus.CLOSED),
        # Failure transitions from any active status
        (ProfessorStatus.NEW, ProfessorStatus.FAILED_FETCH),
        (ProfessorStatus.PAPER_FETCHED, ProfessorStatus.FAILED_DRAFT),
        (ProfessorStatus.APPROVED, ProfessorStatus.FAILED_SEND),
        (ProfessorStatus.FOLLOWUP_APPROVED, ProfessorStatus.FAILED_SEND),
        # Needs human from various states
        (ProfessorStatus.NEW, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.PAPER_FETCHED, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.DRAFTED, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.APPROVED, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.SENT, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FOLLOWUP_DRAFTED, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FOLLOWUP_APPROVED, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FOLLOWUP_SENT, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FAILED_FETCH, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FAILED_DRAFT, ProfessorStatus.NEEDS_HUMAN),
        (ProfessorStatus.FAILED_SEND, ProfessorStatus.NEEDS_HUMAN),
        # Recovery from failure: retry
        (ProfessorStatus.FAILED_FETCH, ProfessorStatus.NEW),
        (ProfessorStatus.FAILED_DRAFT, ProfessorStatus.PAPER_FETCHED),
        (ProfessorStatus.FAILED_SEND, ProfessorStatus.APPROVED),
        # Recovery from needs_human
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.NEW),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.PAPER_FETCHED),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.DRAFTED),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.APPROVED),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.SENT),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.FOLLOWUP_DRAFTED),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.FOLLOWUP_APPROVED),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.FOLLOWUP_SENT),
        (ProfessorStatus.NEEDS_HUMAN, ProfessorStatus.CLOSED),
        # Close from anywhere problematic
        (ProfessorStatus.NEW, ProfessorStatus.CLOSED),
        (ProfessorStatus.PAPER_FETCHED, ProfessorStatus.CLOSED),
        (ProfessorStatus.DRAFTED, ProfessorStatus.CLOSED),
        (ProfessorStatus.APPROVED, ProfessorStatus.CLOSED),
        (ProfessorStatus.FOLLOWUP_DRAFTED, ProfessorStatus.CLOSED),
        (ProfessorStatus.FOLLOWUP_APPROVED, ProfessorStatus.CLOSED),
        (ProfessorStatus.FAILED_FETCH, ProfessorStatus.CLOSED),
        (ProfessorStatus.FAILED_DRAFT, ProfessorStatus.CLOSED),
        (ProfessorStatus.FAILED_SEND, ProfessorStatus.CLOSED),
        # Void approval (edited after approval) goes back to DRAFTED
        (ProfessorStatus.APPROVED, ProfessorStatus.DRAFTED),
        (ProfessorStatus.FOLLOWUP_APPROVED, ProfessorStatus.FOLLOWUP_DRAFTED),
    ]
)


class IllegalTransitionError(ValueError):
    """Raised when a state transition is not allowed."""

    def __init__(self, from_status: ProfessorStatus, to_status: ProfessorStatus):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Illegal transition: {from_status.value} -> {to_status.value}"
        )


def is_legal(from_status: ProfessorStatus, to_status: ProfessorStatus) -> bool:
    """Check if a transition is legal."""
    return (from_status, to_status) in LEGAL_TRANSITIONS


def transition(
    session: Session,
    professor: Professor,
    to_status: ProfessorStatus,
    detail: Optional[dict] = None,
) -> None:
    """Transition a professor to a new status.

    Records an event and raises IllegalTransitionError if the transition is not allowed.
    """
    from_status = ProfessorStatus(professor.status)

    if not is_legal(from_status, to_status):
        raise IllegalTransitionError(from_status, to_status)

    # Record the event
    event = Event(
        professor_id=professor.id,
        from_status=from_status.value,
        to_status=to_status.value,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        detail=json.dumps(detail) if detail else None,
    )
    session.add(event)

    # Update the professor status
    professor.status = to_status.value
    professor.updated_at = datetime.datetime.now(datetime.timezone.utc)

    session.flush()
