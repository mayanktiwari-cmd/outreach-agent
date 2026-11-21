"""Database engine and session management."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from outreach.config import get_settings
from outreach.models import Base, SchemaVersion

CURRENT_SCHEMA_VERSION = 1

_engine = None
_SessionLocal = None


def get_engine(url: str | None = None):
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None or url is not None:
        db_url = url or get_settings().database_url
        _engine = create_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
        )
    return _engine


def get_session_factory(url: str | None = None) -> sessionmaker:
    """Get or create the session factory."""
    global _SessionLocal
    if _SessionLocal is None or url is not None:
        engine = get_engine(url)
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionLocal


def get_session(url: str | None = None) -> Session:
    """Create a new session."""
    factory = get_session_factory(url)
    return factory()


def init_db(url: str | None = None) -> None:
    """Create all tables and set schema version."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)

    session = get_session(url)
    try:
        version_row = session.query(SchemaVersion).first()
        if version_row is None:
            session.add(SchemaVersion(id=1, version=CURRENT_SCHEMA_VERSION))
            session.commit()
    finally:
        session.close()


def reset_engine():
    """Reset the engine and session factory (for testing)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
