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
    pool_recycle=1800,  # Recycle connections every 30 min (handles VPN/Gluetun reconnects)
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
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
)


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
    """Initialize database (create tables and seed categories)."""
    try:
        # Check connection
        if not check_database_connection():
            raise Exception("Cannot connect to database")

        # Create tables
        create_tables()

        # Add any missing columns to existing tables (lightweight migration)
        _add_missing_columns()

        # Seed universal categories so manifest is never empty
        from app.categories import seed_categories

        db = SessionLocal()
        try:
            seed_categories(db)
        finally:
            db.close()

        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def _add_missing_columns():
    """Add columns that exist in models but not yet in the database."""
    migrations = [
        ("media_metadata", "original_language", "VARCHAR(10)"),
        ("media_metadata", "adult", "BOOLEAN DEFAULT FALSE"),
        ("media_metadata", "overview", "TEXT"),
        ("media_metadata", "backdrop_path", "VARCHAR(200)"),
        ("media_metadata", "number_of_seasons", "INTEGER"),
        ("media_metadata", "number_of_episodes", "INTEGER"),
        ("media_metadata", "raw_data", "JSON"),
        ("media_metadata", "imdb_id", "VARCHAR(20)"),
        ("media_metadata", "logo_path", "VARCHAR(200)"),
    ]
    with get_db() as db:
        for table, column, col_type in migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                db.commit()
        # Ensure index on imdb_id
        try:
            db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_imdb_id ON media_metadata (imdb_id)"
                )
            )
            db.commit()
        except Exception:
            db.rollback()

        # --- User model migrations for scrobble support ---
        user_migrations = [
            ("users", "auth_source", "VARCHAR(20) DEFAULT 'trakt' NOT NULL"),
            ("users", "display_name", "VARCHAR(200)"),
        ]
        for table, column, col_type in user_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                db.commit()

        # Make trakt_user_id nullable (was NOT NULL)
        try:
            db.execute(
                text("ALTER TABLE users ALTER COLUMN trakt_user_id DROP NOT NULL")
            )
            db.commit()
        except Exception:
            db.rollback()

        # Make trakt tokens nullable
        for col in [
            "trakt_access_token",
            "trakt_refresh_token",
            "trakt_token_expires_at",
        ]:
            try:
                db.execute(text(f"ALTER TABLE users ALTER COLUMN {col} DROP NOT NULL"))
                db.commit()
            except Exception:
                db.rollback()

        # Drop the old unique constraint on trakt_user_id if it exists,
        # replaced by partial unique index in the model
        try:
            db.execute(
                text(
                    "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_trakt_user_id_key"
                )
            )
            db.commit()
        except Exception:
            db.rollback()

        # Create partial unique index for trakt_user_id (only non-NULL)
        try:
            db.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trakt_user_id_unique "
                    "ON users (trakt_user_id) WHERE trakt_user_id IS NOT NULL"
                )
            )
            db.commit()
        except Exception:
            db.rollback()

        # --- v2: Account system columns ---
        v2_user_migrations = [
            ("users", "email", "VARCHAR(254)"),
            ("users", "password_hash", "VARCHAR(128)"),
            ("users", "totp_secret", "TEXT"),
            ("users", "totp_enabled", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("users", "bandwidth_tier", "VARCHAR(10) DEFAULT 'high' NOT NULL"),
            ("users", "is_admin", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("admin_sessions", "user_id", "INTEGER"),
        ]
        for table, column, col_type in v2_user_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                db.commit()

        # Unique index on email (only non-NULL, for backward compat with Trakt users)
        try:
            db.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
                    "ON users (email) WHERE email IS NOT NULL"
                )
            )
            db.commit()
        except Exception:
            db.rollback()
