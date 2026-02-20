"""
Configuration management using Pydantic Settings.

Loads configuration from environment variables and .env file.
"""

from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # TMDB API
    tmdb_api_key: str = Field(..., description="TMDB API key")

    # Gemini API
    gemini_api_key: str = Field(..., description="Google Gemini API key")
    gemini_paid_tier: bool = Field(False, description="Enable paid tier for Gemini")
    gemini_model: str = Field("gemini-2.0-flash", description="Gemini model to use")

    # Trakt API
    trakt_client_id: str = Field(..., description="Trakt OAuth client ID")
    trakt_client_secret: str = Field(..., description="Trakt OAuth client secret")
    trakt_redirect_uri: str = Field(..., description="Trakt OAuth redirect URI")

    # Master Password
    master_password: str = Field(..., description="Master password for addon access")

    # Database
    database_url: str = Field(..., description="PostgreSQL connection string")
    db_pool_size: int = Field(20, description="Database connection pool size")
    db_max_overflow: int = Field(10, description="Max overflow connections")

    # Application
    base_url: str = Field(..., description="Base URL where addon is hosted")
    addon_name: str = Field("Curatio", description="Addon name in Stremio")

    # Catalog Settings
    catalog_size: int = Field(200, description="Number of items per catalog")
    catalog_page_size: int = Field(100, description="Items returned per Stremio page")
    catalog_shuffle_hours: int = Field(
        3, description="Reshuffle catalog order every N hours (0 to disable)"
    )
    universal_catalog_refresh_hours: int = Field(
        24, description="Hours between universal catalog refreshes"
    )
    personalized_catalog_refresh_hours: int = Field(
        24, description="Hours between personalized catalog refreshes"
    )
    personalized_catalog_count: int = Field(
        14, description="Number of personalized catalogs per user"
    )
    universal_catalog_count: int = Field(40, description="Number of universal catalogs")

    # Logging
    log_level: str = Field("INFO", description="Logging level")
    debug: bool = Field(False, description="Enable debug mode")

    # Rate Limiting
    rate_limit_per_minute: int = Field(60, description="Requests per minute per user")

    # Security
    secret_key: str = Field(..., description="Secret key for JWT tokens")
    token_expiration_days: int = Field(30, description="JWT token expiration in days")

    # Feature Flags
    enable_personalized_catalogs: bool = Field(
        True, description="Enable personalized catalogs"
    )
    enable_universal_catalogs: bool = Field(
        True, description="Enable universal catalogs"
    )
    enable_trakt_sync: bool = Field(True, description="Enable Trakt sync")

    # Content Filters (global defaults — users can override per-account)
    hide_foreign: bool = Field(False, description="Hide non-English content by default")
    hide_adult: bool = Field(False, description="Hide explicit/18+ content by default")

    # Performance
    workers: int = Field(4, description="Number of Uvicorn workers")
    cache_ttl: int = Field(3600, description="Cache TTL in seconds")

    # Daily Update Scheduler
    daily_update_enabled: bool = Field(
        False, description="Enable daily content update scheduler"
    )
    daily_update_time: str = Field(
        "03:00", description="Time to run daily update in HH:MM format (UTC)"
    )

    # Development
    skip_api_validation: bool = Field(
        False, description="Skip API key validation (testing only)"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v):
        """Ensure base_url doesn't have trailing slash."""
        return v.rstrip("/")

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v):
        """Ensure secret key is long enough."""
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long")
        return v

    @field_validator("catalog_size")
    @classmethod
    def validate_catalog_size(cls, v):
        """Ensure catalog size is reasonable."""
        if v < 10 or v > 500:
            raise ValueError("catalog_size must be between 10 and 500")
        return v

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )


# Global settings instance
settings = Settings()  # type: ignore[call-arg]


# Validation functions
def validate_api_keys():
    """Validate that all required API keys are configured."""
    if settings.skip_api_validation:
        return True

    errors = []

    # Check TMDB API key format
    if not settings.tmdb_api_key or settings.tmdb_api_key == "your_tmdb_api_key_here":
        errors.append("TMDB_API_KEY is not configured")

    # Check Gemini API key format
    if (
        not settings.gemini_api_key
        or settings.gemini_api_key == "your_gemini_api_key_here"
    ):
        errors.append("GEMINI_API_KEY is not configured")

    # Check Trakt OAuth credentials
    if (
        not settings.trakt_client_id
        or settings.trakt_client_id == "your_trakt_client_id_here"
    ):
        errors.append("TRAKT_CLIENT_ID is not configured")

    if (
        not settings.trakt_client_secret
        or settings.trakt_client_secret == "your_trakt_client_secret_here"
    ):
        errors.append("TRAKT_CLIENT_SECRET is not configured")

    # Check master password
    if (
        not settings.master_password
        or settings.master_password == "change_this_to_a_strong_password_123"
    ):
        errors.append("MASTER_PASSWORD is not configured")

    if errors:
        raise ValueError(f"Configuration errors: {', '.join(errors)}")

    return True


def get_database_url() -> str:
    """Get database URL with proper formatting."""
    return settings.database_url


def get_stremio_manifest_url(user_key: Optional[str] = None) -> str:
    """Generate Stremio manifest URL."""
    if user_key:
        return f"{settings.base_url}/{user_key}/manifest.json"
    else:
        return f"{settings.base_url}/manifest.json"


def get_catalog_url(
    catalog_type: str, catalog_id: str, user_key: Optional[str] = None
) -> str:
    """Generate catalog URL for Stremio."""
    if user_key:
        return (
            f"{settings.base_url}/{user_key}/catalog/{catalog_type}/{catalog_id}.json"
        )
    else:
        return f"{settings.base_url}/catalog/{catalog_type}/{catalog_id}.json"
