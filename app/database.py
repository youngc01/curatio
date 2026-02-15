"""
Database connection and session management.

Provides database engine, session factory, and helper functions.
"""

from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import Pool
from loguru import logger

from app.config import settings
from app.models import Base

# Create database engine
engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,  # Verify connections before using
    echo=settings.debug,  # Log SQL queries in debug mode
)


# Listen for connection events (useful for debugging)
@event.listens_for(Pool, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Set SQLite pragmas if using SQLite (for testing)."""
    if "sqlite" in settings.database_url:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    """Create all database tables."""
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully")


def drop_tables():
    """Drop all database tables (use with caution!)."""
    logger.warning("Dropping all database tables...")
    Base.metadata.drop_all(bind=engine)
    logger.warning("All database tables dropped")


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    Get database session as context manager.

    Usage:
        with get_db() as db:
            user = db.query(User).first()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        db.close()


def get_db_session() -> Session:
    """
    Get database session (must be closed manually).

    Usage:
        db = get_db_session()
        try:
            user = db.query(User).first()
            db.commit()
        finally:
            db.close()
    """
    return SessionLocal()


# Dependency for FastAPI
def get_db_dependency():
    """
    FastAPI dependency for database session.

    Usage:
        @app.get("/users")
        def get_users(db: Session = Depends(get_db_dependency)):
            return db.query(User).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database_connection() -> bool:
    """Check if database is accessible."""
    try:
        with get_db() as db:
            # Execute simple query
            db.execute(text("SELECT 1"))
        logger.info("Database connection successful")
        return True
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return False


def init_database():
    """Initialize database (create tables if they don't exist)."""
    try:
        # Check connection
        if not check_database_connection():
            raise Exception("Cannot connect to database")

        # Create tables
        create_tables()

        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
