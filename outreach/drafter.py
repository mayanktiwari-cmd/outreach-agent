"""LLM drafting with JSON extraction, deterministic quote check, and judge check."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from outreach.config import get_settings
from outreach.retry import PermanentError, retry_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

def load_profile(path: str | None = None) -> dict:
    """Load user profile from YAML file."""
    if path is None:
        path = get_settings().profile_path
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Profile file not found: {path}")
    with open(p) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# JSON extraction strategies
# ---------------------------------------------------------------------------

def extract_json(text: str) -> Optional[dict]:
    """Try multiple strategies to extract JSON from LLM output.

    Strategies in order:
    1. Direct JSON parse
    2. Strip markdown code fences
    3. Regex for JSON object
    4. Find first { ... last }
    """
    # Strategy 1: Direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: Strip markdown fences
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Strategy 3: Regex for JSON object with known keys
    json_match = re.search(
        r'\{[^{}]*"subject"[^{}]*"body"[^{}]*\}',
        text,
        re.DOTALL,
    )
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 4: Find first { to last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Whitespace normalisation for quote matching
# ---------------------------------------------------------------------------

def normalise_whitespace(text: str) -> str:
    """Normalise whitespace for substring matching."""
    return " ".join(text.split()).strip().lower()


def quote_appears_in_source(quote: str, source_text: str) -> bool:
    """Check if the quote appears (whitespace-normalised) in the source text."""
    if not quote or not source_text:
        return False
    return normalise_whitespace(quote) in normalise_whitespace(source_text)


# ---------------------------------------------------------------------------
# Draft validation rules
# ---------------------------------------------------------------------------

CLICHE_PATTERNS = [
    r"(?i)\bi\s+am\s+(?:deeply|greatly|truly)\s+(?:impressed|fascinated|inspired)\b",
    r"(?i)\byour\s+(?:groundbreaking|pioneering|seminal|remarkable)\s+(?:work|research)\b",
    r"(?i)\bi\s+would\s+be\s+(?:honored|thrilled|delighted)\b",
    r"(?i)\byour\s+(?:esteemed|prestigious)\b",
]

EM_DASH = "\u2014"
EN_DASH = "\u2013"


def validate_draft_text(body: str, profile: dict) -> list[str]:
    """Validate draft text against hard rules. Returns list of violations."""
    violations = []

    # Word count
    word_count = len(body.split())
    if word_count > 170:
        violations.append(f"Draft is {word_count} words, max is 170")

    # Em dashes
    if EM_DASH in body:
        violations.append("Draft contains em dashes")

    # Hyphens as punctuation (not in compound words)
    # Look for " - " pattern (hyphen used as dash)
    if re.search(r"\s-\s", body):
        violations.append("Draft uses hyphens as punctuation (use commas or periods instead)")

    # En dashes
    if EN_DASH in body:
        violations.append("Draft contains en dashes")

    # Flattery clichés
    for pattern in CLICHE_PATTERNS:
        if re.search(pattern, body):
            violations.append(f"Draft contains flattery cliché matching: {pattern}")
            break

    return violations


# ---------------------------------------------------------------------------
# LLM client interface
# ---------------------------------------------------------------------------

class LLMClient:
    """Interface for LLM calls. Can be replaced for testing."""

    def generate(self, messages: list[dict], response_format: str = "json") -> str:
        """Generate a response from the LLM. Returns raw text."""
        raise NotImplementedError


class GroqLLMClient(LLMClient):
    """Real Groq LLM client."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from groq import Groq

        settings = get_settings()
        self.client = Groq(api_key=api_key or settings.groq_api_key)
        self.model = model or settings.llm_model

    def generate(self, messages: list[dict], response_format: str = "json") -> str:
        kwargs = {}
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
            **kwargs,
        )
        return response.choices[0].message.content


class FakeLLMClient(LLMClient):
    """Fake LLM client for testing."""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses) if responses else []
        self.call_count = 0
        self.last_messages = None

    def generate(self, messages: list[dict], response_format: str = "json") -> str:
        self.last_messages = messages
        self.call_count += 1
        if self.responses:
            return self.responses.pop(0)
        return '{"subject": "test", "body": "test", "referenced_claim": "test", "claim_source_quote": "test"}'


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

DRAFT_SYSTEM_PROMPT = """You are helping a student write a concise, specific email to a professor about a research internship opportunity.

RULES (follow exactly):
- Output ONLY valid JSON with keys: subject, body, referenced_claim, claim_source_quote
- referenced_claim: a specific claim from the paper that you reference in the email
- claim_source_quote: the EXACT quote from the provided paper text that supports your claim (copy it verbatim)
- The email body must be under 170 words
- Include ONE clear ask (e.g., "Could we discuss potential research opportunities?")
- Do NOT use em dashes (—), en dashes (–), or hyphens as punctuation (–, -)
- Do NOT use flattery clichés like "groundbreaking", "seminal", "deeply impressed", "honored"
- Do NOT invent facts about the student. Use ONLY the information provided in their profile
- Do NOT invent facts about the professor or paper. Reference ONLY what is in the provided paper
- Be specific about which aspect of the paper connects to the student's experience
- Keep the tone professional but natural, like a real student would write"""

STRICT_RETRY_PROMPT = """Your previous attempt failed the grounding check. The claim_source_quote MUST be an EXACT substring of the paper text provided. Copy a quote character-by-character from the paper text below. Do not paraphrase or modify it in any way.

Additionally:
- Output ONLY valid JSON, no preamble or explanation
- The quote must appear verbatim in the source text"""

JUDGE_SYSTEM_PROMPT = """You are a grounding judge. Given a claim made in an email and a source quote from a paper, determine if the claim is actually supported by the quote.

Output ONLY valid JSON: {"supported": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}

Be strict: the claim must be directly supported by the quote, not merely tangentially related."""


def build_draft_prompt(
    professor_name: str,
    university: str,
    paper_title: str,
    paper_authors: str,
    paper_abstract: str,
    paper_introduction: str | None,
    profile: dict,
) -> list[dict]:
    """Build the LLM messages for draft generation."""
    paper_text = f"Title: {paper_title}\nAuthors: {paper_authors}\nAbstract: {paper_abstract}"
    if paper_introduction:
        paper_text += f"\n\nIntroduction:\n{paper_introduction[:3000]}"

    profile_text = f"""Student Profile:
Name: {profile['name']}
Year: {profile['year']}
College: {profile['college']}
Projects:
{chr(10).join('- ' + p for p in profile.get('projects', []))}
GitHub: {profile.get('github', 'N/A')}
LinkedIn: {profile.get('linkedin', 'N/A')}"""

    user_msg = f"""Write an email from {profile['name']} to Professor {professor_name} at {university}.

PAPER (reference something specific from this):
{paper_text}

{profile_text}

Remember: output ONLY valid JSON with keys: subject, body, referenced_claim, claim_source_quote
The claim_source_quote must be an EXACT substring of the paper text above."""

    return [
        {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def build_judge_prompt(claim: str, quote: str) -> list[dict]:
    """Build messages for the judge LLM call."""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Claim made in email: {claim}\n\nSource quote from paper: {quote}\n\nIs this claim supported by the quote?",
        },
    ]


def build_followup_prompt(
    professor_name: str,
    university: str,
    original_subject: str,
    original_body: str,
    paper_title: str,
    profile: dict,
    followup_number: int,
) -> list[dict]:
    """Build messages for follow-up email draft."""
    system = """You are helping a student write a brief follow-up email to a professor.

RULES:
- Output ONLY valid JSON with keys: subject, body, referenced_claim, claim_source_quote
- For follow-ups, referenced_claim and claim_source_quote can reference the original email's content
- Keep it very short (under 100 words for the body)
- Reference the previous email naturally
- Do NOT contradict anything in the previous email
- Do NOT use em dashes, en dashes, or hyphens as punctuation
- Do NOT use flattery clichés
- One clear, polite ask"""

    user_msg = f"""Write follow-up #{followup_number} from {profile['name']} to Professor {professor_name} at {university}.

PREVIOUS EMAIL:
Subject: {original_subject}
Body: {original_body}

The paper discussed was: {paper_title}

Keep it brief and professional. Output ONLY valid JSON."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]


def generate_draft(
    llm: LLMClient,
    professor_name: str,
    university: str,
    paper_title: str,
    paper_authors: str,
    paper_abstract: str,
    paper_introduction: str | None,
    profile: dict,
    source_text: str | None = None,
    professor_id: int | None = None,
    session=None,
    step_attempt_cls=None,
) -> dict:
    """Generate a draft email with grounding checks.

    Returns dict with keys: subject, body, referenced_claim, claim_source_quote,
    judge_score, grounded, violations, needs_human, error.
    """
    record_fn = None
    if session and step_attempt_cls:
        from outreach.papers import _record_attempt_factory
        record_fn = _record_attempt_factory(session, step_attempt_cls)

    # The source text for quote verification
    full_source = paper_abstract or ""
    if paper_introduction:
        full_source += " " + paper_introduction
    if source_text:
        full_source = source_text

    result = {
        "subject": "",
        "body": "",
        "referenced_claim": "",
        "claim_source_quote": "",
        "judge_score": None,
        "grounded": False,
        "violations": [],
        "needs_human": False,
        "error": None,
    }

    # Attempt 1: Normal prompt
    messages = build_draft_prompt(
        professor_name, university, paper_title, paper_authors,
        paper_abstract, paper_introduction, profile,
    )

    for attempt_num, is_strict in [(1, False), (2, True)]:
        try:
            if is_strict:
                messages.append({"role": "user", "content": STRICT_RETRY_PROMPT + f"\n\nPaper text:\n{full_source[:3000]}"})

            raw = retry_sync(
                llm.generate,
                messages,
                max_attempts=2,
                base_delay=1.0,
                timeout=30.0,
                step_name=f"draft_llm_attempt_{attempt_num}",
                professor_id=professor_id,
                record_attempt=record_fn,
            )

            parsed = extract_json(raw)
            if parsed is None:
                if attempt_num == 2:
                    result["error"] = "Failed to extract JSON from LLM output after 2 attempts"
                    result["needs_human"] = True
                    return result
                continue

            # Validate required keys
            required = {"subject", "body", "referenced_claim", "claim_source_quote"}
            missing = required - set(parsed.keys())
            if missing:
                if attempt_num == 2:
                    result["error"] = f"Missing keys in LLM output: {missing}"
                    result["needs_human"] = True
                    return result
                continue

            result["subject"] = parsed["subject"]
            result["body"] = parsed["body"]
            result["referenced_claim"] = parsed["referenced_claim"]
            result["claim_source_quote"] = parsed["claim_source_quote"]

            # Validate draft text rules
            violations = validate_draft_text(parsed["body"], profile)
            result["violations"] = violations

            # Deterministic quote check
            if not quote_appears_in_source(parsed["claim_source_quote"], full_source):
                logger.warning(f"Quote not found in source (attempt {attempt_num})")
                if attempt_num == 2:
                    result["error"] = "Quote not found in source text after 2 attempts"
                    result["needs_human"] = True
                    return result
                continue

            result["grounded"] = True
            break

        except PermanentError as e:
            result["error"] = f"Permanent LLM error: {e}"
            result["needs_human"] = True
            return result
        except Exception as e:
            if attempt_num == 2:
                result["error"] = f"Draft generation failed: {e}"
                result["needs_human"] = True
                return result
            logger.warning(f"Draft attempt {attempt_num} failed: {e}")

    if not result["grounded"]:
        result["needs_human"] = True
        return result

    # Judge check
    try:
        judge_result = run_judge_check(
            llm,
            result["referenced_claim"],
            result["claim_source_quote"],
            professor_id=professor_id,
            record_fn=record_fn,
        )
        result["judge_score"] = judge_result.get("confidence", 0.0)

        settings = get_settings()
        threshold = settings.judge_confidence_threshold
        retry_threshold = settings.judge_retry_threshold

        if result["judge_score"] < retry_threshold:
            result["needs_human"] = True
            result["error"] = f"Judge score {result['judge_score']:.2f} below retry threshold {retry_threshold}"
        elif result["judge_score"] < threshold:
            # One retry at the lower threshold
            logger.warning(f"Judge score {result['judge_score']:.2f} below threshold, retrying")
            judge_result2 = run_judge_check(
                llm,
                result["referenced_claim"],
                result["claim_source_quote"],
                professor_id=professor_id,
                record_fn=record_fn,
            )
            result["judge_score"] = judge_result2.get("confidence", 0.0)
            if result["judge_score"] < retry_threshold:
                result["needs_human"] = True
                result["error"] = f"Judge score {result['judge_score']:.2f} still below threshold after retry"

    except Exception as e:
        logger.warning(f"Judge check failed: {e}")
        # Judge failure is not fatal, but log it
        result["judge_score"] = None

    return result


def run_judge_check(
    llm: LLMClient,
    claim: str,
    quote: str,
    professor_id: int | None = None,
    record_fn=None,
) -> dict:
    """Run the judge LLM call to verify claim is supported by quote."""
    messages = build_judge_prompt(claim, quote)
    raw = retry_sync(
        llm.generate,
        messages,
        max_attempts=2,
        base_delay=1.0,
        timeout=30.0,
        step_name="judge_check",
        professor_id=professor_id,
        record_attempt=record_fn,
    )
    parsed = extract_json(raw)
    if parsed is None:
        return {"supported": False, "confidence": 0.0, "reasoning": "Failed to parse judge output"}
    return parsed


def generate_followup_draft(
    llm: LLMClient,
    professor_name: str,
    university: str,
    original_subject: str,
    original_body: str,
    paper_title: str,
    profile: dict,
    followup_number: int,
) -> dict:
    """Generate a follow-up draft email."""
    messages = build_followup_prompt(
        professor_name, university, original_subject, original_body,
        paper_title, profile, followup_number,
    )

    result = {
        "subject": "",
        "body": "",
        "referenced_claim": "",
        "claim_source_quote": "",
        "judge_score": None,
        "grounded": True,  # Follow-ups reference stored content, not papers
        "violations": [],
        "needs_human": False,
        "error": None,
    }

    try:
        raw = llm.generate(messages)
        parsed = extract_json(raw)
        if parsed is None:
            result["error"] = "Failed to extract JSON from follow-up LLM output"
            result["needs_human"] = True
            return result

        result["subject"] = parsed.get("subject", f"Re: {original_subject}")
        result["body"] = parsed.get("body", "")
        result["referenced_claim"] = parsed.get("referenced_claim", "")
        result["claim_source_quote"] = parsed.get("claim_source_quote", "")

        violations = validate_draft_text(result["body"], profile)
        result["violations"] = violations

    except Exception as e:
        result["error"] = f"Follow-up draft generation failed: {e}"
        result["needs_human"] = True

    return result
