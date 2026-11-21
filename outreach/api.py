"""FastAPI endpoints for the outreach agent."""

from __future__ import annotations

import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from outreach.db import get_session, init_db
from outreach.models import (
    Draft,
    DraftKind,
    Event,
    Message,
    Professor,
    ProfessorStatus,
)
from outreach.statemachine import IllegalTransitionError, transition


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Outreach Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependency
def get_db():
    session = get_session()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProfessorOut(BaseModel):
    id: int
    name: str
    email: str
    university: str
    link: Optional[str] = None
    status: str
    followup_count: int = 0
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None

    model_config = {"from_attributes": True}


class DraftOut(BaseModel):
    id: int
    professor_id: int
    kind: str
    sequence: int
    subject: str
    body: str
    referenced_claim: Optional[str] = None
    claim_source_quote: Optional[str] = None
    judge_score: Optional[float] = None
    text_hash: str
    approved_at: Optional[datetime.datetime] = None
    approved_hash: Optional[str] = None
    is_active: bool = True
    is_approved: bool = False
    professor_name: Optional[str] = None
    professor_email: Optional[str] = None
    university: Optional[str] = None

    model_config = {"from_attributes": True}


class DraftEditRequest(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


class EventOut(BaseModel):
    id: int
    professor_id: int
    from_status: str
    to_status: str
    timestamp: Optional[datetime.datetime] = None
    detail: Optional[str] = None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    professor_id: int
    idempotency_key: str
    kind: str
    sequence: int
    status: str
    subject: str
    body: str
    sent_at: Optional[datetime.datetime] = None
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class StatusSummary(BaseModel):
    total: int
    by_status: dict[str, int]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/professors", response_model=list[ProfessorOut])
def list_professors(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Professor)
    if status:
        query = query.filter_by(status=status)
    return query.order_by(Professor.id).all()


@app.get("/api/professors/{professor_id}", response_model=ProfessorOut)
def get_professor(professor_id: int, db: Session = Depends(get_db)):
    prof = db.get(Professor, professor_id)
    if not prof:
        raise HTTPException(status_code=404, detail="Professor not found")
    return prof


@app.get("/api/drafts", response_model=list[DraftOut])
def list_drafts(
    status: Optional[str] = None,
    pending_only: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(Draft).filter_by(is_active=1)

    if pending_only:
        query = query.filter(Draft.approved_at.is_(None))

    drafts = query.order_by(Draft.created_at.desc()).all()

    result = []
    for d in drafts:
        prof = db.get(Professor, d.professor_id)
        if status and prof and prof.status != status:
            continue
        out = DraftOut.model_validate(d)
        out.is_approved = d.is_approved
        if prof:
            out.professor_name = prof.name
            out.professor_email = prof.email
            out.university = prof.university
        result.append(out)

    return result


@app.get("/api/drafts/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    out = DraftOut.model_validate(draft)
    out.is_approved = draft.is_approved
    prof = db.get(Professor, draft.professor_id)
    if prof:
        out.professor_name = prof.name
        out.professor_email = prof.email
        out.university = prof.university
    return out


@app.post("/api/drafts/{draft_id}/approve", response_model=DraftOut)
def approve_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft.approve()
    db.flush()

    # Transition professor status
    prof = db.get(Professor, draft.professor_id)
    if prof:
        try:
            if draft.kind == DraftKind.INITIAL.value:
                transition(db, prof, ProfessorStatus.APPROVED)
            else:
                transition(db, prof, ProfessorStatus.FOLLOWUP_APPROVED)
        except IllegalTransitionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    db.commit()

    out = DraftOut.model_validate(draft)
    out.is_approved = draft.is_approved
    return out


@app.put("/api/drafts/{draft_id}", response_model=DraftOut)
def edit_draft(draft_id: int, req: DraftEditRequest, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    if req.subject is not None:
        draft.subject = req.subject
    if req.body is not None:
        draft.body = req.body

    # Recompute hash and void approval
    draft.refresh_hash()
    if draft.approved_hash != draft.text_hash:
        draft.void_approval()
        # If professor was APPROVED, move back to DRAFTED
        prof = db.get(Professor, draft.professor_id)
        if prof and prof.status in (ProfessorStatus.APPROVED.value, ProfessorStatus.FOLLOWUP_APPROVED.value):
            try:
                target = (
                    ProfessorStatus.DRAFTED
                    if prof.status == ProfessorStatus.APPROVED.value
                    else ProfessorStatus.FOLLOWUP_DRAFTED
                )
                transition(db, prof, target, detail={"reason": "draft_edited"})
            except IllegalTransitionError:
                pass

    db.commit()

    out = DraftOut.model_validate(draft)
    out.is_approved = draft.is_approved
    return out


@app.post("/api/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    draft.is_active = 0
    draft.void_approval()

    prof = db.get(Professor, draft.professor_id)
    if prof:
        try:
            transition(
                db,
                prof,
                ProfessorStatus.NEEDS_HUMAN,
                detail={"reason": "draft_rejected"},
            )
        except IllegalTransitionError:
            pass

    db.commit()
    return {"status": "rejected", "draft_id": draft_id}


@app.get("/api/status", response_model=StatusSummary)
def get_status(db: Session = Depends(get_db)):
    professors = db.query(Professor).all()
    by_status: dict[str, int] = {}
    for p in professors:
        s = p.status if isinstance(p.status, str) else p.status.value
        by_status[s] = by_status.get(s, 0) + 1
    return StatusSummary(total=len(professors), by_status=by_status)


@app.get("/api/events", response_model=list[EventOut])
def list_events(professor_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Event)
    if professor_id:
        query = query.filter_by(professor_id=professor_id)
    return query.order_by(Event.timestamp.desc()).limit(100).all()


@app.get("/api/messages", response_model=list[MessageOut])
def list_messages(professor_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Message)
    if professor_id:
        query = query.filter_by(professor_id=professor_id)
    return query.order_by(Message.created_at.desc()).all()


@app.post("/api/professors/{professor_id}/mark-replied")
def mark_replied_endpoint(professor_id: int, db: Session = Depends(get_db)):
    from outreach.followups import mark_replied

    success = mark_replied(db, professor_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot mark as replied")
    return {"status": "replied", "professor_id": professor_id}


@app.post("/api/send/{professor_id}")
def send_professor(professor_id: int, db: Session = Depends(get_db)):
    from outreach.runner import send_approved

    result = send_approved(db, professor_id=professor_id)
    if result["errors"]:
        raise HTTPException(status_code=400, detail=result["errors"][0])
    return result
