"""Follow-up scheduler: determines which professors are due for follow-ups."""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from outreach.config import get_settings
from outreach.models import (
    Draft,
    DraftKind,
    Message,
    MessageStatus,
    Professor,
    ProfessorStatus,
)


def due_followups(
    session: Session,
    now: datetime.datetime,
    interval_days: int | None = None,
    max_followups: int | None = None,
) -> list[dict]:
    """Return professors who are due for a follow-up.

    Pure function of database state and the supplied `now`.

    A professor is due for follow-up if:
    1. Their last SENT message is older than interval_days
    2. They have not replied (status is not REPLIED or CLOSED)
    3. They have fewer than max_followups follow-ups sent
    4. They are in SENT or FOLLOWUP_SENT status

    Returns list of dicts with professor info and context.
    """
    settings = get_settings()
    interval = interval_days if interval_days is not None else settings.followup_interval_days
    max_fu = max_followups if max_followups is not None else settings.max_followups

    cutoff = now - datetime.timedelta(days=interval)

    # Find professors in eligible statuses
    eligible_statuses = [ProfessorStatus.SENT.value, ProfessorStatus.FOLLOWUP_SENT.value]
    professors = (
        session.query(Professor)
        .filter(Professor.status.in_(eligible_statuses))
        .all()
    )

    due = []
    for prof in professors:
        # Count follow-ups already sent
        followup_count = (
            session.query(Message)
            .filter_by(
                professor_id=prof.id,
                kind=DraftKind.FOLLOWUP.value,
                status=MessageStatus.SENT.value,
            )
            .count()
        )

        if followup_count >= max_fu:
            continue

        # Find the last sent message
        last_sent = (
            session.query(Message)
            .filter_by(
                professor_id=prof.id,
                status=MessageStatus.SENT.value,
            )
            .order_by(Message.sent_at.desc())
            .first()
        )

        if last_sent is None:
            continue

        if last_sent.sent_at and last_sent.sent_at <= cutoff:
            # Get the original draft for context
            original_draft = (
                session.query(Draft)
                .filter_by(
                    professor_id=prof.id,
                    kind=DraftKind.INITIAL.value,
                    is_active=1,
                )
                .first()
            )

            due.append({
                "professor_id": prof.id,
                "professor_name": prof.name,
                "professor_email": prof.email,
                "university": prof.university,
                "followup_number": followup_count + 1,
                "last_sent_at": last_sent.sent_at,
                "original_subject": original_draft.subject if original_draft else "",
                "original_body": original_draft.body if original_draft else "",
            })

    return due


def mark_replied(session: Session, professor_id: int) -> bool:
    """Mark a professor as having replied.

    Returns True if successfully transitioned.
    """
    from outreach.statemachine import IllegalTransitionError, transition

    professor = session.get(Professor, professor_id)
    if professor is None:
        return False

    try:
        transition(
            session,
            professor,
            ProfessorStatus.REPLIED,
            detail={"source": "manual"},
        )
        session.commit()
        return True
    except IllegalTransitionError:
        return False
