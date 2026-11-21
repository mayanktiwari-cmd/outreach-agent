"""Resumable batch runner for processing professors."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from outreach.config import get_settings
from outreach.drafter import (
    GroqLLMClient,
    LLMClient,
    generate_draft,
    load_profile,
)
from outreach.models import (
    Draft,
    DraftKind,
    MessageStatus,
    Paper,
    Professor,
    ProfessorStatus,
    StepAttempt,
)
from outreach.papers import extract_pdf_text, fetch_paper_for_professor
from outreach.senders import (
    ApprovalGatedSender,
    get_sender,
    recover_sending_messages,
)
from outreach.statemachine import transition

logger = logging.getLogger(__name__)


def import_professors(session: Session, csv_path: str) -> list[Professor]:
    """Import professors from a CSV file. Skips duplicates by email."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    imported = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name", "").strip()
            em = row.get("email", "").strip()
            uni = row.get("university", "").strip()
            link = row.get("link", "").strip()

            if not name or not em:
                logger.warning(f"Skipping row with missing name or email: {row}")
                continue

            # Check for duplicate
            existing = session.query(Professor).filter_by(email=em).first()
            if existing:
                logger.info(f"Skipping duplicate: {em}")
                continue

            prof = Professor(
                name=name,
                email=em,
                university=uni,
                link=link,
                status=ProfessorStatus.NEW.value,
            )
            session.add(prof)
            imported.append(prof)

    session.commit()
    logger.info(f"Imported {len(imported)} professors")
    return imported


def process_professor_paper(
    session: Session, professor: Professor
) -> Optional[Paper]:
    """Fetch a paper for a professor and store it."""
    if professor.status != ProfessorStatus.NEW.value:
        logger.info(f"Skipping paper fetch for {professor.name}: status is {professor.status}")
        return None

    result = fetch_paper_for_professor(
        name=professor.name,
        link=professor.link or "",
        professor_id=professor.id,
        session=session,
        step_attempt_cls=StepAttempt,
    )

    if result is None:
        transition(
            session,
            professor,
            ProfessorStatus.FAILED_FETCH,
            detail={"reason": "all_sources_failed"},
        )
        session.commit()
        return None

    # Try to get introduction from PDF
    introduction = None
    if result.get("pdf_url"):
        try:
            introduction = extract_pdf_text(result["pdf_url"])
        except Exception as e:
            logger.warning(f"PDF extraction failed for {professor.name}: {e}")

    paper = Paper(
        professor_id=professor.id,
        arxiv_id=result.get("arxiv_id"),
        semantic_scholar_id=result.get("semantic_scholar_id"),
        title=result["title"],
        authors=result["authors"],
        abstract=result["abstract"],
        introduction=introduction,
        pdf_url=result.get("pdf_url"),
        source=result["source"],
    )
    session.add(paper)

    transition(
        session,
        professor,
        ProfessorStatus.PAPER_FETCHED,
        detail={"paper_title": result["title"]},
    )
    session.commit()
    return paper


def process_professor_draft(
    session: Session,
    professor: Professor,
    llm: LLMClient,
    profile: dict,
) -> Optional[Draft]:
    """Generate a draft email for a professor."""
    if professor.status != ProfessorStatus.PAPER_FETCHED.value:
        logger.info(f"Skipping draft for {professor.name}: status is {professor.status}")
        return None

    paper = (
        session.query(Paper)
        .filter_by(professor_id=professor.id)
        .order_by(Paper.fetched_at.desc())
        .first()
    )
    if paper is None:
        transition(
            session,
            professor,
            ProfessorStatus.FAILED_DRAFT,
            detail={"reason": "no_paper_found"},
        )
        session.commit()
        return None

    result = generate_draft(
        llm=llm,
        professor_name=professor.name,
        university=professor.university,
        paper_title=paper.title,
        paper_authors=paper.authors,
        paper_abstract=paper.abstract,
        paper_introduction=paper.introduction,
        profile=profile,
        professor_id=professor.id,
        session=session,
        step_attempt_cls=StepAttempt,
    )

    if result.get("needs_human"):
        transition(
            session,
            professor,
            ProfessorStatus.NEEDS_HUMAN,
            detail={"reason": result.get("error", "grounding_failed")},
        )
        session.commit()
        return None

    if result.get("error"):
        transition(
            session,
            professor,
            ProfessorStatus.FAILED_DRAFT,
            detail={"reason": result["error"]},
        )
        session.commit()
        return None

    draft = Draft(
        professor_id=professor.id,
        kind=DraftKind.INITIAL.value,
        sequence=1,
        subject=result["subject"],
        body=result["body"],
        referenced_claim=result.get("referenced_claim"),
        claim_source_quote=result.get("claim_source_quote"),
        judge_score=result.get("judge_score"),
        text_hash=Draft.compute_hash(result["subject"], result["body"]),
        is_active=1,
    )
    session.add(draft)

    transition(
        session,
        professor,
        ProfessorStatus.DRAFTED,
        detail={"draft_subject": result["subject"]},
    )
    session.commit()
    return draft


def run_batch(
    session: Session,
    llm: LLMClient | None = None,
    profile_path: str | None = None,
    skip_paper_fetch: bool = False,
    skip_draft: bool = False,
) -> dict:
    """Run the batch processor: fetch papers, generate drafts.

    Resumable: re-running after a crash picks up from persisted state.
    One professor failing does not stop the rest.
    """
    # Crash recovery first
    recovery_actions = recover_sending_messages(session)
    if recovery_actions:
        logger.warning(f"Crash recovery: {len(recovery_actions)} messages handled")

    settings = get_settings()
    if llm is None:
        llm = GroqLLMClient()

    profile = load_profile(profile_path or settings.profile_path)

    stats = {
        "total": 0,
        "papers_fetched": 0,
        "drafts_created": 0,
        "failed": 0,
        "skipped": 0,
        "recovery_actions": len(recovery_actions),
    }

    professors = session.query(Professor).all()
    stats["total"] = len(professors)

    for prof in professors:
        try:
            # Step 1: Fetch paper if needed
            if not skip_paper_fetch and prof.status == ProfessorStatus.NEW.value:
                paper = process_professor_paper(session, prof)
                if paper:
                    stats["papers_fetched"] += 1
                else:
                    stats["failed"] += 1
                    continue

            # Step 2: Generate draft if needed
            if not skip_draft and prof.status == ProfessorStatus.PAPER_FETCHED.value:
                draft = process_professor_draft(session, prof, llm, profile)
                if draft:
                    stats["drafts_created"] += 1
                else:
                    stats["failed"] += 1
                    continue

            # Other statuses are skipped (already processed)
            if prof.status not in (
                ProfessorStatus.NEW.value,
                ProfessorStatus.PAPER_FETCHED.value,
            ):
                stats["skipped"] += 1

        except Exception as e:
            logger.error(f"Error processing {prof.name}: {e}")
            stats["failed"] += 1
            try:
                # Try to mark as NEEDS_HUMAN
                transition(
                    session,
                    prof,
                    ProfessorStatus.NEEDS_HUMAN,
                    detail={"reason": f"batch_error: {str(e)}"},
                )
                session.commit()
            except Exception:
                logger.error(f"Could not mark {prof.name} as NEEDS_HUMAN")

    return stats


def send_approved(session: Session, professor_id: int | None = None) -> dict:
    """Send all approved drafts (or one specific professor's draft)."""
    sender = get_sender()
    gated = ApprovalGatedSender(sender, session)

    query = session.query(Professor)
    if professor_id:
        query = query.filter_by(id=professor_id)

    # Find approved professors
    approved_statuses = [ProfessorStatus.APPROVED.value, ProfessorStatus.FOLLOWUP_APPROVED.value]
    professors = query.filter(Professor.status.in_(approved_statuses)).all()

    stats = {"sent": 0, "failed": 0, "errors": []}

    for prof in professors:
        # Find the active approved draft
        draft = (
            session.query(Draft)
            .filter_by(professor_id=prof.id, is_active=1)
            .order_by(Draft.created_at.desc())
            .first()
        )

        if draft is None or not draft.is_approved:
            stats["errors"].append(f"{prof.name}: no approved draft found")
            continue

        try:
            message = gated.send_draft(draft, prof)
            if message.status == MessageStatus.SENT.value:
                # Determine target status
                if prof.status == ProfessorStatus.APPROVED.value:
                    target = ProfessorStatus.SENT
                else:
                    target = ProfessorStatus.FOLLOWUP_SENT
                    prof.followup_count += 1

                transition(session, prof, target)
                session.commit()
                stats["sent"] += 1
            else:
                stats["failed"] += 1
                stats["errors"].append(f"{prof.name}: send returned non-SENT status")
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{prof.name}: {str(e)}")
            logger.error(f"Failed to send for {prof.name}: {e}")

    return stats
