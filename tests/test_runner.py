"""Tests for Phase 5: Runner resumability and crash recovery."""

from __future__ import annotations

import json

import pytest

from outreach.drafter import FakeLLMClient
from outreach.models import (
    Draft,
    DraftKind,
    Paper,
    ProfessorStatus,
)
from outreach.runner import import_professors, run_batch
from outreach.statemachine import transition


@pytest.fixture
def csv_file(tmp_path):
    """Create a sample CSV file for import."""
    csv_content = """name,email,university,link
"Alice Smith",alice@example.edu,"Example University","https://arxiv.org/abs/2301.00001"
"Bob Jones",bob@example.edu,"Other University","https://arxiv.org/abs/2301.00002"
"Carol White",carol@example.edu,"Third University","https://arxiv.org/abs/2301.00003"
"""
    path = tmp_path / "professors.csv"
    path.write_text(csv_content)
    return str(path)


class TestImport:
    def test_import_professors(self, db_session, csv_file):
        professors = import_professors(db_session, csv_file)
        assert len(professors) == 3
        assert professors[0].name == "Alice Smith"
        assert professors[0].status == ProfessorStatus.NEW.value

    def test_import_skips_duplicates(self, db_session, csv_file):
        professors1 = import_professors(db_session, csv_file)
        professors2 = import_professors(db_session, csv_file)
        assert len(professors1) == 3
        assert len(professors2) == 0  # All duplicates

    def test_import_missing_file(self, db_session):
        with pytest.raises(FileNotFoundError):
            import_professors(db_session, "/nonexistent/path.csv")


class TestRunnerResumability:
    """Test that the runner resumes correctly after a crash."""

    def test_runner_skips_already_processed(self, db_session, csv_file, sample_profile, tmp_path):
        """Professors already past NEW should be skipped on re-run."""
        import yaml

        profile_path = tmp_path / "profile.yaml"
        with open(profile_path, "w") as f:
            yaml.dump(sample_profile, f)

        professors = import_professors(db_session, csv_file)

        # Manually advance first professor to PAPER_FETCHED
        paper = Paper(
            professor_id=professors[0].id,
            title="Test Paper",
            authors="Alice Smith",
            abstract="Test abstract about machine learning.",
            source="arxiv",
        )
        db_session.add(paper)
        transition(db_session, professors[0], ProfessorStatus.PAPER_FETCHED)
        db_session.commit()

        # Create LLM responses for drafting (only the first professor needs it)
        draft_resp = json.dumps({
            "subject": "Research Inquiry",
            "body": "Dear Professor, your work on machine learning is interesting.",
            "referenced_claim": "machine learning",
            "claim_source_quote": "machine learning",
        })
        judge_resp = json.dumps({
            "supported": True,
            "confidence": 0.85,
            "reasoning": "Match",
        })
        llm = FakeLLMClient(responses=[draft_resp, judge_resp])

        # Run batch - should draft for first prof, skip/fail paper fetch for others (no network)
        run_batch(
            db_session,
            llm=llm,
            profile_path=str(profile_path),
            skip_paper_fetch=True,  # Skip paper fetch to avoid network calls
        )

        # First professor should now be DRAFTED
        db_session.refresh(professors[0])
        assert professors[0].status == ProfessorStatus.DRAFTED.value

    def test_simulated_crash_mid_batch(self, db_session, csv_file, sample_profile, tmp_path):
        """Simulate a crash by injecting an exception, then verify resumability."""
        import yaml

        profile_path = tmp_path / "profile.yaml"
        with open(profile_path, "w") as f:
            yaml.dump(sample_profile, f)

        professors = import_professors(db_session, csv_file)

        # Advance all to PAPER_FETCHED with papers
        for prof in professors:
            paper = Paper(
                professor_id=prof.id,
                title=f"Paper by {prof.name}",
                authors=prof.name,
                abstract=f"Research on topic by {prof.name} about neural networks.",
                source="arxiv",
            )
            db_session.add(paper)
            transition(db_session, prof, ProfessorStatus.PAPER_FETCHED)
        db_session.commit()

        # LLM that fails on the second call (simulated crash)
        good_resp = json.dumps({
            "subject": "Research Inquiry",
            "body": "Dear Professor, your neural networks work is interesting.",
            "referenced_claim": "neural networks",
            "claim_source_quote": "neural networks",
        })
        judge_resp = json.dumps({
            "supported": True,
            "confidence": 0.85,
            "reasoning": "Match",
        })

        class CrashingLLM(FakeLLMClient):
            def generate(self, messages, response_format="json"):
                self.call_count += 1
                user_msg = messages[-1]["content"] if messages else ""
                # Fail specifically for Bob Jones
                if "Bob Jones" in user_msg:
                    raise RuntimeError("Simulated crash for Bob Jones!")
                if self.call_count % 2 == 1:
                    return good_resp
                return judge_resp

        llm = CrashingLLM()
        stats = run_batch(
            db_session,
            llm=llm,
            profile_path=str(profile_path),
            skip_paper_fetch=True,
        )
        assert stats["failed"] >= 1

        # First prof should be DRAFTED
        db_session.refresh(professors[0])
        assert professors[0].status == ProfessorStatus.DRAFTED.value

        # Second prof should be in a failure state (NEEDS_HUMAN or FAILED_DRAFT)
        db_session.refresh(professors[1])
        assert professors[1].status in (
            ProfessorStatus.NEEDS_HUMAN.value,
            ProfessorStatus.FAILED_DRAFT.value,
        )

        # Third prof (Carol) should have succeeded despite second prof's failure
        db_session.refresh(professors[2])
        assert professors[2].status == ProfessorStatus.DRAFTED.value

    def test_no_duplicate_operations_on_rerun(self, db_session, csv_file, sample_profile, tmp_path):
        """Re-running after completion should not repeat any step."""
        import yaml

        profile_path = tmp_path / "profile.yaml"
        with open(profile_path, "w") as f:
            yaml.dump(sample_profile, f)

        professors = import_professors(db_session, csv_file)

        # Advance all to DRAFTED (past the batch runner's scope)
        for prof in professors:
            paper = Paper(
                professor_id=prof.id,
                title=f"Paper by {prof.name}",
                authors=prof.name,
                abstract="Some abstract.",
                source="arxiv",
            )
            db_session.add(paper)
            transition(db_session, prof, ProfessorStatus.PAPER_FETCHED)
            draft = Draft(
                professor_id=prof.id,
                kind=DraftKind.INITIAL.value,
                sequence=1,
                subject="Test Subject",
                body="Test body.",
                text_hash=Draft.compute_hash("Test Subject", "Test body."),
                is_active=1,
            )
            db_session.add(draft)
            transition(db_session, prof, ProfessorStatus.DRAFTED)
        db_session.commit()

        llm = FakeLLMClient()
        stats = run_batch(
            db_session,
            llm=llm,
            profile_path=str(profile_path),
            skip_paper_fetch=True,
        )

        # All should be skipped
        assert stats["skipped"] == 3
        assert stats["papers_fetched"] == 0
        assert stats["drafts_created"] == 0
        assert stats["failed"] == 0
        # LLM should not have been called
        assert llm.call_count == 0
