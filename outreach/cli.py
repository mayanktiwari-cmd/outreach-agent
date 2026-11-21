"""Typer CLI for the outreach agent."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from outreach.config import get_settings
from outreach.db import get_session, init_db

console = Console()
app = typer.Typer(name="outreach", help="Research internship outreach agent")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@app.command()
def init():
    """Initialize the database."""
    init_db()
    console.print("[green]Database initialized.[/green]")


@app.command(name="import")
def import_cmd(csv_path: str = typer.Argument(..., help="Path to professors CSV")):
    """Import professors from a CSV file."""
    from outreach.runner import import_professors

    init_db()
    session = get_session()
    try:
        professors = import_professors(session, csv_path)
        console.print(f"[green]Imported {len(professors)} professors[/green]")
        for p in professors:
            console.print(f"  {p.name} <{p.email}> ({p.university})")
    finally:
        session.close()


@app.command()
def run(
    profile: str = typer.Option("profile.yaml", help="Path to profile YAML"),
    skip_paper: bool = typer.Option(False, help="Skip paper fetching"),
    skip_draft: bool = typer.Option(False, help="Skip draft generation"),
):
    """Run the batch processor: fetch papers and generate drafts."""
    from outreach.runner import run_batch

    init_db()
    session = get_session()
    try:
        stats = run_batch(
            session,
            profile_path=profile,
            skip_paper_fetch=skip_paper,
            skip_draft=skip_draft,
        )
        console.print("\n[bold]Batch Results:[/bold]")
        for key, val in stats.items():
            console.print(f"  {key}: {val}")
    finally:
        session.close()


@app.command()
def review():
    """Review pending drafts."""
    from outreach.models import Draft, Professor

    init_db()
    session = get_session()
    try:
        drafts = (
            session.query(Draft)
            .filter_by(is_active=1)
            .filter(Draft.approved_at.is_(None))
            .all()
        )

        if not drafts:
            console.print("[yellow]No pending drafts to review.[/yellow]")
            return

        table = Table(title="Pending Drafts")
        table.add_column("ID", style="cyan")
        table.add_column("Professor", style="green")
        table.add_column("Subject", style="white")
        table.add_column("Words", style="yellow")
        table.add_column("Judge", style="magenta")
        table.add_column("Kind", style="blue")

        for d in drafts:
            prof = session.get(Professor, d.professor_id)
            name = prof.name if prof else "?"
            words = str(len(d.body.split()))
            judge = f"{d.judge_score:.2f}" if d.judge_score else "N/A"
            table.add_row(str(d.id), name, d.subject[:50], words, judge, d.kind)

        console.print(table)
        console.print(f"\nTotal: {len(drafts)} pending drafts")
        console.print("Use [cyan]outreach approve <id>[/cyan] to approve a draft")
        console.print("Use [cyan]outreach show <id>[/cyan] to view full draft")
    finally:
        session.close()


@app.command()
def show(draft_id: int = typer.Argument(..., help="Draft ID to show")):
    """Show a draft's full content."""
    from outreach.models import Draft, Professor

    init_db()
    session = get_session()
    try:
        draft = session.get(Draft, draft_id)
        if not draft:
            console.print(f"[red]Draft {draft_id} not found[/red]")
            return

        prof = session.get(Professor, draft.professor_id)

        console.print(f"\n[bold cyan]Draft #{draft.id}[/bold cyan]")
        console.print(f"[bold]To:[/bold] {prof.name if prof else '?'} <{prof.email if prof else '?'}>")
        console.print(f"[bold]Subject:[/bold] {draft.subject}")
        console.print(f"[bold]Kind:[/bold] {draft.kind} (seq {draft.sequence})")
        console.print(f"[bold]Approved:[/bold] {'✓' if draft.is_approved else '✗'}")
        console.print(f"[bold]Judge Score:[/bold] {draft.judge_score or 'N/A'}")
        console.print(f"[bold]Hash:[/bold] {draft.text_hash[:16]}...")
        console.print("\n[bold]Body:[/bold]")
        console.print(draft.body)
        if draft.referenced_claim:
            console.print(f"\n[dim]Referenced claim: {draft.referenced_claim}[/dim]")
        if draft.claim_source_quote:
            console.print(f"[dim]Source quote: {draft.claim_source_quote[:100]}...[/dim]")
    finally:
        session.close()


@app.command()
def approve(draft_id: int = typer.Argument(..., help="Draft ID to approve")):
    """Approve a draft for sending."""
    from outreach.models import Draft, DraftKind, Professor, ProfessorStatus
    from outreach.statemachine import transition

    init_db()
    session = get_session()
    try:
        draft = session.get(Draft, draft_id)
        if not draft:
            console.print(f"[red]Draft {draft_id} not found[/red]")
            return

        draft.approve()

        prof = session.get(Professor, draft.professor_id)
        if prof:
            target = (
                ProfessorStatus.APPROVED
                if draft.kind == DraftKind.INITIAL.value
                else ProfessorStatus.FOLLOWUP_APPROVED
            )
            try:
                transition(session, prof, target)
            except Exception as e:
                console.print(f"[red]Transition error: {e}[/red]")
                return

        session.commit()
        console.print(f"[green]Draft {draft_id} approved ✓[/green]")
    finally:
        session.close()


@app.command()
def send(
    professor_id: Optional[int] = typer.Argument(None, help="Professor ID (omit for all approved)"),
    confirm: bool = typer.Option(False, "--confirm", help="Confirm sending"),
):
    """Send approved drafts."""
    from outreach.runner import send_approved

    settings = get_settings()
    if settings.is_smtp and not confirm:
        console.print(
            "[red]SEND_MODE=smtp requires --confirm flag to prevent accidental sends[/red]"
        )
        return

    init_db()
    session = get_session()
    try:
        result = send_approved(session, professor_id=professor_id)
        console.print("\n[bold]Send Results:[/bold]")
        console.print(f"  Sent: {result['sent']}")
        console.print(f"  Failed: {result['failed']}")
        for err in result.get("errors", []):
            console.print(f"  [red]Error: {err}[/red]")

        if settings.is_dry_run:
            console.print(f"\n[yellow]Mode: DRY RUN. Emails saved to {settings.outbox_dir}/[/yellow]")
    finally:
        session.close()


@app.command()
def status():
    """Show status summary."""
    from outreach.models import Professor

    init_db()
    session = get_session()
    try:
        professors = session.query(Professor).all()
        by_status: dict[str, int] = {}
        for p in professors:
            s = p.status if isinstance(p.status, str) else p.status.value
            by_status[s] = by_status.get(s, 0) + 1

        table = Table(title="Professor Status Summary")
        table.add_column("Status", style="cyan")
        table.add_column("Count", style="green")

        for s, c in sorted(by_status.items()):
            table.add_row(s, str(c))

        console.print(table)
        console.print(f"\nTotal: {len(professors)}")
    finally:
        session.close()


@app.command(name="mark-replied")
def mark_replied_cmd(
    professor_id: int = typer.Argument(..., help="Professor ID"),
):
    """Mark a professor as having replied."""
    from outreach.followups import mark_replied

    init_db()
    session = get_session()
    try:
        success = mark_replied(session, professor_id)
        if success:
            console.print(f"[green]Professor {professor_id} marked as REPLIED[/green]")
        else:
            console.print(f"[red]Could not mark professor {professor_id} as replied[/red]")
    finally:
        session.close()


@app.command()
def followups():
    """Show professors due for follow-ups."""
    import datetime

    from outreach.followups import due_followups

    init_db()
    session = get_session()
    try:
        due = due_followups(session, datetime.datetime.utcnow())

        if not due:
            console.print("[yellow]No follow-ups due.[/yellow]")
            return

        table = Table(title="Follow-ups Due")
        table.add_column("Prof ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("FU #", style="yellow")
        table.add_column("Last Sent", style="magenta")

        for item in due:
            table.add_row(
                str(item["professor_id"]),
                item["professor_name"],
                str(item["followup_number"]),
                str(item["last_sent_at"]),
            )

        console.print(table)
    finally:
        session.close()


if __name__ == "__main__":
    app()
