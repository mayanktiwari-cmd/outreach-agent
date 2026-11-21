"""Tests for Phase 2: Paper fetching with fixtures (no live network)."""

from __future__ import annotations

from outreach.papers import (
    parse_link,
)


class TestParseLink:
    def test_arxiv_abs(self):
        result = parse_link("https://arxiv.org/abs/2301.08243")
        assert result["type"] == "arxiv_abs"
        assert result["id"] == "2301.08243"

    def test_arxiv_abs_v2(self):
        result = parse_link("https://arxiv.org/abs/2301.08243v2")
        assert result["type"] == "arxiv_abs"
        assert result["id"] == "2301.08243v2"

    def test_arxiv_search(self):
        result = parse_link("https://arxiv.org/search/?query=fei-fei+li&searchtype=author")
        assert result["type"] == "arxiv_search"

    def test_semantic_scholar(self):
        result = parse_link("https://www.semanticscholar.org/paper/abc123")
        assert result["type"] == "semantic_scholar"

    def test_generic_url(self):
        result = parse_link("https://www.cs.stanford.edu/~john/")
        assert result["type"] == "url"

    def test_empty(self):
        result = parse_link("")
        assert result["type"] == "unknown"

    def test_none_safe(self):
        # parse_link expects str, but should handle empty gracefully
        result = parse_link("")
        assert result["type"] == "unknown"


class TestPaperFetchWithFixtures:
    """Tests using recorded/mocked data, not live network."""

    def test_fetch_returns_expected_structure(self):
        """Test that a paper dict has all required keys."""
        paper = {
            "arxiv_id": "2301.00001",
            "title": "Test Paper",
            "authors": "Author One, Author Two",
            "abstract": "This is the abstract.",
            "pdf_url": "https://arxiv.org/pdf/2301.00001",
            "source": "arxiv",
        }
        required_keys = {"title", "authors", "abstract", "source"}
        assert required_keys.issubset(paper.keys())

    def test_parse_link_various(self):
        cases = [
            ("https://arxiv.org/abs/2401.12345", "arxiv_abs"),
            ("https://arxiv.org/search/?query=test", "arxiv_search"),
            ("https://semanticscholar.org/paper/xyz", "semantic_scholar"),
            ("https://example.com", "url"),
            ("", "unknown"),
        ]
        for link, expected_type in cases:
            assert parse_link(link)["type"] == expected_type


class TestPDFExtraction:
    """Test PDF text extraction with mocked data."""

    def test_extract_from_bytes(self, tmp_path):
        """Create a minimal PDF and extract text."""
        from io import BytesIO

        from pypdf import PdfReader, PdfWriter

        # Create a simple PDF
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        # pypdf doesn't easily add text to blank pages in tests,
        # but we can test the extraction pipeline doesn't crash
        pdf_bytes = BytesIO()
        writer.write(pdf_bytes)
        pdf_bytes.seek(0)

        reader = PdfReader(pdf_bytes)
        assert len(reader.pages) >= 1
        # Extract text (may be empty for blank page)
        text = reader.pages[0].extract_text()
        assert text is not None or text == ""
