"""SQLAlchemy models for the outreach agent."""

from __future__ import annotations

import datetime
import enum
import hashlib
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProfessorStatus(str, enum.Enum):
    NEW = "NEW"
    PAPER_FETCHED = "PAPER_FETCHED"
    DRAFTED = "DRAFTED"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FOLLOWUP_DRAFTED = "FOLLOWUP_DRAFTED"
    FOLLOWUP_APPROVED = "FOLLOWUP_APPROVED"
    FOLLOWUP_SENT = "FOLLOWUP_SENT"
    REPLIED = "REPLIED"
    CLOSED = "CLOSED"
    # Failure statuses
    FAILED_FETCH = "FAILED_FETCH"
    FAILED_DRAFT = "FAILED_DRAFT"
    FAILED_SEND = "FAILED_SEND"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class MessageStatus(str, enum.Enum):
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class DraftKind(str, enum.Enum):
    INITIAL = "initial"
    FOLLOWUP = "followup"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def utc_now() -> datetime.datetime:
    """Current UTC datetime."""
    return datetime.datetime.now(datetime.timezone.utc)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    applied_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )


class Professor(Base):
    __tablename__ = "professors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    university: Mapped[str] = mapped_column(String(255), nullable=False)
    link: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[ProfessorStatus] = mapped_column(
        String(30), nullable=False, default=ProfessorStatus.NEW
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )
    followup_count: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    papers: Mapped[list["Paper"]] = relationship("Paper", back_populates="professor")
    drafts: Mapped[list["Draft"]] = relationship("Draft", back_populates="professor")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="professor")
    events: Mapped[list["Event"]] = relationship("Event", back_populates="professor")
    step_attempts: Mapped[list["StepAttempt"]] = relationship(
        "StepAttempt", back_populates="professor"
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    professor_id: Mapped[int] = mapped_column(Integer, ForeignKey("professors.id"), nullable=False)
    arxiv_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    semantic_scholar_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    introduction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # "arxiv" or "semantic_scholar"
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )

    professor: Mapped["Professor"] = relationship("Professor", back_populates="papers")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    professor_id: Mapped[int] = mapped_column(Integer, ForeignKey("professors.id"), nullable=False)
    kind: Mapped[DraftKind] = mapped_column(String(20), nullable=False, default=DraftKind.INITIAL)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    referenced_claim: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claim_source_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    judge_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    approved_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )
    is_active: Mapped[bool] = mapped_column(Integer, default=True)  # SQLite has no bool

    professor: Mapped["Professor"] = relationship("Professor", back_populates="drafts")

    @staticmethod
    def compute_hash(subject: str, body: str) -> str:
        """Compute SHA-256 hash of draft text."""
        content = f"{subject}\n{body}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def refresh_hash(self) -> None:
        """Recompute text_hash from current subject and body."""
        self.text_hash = self.compute_hash(self.subject, self.body)

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None and self.approved_hash == self.text_hash

    def approve(self) -> None:
        """Mark this draft as approved with the current text hash."""
        self.approved_at = utc_now()
        self.approved_hash = self.text_hash

    def void_approval(self) -> None:
        """Void approval (called when text changes after approval)."""
        self.approved_at = None
        self.approved_hash = None


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    professor_id: Mapped[int] = mapped_column(Integer, ForeignKey("professors.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    kind: Mapped[DraftKind] = mapped_column(String(20), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_id: Mapped[int] = mapped_column(Integer, ForeignKey("drafts.id"), nullable=False)
    status: Mapped[MessageStatus] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )

    professor: Mapped["Professor"] = relationship("Professor", back_populates="messages")

    @staticmethod
    def make_idempotency_key(professor_id: int, kind: str, sequence: int) -> str:
        return f"{professor_id}:{kind}:{sequence}"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    professor_id: Mapped[int] = mapped_column(Integer, ForeignKey("professors.id"), nullable=False)
    from_status: Mapped[str] = mapped_column(String(30), nullable=False)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string

    professor: Mapped["Professor"] = relationship("Professor", back_populates="events")


class StepAttempt(Base):
    __tablename__ = "step_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    professor_id: Mapped[int] = mapped_column(Integer, ForeignKey("professors.id"), nullable=False)
    step: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    ok: Mapped[bool] = mapped_column(Integer, nullable=False)  # SQLite bool
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utc_now
    )

    professor: Mapped["Professor"] = relationship("Professor", back_populates="step_attempts")
