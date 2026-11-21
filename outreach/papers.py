"""Paper fetching: arXiv API first, Semantic Scholar as fallback, PDF text extraction."""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Optional

import httpx
from pypdf import PdfReader

from outreach.retry import PermanentError, TransientError, retry_sync

logger = logging.getLogger(__name__)

# Max pages to read from PDF for introduction
MAX_PDF_PAGES = 3
# Timeout for HTTP calls
HTTP_TIMEOUT = 30.0


def _record_attempt_factory(session, StepAttempt):
    """Create a record_attempt callback that writes to the step_attempts table."""

    def record_attempt(professor_id, step, attempt, ok, error, duration_ms):
        if session is not None and professor_id is not None:
            sa = StepAttempt(
                professor_id=professor_id,
                step=step,
                attempt=attempt,
                ok=1 if ok else 0,
                error=error,
                duration_ms=duration_ms,
            )
            session.add(sa)
            session.flush()

    return record_attempt


def parse_link(link: str) -> dict:
    """Parse a professor's link to determine the type and extract info.

    Returns dict with keys: type ("arxiv_abs", "arxiv_search", "semantic_scholar", "url"), id/query.
    """
    if not link:
        return {"type": "unknown"}

    # Direct arXiv abstract link: https://arxiv.org/abs/XXXX.XXXXX
    match = re.match(r"https?://arxiv\.org/abs/([^\s?#]+)", link)
    if match:
        return {"type": "arxiv_abs", "id": match.group(1)}

    # arXiv search/author query
    if "arxiv.org/search" in link or "arxiv.org/find" in link:
        return {"type": "arxiv_search", "url": link}

    # Semantic Scholar paper
    match = re.match(r"https?://(?:www\.)?semanticscholar\.org/paper/([^\s?#]+)", link)
    if match:
        return {"type": "semantic_scholar", "id": match.group(1)}

    # Generic URL (could be homepage, etc.)
    return {"type": "url", "url": link}


def fetch_arxiv_by_id(arxiv_id: str) -> Optional[dict]:
    """Fetch a paper from arXiv by ID using the arxiv Python package."""
    try:
        import arxiv

        client = arxiv.Client()
        search = arxiv.Search(id_list=[arxiv_id])
        results = list(client.results(search))
        if not results:
            return None
        paper = results[0]
        return {
            "arxiv_id": paper.entry_id.split("/abs/")[-1] if "/abs/" in paper.entry_id else arxiv_id,
            "title": paper.title,
            "authors": ", ".join(a.name for a in paper.authors),
            "abstract": paper.summary,
            "pdf_url": paper.pdf_url,
            "source": "arxiv",
        }
    except Exception as e:
        logger.warning(f"arXiv fetch by ID failed for {arxiv_id}: {e}")
        return None


def fetch_arxiv_by_author(author_name: str) -> Optional[dict]:
    """Fetch the most recent paper by an author from arXiv."""
    try:
        import arxiv

        client = arxiv.Client()
        search = arxiv.Search(
            query=f"au:{author_name}",
            max_results=1,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        results = list(client.results(search))
        if not results:
            return None
        paper = results[0]
        return {
            "arxiv_id": paper.entry_id.split("/abs/")[-1] if "/abs/" in paper.entry_id else "",
            "title": paper.title,
            "authors": ", ".join(a.name for a in paper.authors),
            "abstract": paper.summary,
            "pdf_url": paper.pdf_url,
            "source": "arxiv",
        }
    except Exception as e:
        logger.warning(f"arXiv author search failed for {author_name}: {e}")
        return None


def fetch_semantic_scholar(query: str) -> Optional[dict]:
    """Fetch a recent paper from Semantic Scholar by author name."""
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": 1,
            "fields": "title,authors,abstract,externalIds,url,openAccessPdf",
        }
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 429:
                raise TransientError("Semantic Scholar rate limit")
            if resp.status_code >= 500:
                raise TransientError(f"Semantic Scholar server error: {resp.status_code}")
            if resp.status_code >= 400:
                raise PermanentError(f"Semantic Scholar client error: {resp.status_code}")

            data = resp.json()
            papers = data.get("data", [])
            if not papers:
                return None
            paper = papers[0]
            arxiv_id = None
            ext_ids = paper.get("externalIds", {})
            if ext_ids:
                arxiv_id = ext_ids.get("ArXiv")

            pdf_url = None
            oap = paper.get("openAccessPdf")
            if oap:
                pdf_url = oap.get("url")

            authors = ", ".join(a.get("name", "") for a in paper.get("authors", []))

            return {
                "arxiv_id": arxiv_id,
                "semantic_scholar_id": paper.get("paperId"),
                "title": paper.get("title", ""),
                "authors": authors,
                "abstract": paper.get("abstract", ""),
                "pdf_url": pdf_url,
                "source": "semantic_scholar",
            }
    except (TransientError, PermanentError):
        raise
    except Exception as e:
        logger.warning(f"Semantic Scholar search failed for {query}: {e}")
        return None


def extract_pdf_text(pdf_url: str, max_pages: int = MAX_PDF_PAGES) -> Optional[str]:
    """Download a PDF and extract text from the first few pages."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(pdf_url)
            if resp.status_code != 200:
                logger.warning(f"PDF download failed with status {resp.status_code}")
                return None

            reader = PdfReader(BytesIO(resp.content))
            text_parts = []
            for i, page in enumerate(reader.pages):
                if i >= max_pages:
                    break
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

            if text_parts:
                return "\n".join(text_parts)
            return None
    except Exception as e:
        logger.warning(f"PDF text extraction failed: {e}")
        return None


def fetch_paper_for_professor(
    name: str,
    link: str,
    professor_id: int | None = None,
    session=None,
    step_attempt_cls=None,
) -> Optional[dict]:
    """Main entry point: fetch one paper for a professor.

    Tries arXiv first, then Semantic Scholar as fallback.
    Returns a dict with paper info or None.
    """
    record_fn = None
    if session and step_attempt_cls:
        record_fn = _record_attempt_factory(session, step_attempt_cls)

    parsed = parse_link(link)

    # Strategy 1: Direct arXiv ID
    if parsed["type"] == "arxiv_abs":

        def _fetch_arxiv_id():
            result = fetch_arxiv_by_id(parsed["id"])
            if result is None:
                raise TransientError(f"arXiv returned no results for {parsed['id']}")
            return result

        try:
            return retry_sync(
                _fetch_arxiv_id,
                max_attempts=3,
                base_delay=1.0,
                timeout=30.0,
                step_name="fetch_paper_arxiv_id",
                professor_id=professor_id,
                record_attempt=record_fn,
            )
        except Exception as e:
            logger.warning(f"arXiv ID fetch failed, falling back: {e}")

    # Strategy 2: arXiv author search
    if parsed["type"] in ("arxiv_search", "url", "unknown"):

        def _fetch_arxiv_author():
            result = fetch_arxiv_by_author(name)
            if result is None:
                raise TransientError(f"arXiv author search returned no results for {name}")
            return result

        try:
            return retry_sync(
                _fetch_arxiv_author,
                max_attempts=3,
                base_delay=1.0,
                timeout=30.0,
                step_name="fetch_paper_arxiv_author",
                professor_id=professor_id,
                record_attempt=record_fn,
            )
        except Exception:
            logger.warning(f"arXiv author search failed for {name}, trying Semantic Scholar")

    # Strategy 3: Semantic Scholar fallback
    def _fetch_ss():
        result = fetch_semantic_scholar(name)
        if result is None:
            raise TransientError(f"Semantic Scholar returned no results for {name}")
        return result

    try:
        return retry_sync(
            _fetch_ss,
            max_attempts=3,
            base_delay=1.0,
            timeout=30.0,
            step_name="fetch_paper_semantic_scholar",
            professor_id=professor_id,
            record_attempt=record_fn,
        )
    except Exception as e:
        logger.error(f"All paper fetch strategies failed for {name}: {e}")
        return None
