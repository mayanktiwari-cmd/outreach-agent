"""Tests for Phase 3: Drafter with JSON extraction, grounding, and judge checks."""

from __future__ import annotations

import json

from outreach.drafter import (
    FakeLLMClient,
    extract_json,
    generate_draft,
    quote_appears_in_source,
    validate_draft_text,
)


class TestExtractJSON:
    """Test JSON extraction from various LLM outputs."""

    def test_clean_json(self):
        raw = '{"subject": "Hello", "body": "World"}'
        result = extract_json(raw)
        assert result == {"subject": "Hello", "body": "World"}

    def test_json_in_markdown_fences(self):
        raw = '''Here is the email:
```json
{"subject": "Hello", "body": "World"}
```'''
        result = extract_json(raw)
        assert result["subject"] == "Hello"

    def test_json_with_preamble(self):
        raw = '''Sure! Here's the email draft:

{"subject": "Research Opportunity", "body": "Dear Professor"}'''
        result = extract_json(raw)
        assert result["subject"] == "Research Opportunity"

    def test_json_with_trailing_text(self):
        raw = '{"subject": "Hello", "body": "World"}\n\nI hope this helps!'
        result = extract_json(raw)
        assert result["subject"] == "Hello"

    def test_no_json_returns_none(self):
        result = extract_json("This is just plain text with no JSON")
        assert result is None

    def test_malformed_json(self):
        result = extract_json('{subject: broken}')
        assert result is None

    def test_nested_json(self):
        raw = '{"subject": "Hello", "body": "World", "meta": {"key": "val"}}'
        result = extract_json(raw)
        assert result["subject"] == "Hello"


class TestQuoteMatching:
    def test_exact_match(self):
        source = "We present a novel approach to neural network pruning."
        quote = "novel approach to neural network pruning"
        assert quote_appears_in_source(quote, source) is True

    def test_whitespace_normalised(self):
        source = "We  present   a novel\n\napproach to pruning."
        quote = "novel approach to pruning"
        assert quote_appears_in_source(quote, source) is True

    def test_case_insensitive(self):
        source = "We Present A Novel Approach"
        quote = "we present a novel approach"
        assert quote_appears_in_source(quote, source) is True

    def test_no_match(self):
        source = "We study image classification"
        quote = "neural network pruning"
        assert quote_appears_in_source(quote, source) is False

    def test_empty_quote(self):
        assert quote_appears_in_source("", "some text") is False

    def test_empty_source(self):
        assert quote_appears_in_source("quote", "") is False


class TestValidateDraft:
    def test_valid_draft(self):
        body = "Dear Professor Smith, I read your paper on pruning. " * 2
        violations = validate_draft_text(body, {})
        assert len(violations) == 0

    def test_too_many_words(self):
        body = "word " * 200
        violations = validate_draft_text(body, {})
        assert any("170" in v for v in violations)

    def test_em_dash_rejected(self):
        body = "This is a test \u2014 with an em dash."
        violations = validate_draft_text(body, {})
        assert any("em dash" in v.lower() for v in violations)

    def test_en_dash_rejected(self):
        body = "This is a test \u2013 with an en dash."
        violations = validate_draft_text(body, {})
        assert any("en dash" in v.lower() for v in violations)

    def test_hyphen_as_punctuation(self):
        body = "This is - a test with hyphens as punctuation."
        violations = validate_draft_text(body, {})
        assert any("hyphen" in v.lower() for v in violations)

    def test_flattery_cliche(self):
        body = "I am deeply impressed by your groundbreaking work on AI."
        violations = validate_draft_text(body, {})
        assert any("cliché" in v or "cliche" in v.lower() for v in violations)


class TestGenerateDraft:
    """Test draft generation with fake LLM client."""

    def test_successful_draft(self, sample_profile):
        """Test a clean successful draft generation."""
        source_text = "We present a novel approach to neural network pruning that reduces model size."
        llm_response = json.dumps({
            "subject": "Research Internship Inquiry",
            "body": "Dear Professor Smith, I am a junior at MIT studying CS. I read your recent paper on neural network pruning. Your approach to reducing model size is interesting. I have experience with neural networks through my transformer project. Could we discuss potential research opportunities? Best, Jane",
            "referenced_claim": "The paper presents a novel approach to reducing model size",
            "claim_source_quote": "novel approach to neural network pruning that reduces model size",
        })

        llm = FakeLLMClient(responses=[
            llm_response,
            # Judge response
            json.dumps({"supported": True, "confidence": 0.85, "reasoning": "Claim matches quote"}),
        ])

        result = generate_draft(
            llm=llm,
            professor_name="Alice Smith",
            university="Example University",
            paper_title="Advances in Neural Network Pruning",
            paper_authors="Alice Smith, Bob Jones",
            paper_abstract=source_text,
            paper_introduction=None,
            profile=sample_profile,
            source_text=source_text,
        )

        assert result["subject"] == "Research Internship Inquiry"
        assert result["grounded"] is True
        assert result["needs_human"] is False
        assert result["error"] is None

    def test_hallucinated_quote_needs_human(self, sample_profile):
        """Test that a hallucinated quote eventually triggers needs_human."""
        source_text = "We study image classification using CNNs."

        # Both attempts have a quote that doesn't appear in source
        bad_response = json.dumps({
            "subject": "Research Inquiry",
            "body": "Dear Professor, interesting work.",
            "referenced_claim": "pruning networks",
            "claim_source_quote": "this quote does not exist in the source text at all",
        })

        llm = FakeLLMClient(responses=[bad_response, bad_response])

        result = generate_draft(
            llm=llm,
            professor_name="Alice Smith",
            university="Example University",
            paper_title="CNN Classification",
            paper_authors="Alice Smith",
            paper_abstract=source_text,
            paper_introduction=None,
            profile=sample_profile,
            source_text=source_text,
        )

        assert result["needs_human"] is True
        assert "not found in source" in (result["error"] or "").lower() or result["needs_human"]

    def test_judge_below_threshold(self, sample_profile):
        """Test that low judge score escalates."""
        source_text = "We present a novel approach to pruning."
        draft_response = json.dumps({
            "subject": "Research Inquiry",
            "body": "Dear Professor, interesting work on pruning.",
            "referenced_claim": "novel approach to pruning",
            "claim_source_quote": "novel approach to pruning",
        })

        # Judge returns low confidence twice
        low_judge = json.dumps({"supported": False, "confidence": 0.30, "reasoning": "Weak match"})

        llm = FakeLLMClient(responses=[draft_response, low_judge, low_judge])

        result = generate_draft(
            llm=llm,
            professor_name="Alice Smith",
            university="Example University",
            paper_title="Pruning Paper",
            paper_authors="Alice Smith",
            paper_abstract=source_text,
            paper_introduction=None,
            profile=sample_profile,
            source_text=source_text,
        )

        assert result["needs_human"] is True

    def test_json_extraction_failure_needs_human(self, sample_profile):
        """Test that unparseable LLM output leads to needs_human."""
        llm = FakeLLMClient(responses=[
            "This is not JSON at all, just rambling text.",
            "Still not JSON, sorry!",
        ])

        result = generate_draft(
            llm=llm,
            professor_name="Alice Smith",
            university="Example University",
            paper_title="Test Paper",
            paper_authors="Alice Smith",
            paper_abstract="Some abstract text.",
            paper_introduction=None,
            profile=sample_profile,
        )

        assert result["needs_human"] is True

    def test_markdown_fenced_json_works(self, sample_profile):
        """Test that JSON inside markdown fences is extracted."""
        source_text = "We study deep learning optimization."
        fenced = '''Here's the email:
```json
{
    "subject": "Research Inquiry",
    "body": "Dear Professor, your work on deep learning is interesting.",
    "referenced_claim": "deep learning optimization",
    "claim_source_quote": "deep learning optimization"
}
```'''
        judge = json.dumps({"supported": True, "confidence": 0.90, "reasoning": "Good match"})

        llm = FakeLLMClient(responses=[fenced, judge])

        result = generate_draft(
            llm=llm,
            professor_name="Test Prof",
            university="Test U",
            paper_title="DL Optimization",
            paper_authors="Test Prof",
            paper_abstract=source_text,
            paper_introduction=None,
            profile=sample_profile,
            source_text=source_text,
        )

        assert result["grounded"] is True
        assert result["subject"] == "Research Inquiry"
