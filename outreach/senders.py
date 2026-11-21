"""Email senders: DryRunSender, SMTPSender, and the approval gate wrapper."""

from __future__ import annotations

import datetime
import email.mime.text
import logging
import smtplib
from pathlib import Path

from outreach.config import get_settings
from outreach.models import Draft, Message, MessageStatus, Professor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval gate error
# ---------------------------------------------------------------------------

class ApprovalRequiredError(Exception):
    """Raised when trying to send without proper approval."""
    pass


class DuplicateSendError(Exception):
    """Raised when trying to send with an already-used idempotency key."""
    pass


# ---------------------------------------------------------------------------
# EmailSender interface
# ---------------------------------------------------------------------------

class EmailSender:
    """Abstract email sender interface."""

    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        from_email: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        """Send an email. Returns True on success."""
        raise NotImplementedError


class DryRunSender(EmailSender):
    """Writes emails to local outbox/ folder as .eml files. Never sends anything."""

    def __init__(self, outbox_dir: str | None = None):
        settings = get_settings()
        self.outbox_dir = Path(outbox_dir or settings.outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        from_email: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        settings = get_settings()
        from_email = from_email or settings.sender_email
        from_name = from_name or settings.sender_name

        msg = email.mime.text.MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email
        msg["Date"] = email.utils.formatdate(localtime=True)

        # Write to outbox
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_email = to_email.replace("@", "_at_").replace(".", "_")
        filename = f"{timestamp}_{safe_email}.eml"
        filepath = self.outbox_dir / filename

        with open(filepath, "w") as f:
            f.write(msg.as_string())

        logger.info(f"[DRY RUN] Email written to {filepath}")
        return True


class SMTPSender(EmailSender):
    """Real SMTP email sender. Only used when SEND_MODE=smtp."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        settings = get_settings()
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.user = user or settings.smtp_user
        self.password = password or settings.smtp_password

    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        from_email: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        settings = get_settings()
        from_email = from_email or settings.sender_email
        from_name = from_name or settings.sender_name

        msg = email.mime.text.MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email
        msg["Date"] = email.utils.formatdate(localtime=True)

        with smtplib.SMTP(self.host, self.port) as server:
            server.starttls()
            server.login(self.user, self.password)
            server.send_message(msg)

        logger.info(f"[SMTP] Email sent to {to_email}")
        return True


# ---------------------------------------------------------------------------
# Approval-gated sender wrapper
# ---------------------------------------------------------------------------

class ApprovalGatedSender:
    """Wraps an EmailSender with approval gate and idempotency checks.

    This is the ONLY way emails should be sent. It enforces:
    1. Draft must have approved_at set
    2. approved_hash must equal current text_hash
    3. Idempotency key must not already be SENT
    4. Claims the idempotency key as SENDING before attempting
    """

    def __init__(self, sender: EmailSender, session):
        self.sender = sender
        self.session = session

    def send_draft(self, draft: Draft, professor: Professor) -> Message:
        """Send an approved draft with all safety checks.

        Returns the Message record.
        Raises ApprovalRequiredError if draft is not properly approved.
        Raises DuplicateSendError if idempotency key already used.
        """
        # CHECK 1: Draft must be approved
        if draft.approved_at is None:
            raise ApprovalRequiredError(
                f"Draft {draft.id} has not been approved. approved_at is None."
            )

        # CHECK 2: Approved hash must match current text hash
        current_hash = Draft.compute_hash(draft.subject, draft.body)
        if draft.approved_hash != current_hash:
            raise ApprovalRequiredError(
                f"Draft {draft.id} was edited after approval. "
                f"approved_hash={draft.approved_hash}, current_hash={current_hash}. "
                f"Please re-approve."
            )

        # Build idempotency key
        idem_key = Message.make_idempotency_key(
            professor.id, draft.kind, draft.sequence
        )

        # CHECK 3: Check for existing message with this key
        existing = (
            self.session.query(Message)
            .filter_by(idempotency_key=idem_key)
            .first()
        )
        if existing:
            if existing.status == MessageStatus.SENT.value:
                raise DuplicateSendError(
                    f"Message with key {idem_key} already sent"
                )
            if existing.status == MessageStatus.SENDING.value:
                # This means a previous attempt crashed mid-send
                raise DuplicateSendError(
                    f"Message with key {idem_key} is in SENDING state "
                    f"(possible crash recovery needed)"
                )

        # Claim the idempotency key
        message = Message(
            professor_id=professor.id,
            idempotency_key=idem_key,
            kind=draft.kind,
            sequence=draft.sequence,
            draft_id=draft.id,
            status=MessageStatus.SENDING.value,
            subject=draft.subject,
            body=draft.body,
        )
        self.session.add(message)
        self.session.commit()  # Must commit to claim the key

        # Attempt to send
        try:
            success = self.sender.send(
                to_email=professor.email,
                subject=draft.subject,
                body=draft.body,
            )
            if success:
                message.status = MessageStatus.SENT.value
                message.sent_at = datetime.datetime.now(datetime.timezone.utc)
                self.session.commit()
                logger.info(f"Message {idem_key} sent successfully")
                return message
            else:
                message.status = MessageStatus.FAILED.value
                message.error = "Sender returned False"
                self.session.commit()
                return message

        except Exception as e:
            # If we crash here, the SENDING row will remain.
            # On restart, this will be detected and moved to NEEDS_HUMAN.
            message.status = MessageStatus.FAILED.value
            message.error = str(e)
            try:
                self.session.commit()
            except Exception:
                pass  # If commit fails, the SENDING row stays
            raise


def get_sender() -> EmailSender:
    """Get the appropriate sender based on configuration."""
    settings = get_settings()
    if settings.is_smtp:
        return SMTPSender()
    return DryRunSender()


def recover_sending_messages(session) -> list[dict]:
    """Find messages stuck in SENDING state and move them to NEEDS_HUMAN.

    Returns list of recovery actions taken.
    """
    from outreach.models import Message, MessageStatus, Professor, ProfessorStatus
    from outreach.statemachine import transition

    stuck = (
        session.query(Message)
        .filter_by(status=MessageStatus.SENDING.value)
        .all()
    )

    actions = []
    for msg in stuck:
        professor = session.get(Professor, msg.professor_id)
        actions.append({
            "message_id": msg.id,
            "idempotency_key": msg.idempotency_key,
            "professor_id": msg.professor_id,
            "professor_name": professor.name if professor else "unknown",
            "action": "moved_to_needs_human",
        })
        msg.status = MessageStatus.FAILED.value
        msg.error = "Found in SENDING state on recovery - possible crash during send"
        if professor:
            try:
                transition(
                    session,
                    professor,
                    ProfessorStatus.NEEDS_HUMAN,
                    detail={"reason": "crash_recovery", "message_key": msg.idempotency_key},
                )
            except Exception as e:
                logger.warning(f"Could not transition professor {professor.id} to NEEDS_HUMAN: {e}")

    if actions:
        session.commit()
        logger.warning(f"Recovered {len(actions)} stuck SENDING messages")

    return actions
