"""
Database models for Curatio.

This module defines all database tables using SQLAlchemy ORM.
"""

from datetime import datetime
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class OAuthState(Base):
    """CSRF state tokens for OAuth flows."""

    __tablename__ = "oauth_states"

    state = Column(String(100), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # States expire after 10 minutes


class AdminSession(Base):
    """Persistent admin sessions (survives restarts)."""

    __tablename__ = "admin_sessions"

    token = Column(String(64), primary_key=True)
    expires_at = Column(DateTime, nullable=False)


class Tag(Base):
    """Master list of all semantic tags used for categorization."""

    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    # Categories: 'genre', 'mood', 'era', 'region', 'plot', 'style', 'character'

    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    movie_tags = relationship(
        "MovieTag", back_populates="tag", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Tag(id={self.id}, name='{self.name}', category='{self.category}')>"


class MovieTag(Base):
    """Junction table linking movies/shows to their semantic tags."""

    __tablename__ = "movie_tags"

    tmdb_id = Column(Integer, primary_key=True)
    tag_id = Column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    confidence = Column(Float, nullable=False)  # 0.0 to 1.0
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'
    tagged_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    tag = relationship("Tag", back_populates="movie_tags")

    # Indexes for fast queries
    __table_args__ = (
        Index("idx_tmdb_media", "tmdb_id", "media_type"),
        Index("idx_tag_confidence", "tag_id", "confidence"),
        Index("idx_media_confidence", "media_type", "confidence"),
        Index("idx_tagged_at", "tagged_at"),
    )

    def __repr__(self):
        return f"<MovieTag(tmdb_id={self.tmdb_id}, tag_id={self.tag_id}, confidence={self.confidence})>"


class UniversalCategory(Base):
    """Netflix-style universal categories available to all users."""

    __tablename__ = "universal_categories"

    id = Column(String(50), primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    tier = Column(Integer, nullable=False)  # 1-5 for organization
    sort_order = Column(Integer, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # Tag matching formula
    tag_formula = Column(JSON, nullable=False)
    # Example: {"required": ["Dark", "Crime"], "optional": ["Thriller"], "min_required": 2}

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    catalog_items = relationship(
        "UniversalCatalogContent",
        back_populates="category",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<UniversalCategory(id='{self.id}', name='{self.name}')>"


class UniversalCatalogContent(Base):
    """Pre-computed content for universal catalogs."""

    __tablename__ = "universal_catalog_content"

    category_id = Column(
        String(50),
        ForeignKey("universal_categories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tmdb_id = Column(Integer, primary_key=True)
    rank = Column(Integer, nullable=False)
    match_score = Column(Float, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    last_updated = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    category = relationship("UniversalCategory", back_populates="catalog_items")

    # Indexes
    __table_args__ = (
        Index("idx_category_rank", "category_id", "rank"),
        Index("idx_ucc_media_type", "media_type"),
    )

    def __repr__(self):
        return f"<UniversalCatalogContent(category_id='{self.category_id}', tmdb_id={self.tmdb_id}, rank={self.rank})>"


class User(Base):
    """Users who have authenticated with Trakt or registered locally."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_key = Column(String(100), unique=True, nullable=False, index=True)
    # user_key is a URL-safe token that goes in the Stremio manifest URL

    auth_source = Column(String(20), nullable=False, default="trakt")
    # 'trakt' or 'local'

    display_name = Column(String(200), nullable=True)

    # Account fields (for local auth with email/password/2FA)
    email = Column(String(254), unique=True, nullable=True, index=True)
    password_hash = Column(String(128), nullable=True)
    totp_secret = Column(Text, nullable=True)  # Fernet-encrypted
    totp_enabled = Column(Boolean, default=False, nullable=False)
    bandwidth_tier = Column(
        String(10), default="high", nullable=False
    )  # 'low' or 'high'

    trakt_user_id = Column(String(100), nullable=True, index=True)
    trakt_username = Column(String(100), nullable=True)
    trakt_access_token = Column(Text, nullable=True)  # encrypted at rest
    trakt_refresh_token = Column(Text, nullable=True)  # encrypted at rest
    trakt_token_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_sync = Column(DateTime, nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    personal_catalogs = relationship(
        "UserCatalog", back_populates="user", cascade="all, delete-orphan"
    )
    watch_events = relationship(
        "WatchEvent", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_active_sync", "is_active", "last_sync"),
        Index(
            "idx_trakt_user_id_unique",
            "trakt_user_id",
            unique=True,
            postgresql_where=text("trakt_user_id IS NOT NULL"),
        ),
    )

    def __repr__(self):
        return f"<User(id={self.id}, trakt_username='{self.trakt_username}')>"


class UserCatalog(Base):
    """Personalized catalogs for individual users."""

    __tablename__ = "user_catalogs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    slot_id = Column(String(50), nullable=False)
    # e.g., 'personalized-1', 'personalized-2', etc.

    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # How this catalog was generated
    generation_method = Column(String(50), nullable=False)
    # Methods: 'because_you_watched', 'top_picks', 'hidden_gems', etc.

    generation_params = Column(JSON, nullable=True)
    # Extra data used to generate this catalog

    last_generated = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    user = relationship("User", back_populates="personal_catalogs")
    catalog_items = relationship(
        "UserCatalogContent", back_populates="catalog", cascade="all, delete-orphan"
    )

    # Constraints
    __table_args__ = (
        UniqueConstraint("user_id", "slot_id", name="uq_user_slot"),
        Index("idx_user_active", "user_id", "is_active"),
    )

    def __repr__(self):
        return (
            f"<UserCatalog(id={self.id}, user_id={self.user_id}, name='{self.name}')>"
        )


class UserCatalogContent(Base):
    """Content for personalized user catalogs."""

    __tablename__ = "user_catalog_content"

    catalog_id = Column(
        Integer, ForeignKey("user_catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    tmdb_id = Column(Integer, primary_key=True)

    rank = Column(Integer, nullable=False)
    match_score = Column(Float, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    catalog = relationship("UserCatalog", back_populates="catalog_items")

    # Indexes
    __table_args__ = (Index("idx_catalog_rank", "catalog_id", "rank"),)

    def __repr__(self):
        return f"<UserCatalogContent(catalog_id={self.catalog_id}, tmdb_id={self.tmdb_id}, rank={self.rank})>"


class MediaMetadata(Base):
    """Cached metadata from TMDB to reduce API calls."""

    __tablename__ = "media_metadata"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)  # 'movie' or 'tv'

    title = Column(String(500), nullable=False)
    original_title = Column(String(500), nullable=True)
    overview = Column(Text, nullable=True)

    release_date = Column(String(20), nullable=True)  # YYYY-MM-DD
    genres = Column(JSON, nullable=True)  # List of genre names

    poster_path = Column(String(200), nullable=True)
    backdrop_path = Column(String(200), nullable=True)

    imdb_id = Column(String(20), nullable=True, index=True)
    logo_path = Column(String(200), nullable=True)

    original_language = Column(String(10), nullable=True)
    adult = Column(Boolean, default=False, nullable=True)

    vote_average = Column(Float, nullable=True)
    vote_count = Column(Integer, nullable=True)
    popularity = Column(Float, nullable=True)

    # For TV shows
    number_of_seasons = Column(Integer, nullable=True)
    number_of_episodes = Column(Integer, nullable=True)

    # Full TMDB response (for future use)
    raw_data = Column(JSON, nullable=True)

    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_media_type_popularity", "media_type", "popularity"),
        Index("idx_release_date", "release_date"),
        Index("idx_ratings", "media_type", "vote_average", "vote_count"),
        Index("idx_original_language", "original_language"),
    )

    def __repr__(self):
        return f"<MediaMetadata(tmdb_id={self.tmdb_id}, media_type='{self.media_type}', title='{self.title}')>"


class AdminSetting(Base):
    """Key-value store for admin-configurable settings that persist in the database."""

    __tablename__ = "admin_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AdminSetting(key='{self.key}')>"


class InviteCode(Base):
    """One-time invite codes for Trakt authentication (generated by admin)."""

    __tablename__ = "invite_codes"

    code = Column(String(64), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # None = never expires
    used_at = Column(DateTime, nullable=True)
    used_by = Column(String(100), nullable=True)  # trakt_username of who used it
    is_used = Column(Boolean, default=False, nullable=False)
    label = Column(String(200), nullable=True)  # optional admin note


class TaggingJob(Base):
    """Track tagging jobs for monitoring and debugging."""

    __tablename__ = "tagging_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(String(50), nullable=False)
    # Types: 'database_build', 'weekly_update', 'manual_retag'

    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    items_processed = Column(Integer, default=0, nullable=False)
    items_failed = Column(Integer, default=0, nullable=False)

    status = Column(String(20), default="running", nullable=False)
    # Statuses: 'running', 'completed', 'failed', 'cancelled'

    error_message = Column(Text, nullable=True)

    # Stats
    total_api_calls = Column(Integer, default=0, nullable=False)
    total_tokens_used = Column(Integer, default=0, nullable=False)
    estimated_cost = Column(Float, default=0.0, nullable=False)

    job_metadata = Column(
        JSON, nullable=True
    )  # Renamed from 'metadata' (reserved by SQLAlchemy)

    def __repr__(self):
        return f"<TaggingJob(id={self.id}, job_type='{self.job_type}', status='{self.status}')>"


class WatchEvent(Base):
    """Scrobble events from the custom Stremio client."""

    __tablename__ = "watch_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tmdb_id = Column(Integer, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # Episode tracking for TV
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)

    # Only 'complete' events count for recommendations
    action = Column(String(20), nullable=False)  # 'complete'

    title = Column(String(500), nullable=True)  # denormalized for display

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="watch_events")

    __table_args__ = (
        Index("idx_watch_user_created", "user_id", "created_at"),
        Index("idx_watch_user_tmdb", "user_id", "tmdb_id", "media_type"),
    )


class UserSession(Base):
    """Web login sessions for users with email/password accounts."""

    __tablename__ = "user_sessions"

    token = Column(String(64), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AppPairingSession(Base):
    """One-time pairing sessions for signing into the app via QR code or short code."""

    __tablename__ = "app_pairing_sessions"

    token = Column(String(64), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    short_code = Column(String(8), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # 5-minute expiry
    claimed = Column(Boolean, default=False, nullable=False)


class DevicePairingSession(Base):
    """Device-initiated pairing sessions (e.g. tvOS).

    Flow: device creates session (no auth) → displays code on screen →
    authenticated user claims code via web/phone → device polls until claimed.
    """

    __tablename__ = "device_pairing_sessions"

    device_token = Column(String(64), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    short_code = Column(String(8), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # 5-minute expiry
    claimed = Column(Boolean, default=False, nullable=False)
