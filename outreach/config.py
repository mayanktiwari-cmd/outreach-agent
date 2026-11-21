"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration loaded from env vars / .env file."""

    # LLM
    groq_api_key: str = Field(default="", description="Groq API key")
    llm_model: str = Field(default="llama-3.3-70b-versatile", description="LLM model name")

    # Database
    database_url: str = Field(default="sqlite:///./outreach.db")

    # Email sending
    send_mode: str = Field(default="dry_run", description="dry_run or smtp")
    sender_email: str = Field(default="you@university.edu")
    sender_name: str = Field(default="Your Name")

    # SMTP (only when send_mode=smtp)
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")

    # IMAP (optional reply tracking)
    imap_host: str = Field(default="")
    imap_user: str = Field(default="")
    imap_password: str = Field(default="")

    # Follow-up settings
    followup_interval_days: int = Field(default=7)
    max_followups: int = Field(default=2)

    # Grounding check
    judge_confidence_threshold: float = Field(default=0.60)
    judge_retry_threshold: float = Field(default=0.40)

    # Paths
    profile_path: str = Field(default="profile.yaml")
    outbox_dir: str = Field(default="outbox")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def is_smtp(self) -> bool:
        return self.send_mode.lower() == "smtp"

    @property
    def is_dry_run(self) -> bool:
        return self.send_mode.lower() != "smtp"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton settings."""
    return Settings()
