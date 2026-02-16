"""
Main FastAPI application for Curatio.

Provides Stremio manifest and catalog endpoints.
"""

import asyncio
import hashlib
import random
from time import time

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import secrets
from loguru import logger

from app.config import settings, validate_api_keys
from app.database import get_db_dependency, get_db, init_database, check_database_connection
from app.models import User, UniversalCategory, UserCatalog, OAuthState
from app.catalog_generator import CatalogGenerator
from app.trakt_client import trakt_client
from app.crypto import encrypt_token, decrypt_token
from app.landing import landing_page_html, auth_success_html, auth_error_html
from app.admin import router as admin_router, load_settings_from_db


async def ensure_valid_trakt_token(user: User, db: Session) -> str:
    """Return a valid (decrypted) Trakt access token, refreshing if expired."""
    if datetime.utcnow() < user.trakt_token_expires_at - timedelta(minutes=5):
        return decrypt_token(user.trakt_access_token)

    # Token expired or about to -- refresh it
    try:
        refresh = decrypt_token(user.trakt_refresh_token)
        token_data = await trakt_client.refresh_access_token(refresh)

        user.trakt_access_token = encrypt_token(token_data["access_token"])
        user.trakt_refresh_token = encrypt_token(token_data["refresh_token"])
        user.trakt_token_expires_at = datetime.utcnow() + timedelta(
            seconds=token_data["expires_in"]
        )
        db.commit()
        logger.info(f"Refreshed Trakt token for user {user.trakt_username}")
        return token_data["access_token"]
    except Exception as e:
        logger.error(f"Token refresh failed for user {user.trakt_username}: {e}")
        # Fall back to existing token (may still work if clock skew)
        return decrypt_token(user.trakt_access_token)


def _stremio_type(media_type: str) -> str:
    """Map internal media types to Stremio-compatible types.

    TMDB uses 'tv' but Stremio expects 'series'.
    """
    return "series" if media_type == "tv" else media_type


# Initialize FastAPI app
app = FastAPI(
    title="Curatio",
    description="AI-curated cinema for Stremio",
    version="1.0.0",
)

# CORS middleware (required for Stremio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount admin portal
app.include_router(admin_router)


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    logger.info("Starting Curatio...")

    # Validate configuration
    try:
        validate_api_keys()
        logger.info("Configuration validated")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        if not settings.skip_api_validation:
            raise

    # Initialize database
    try:
        init_database()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

    # Load admin settings from database (overrides env vars)
    load_settings_from_db()

    # Auto-resume interrupted builds (container restart recovery)
    from app.admin import auto_resume_build

    await auto_resume_build()

    # Start daily update scheduler if enabled
    if settings.daily_update_enabled:
        from app.scheduler import run_scheduler

        app.state.scheduler_task = asyncio.create_task(run_scheduler())
        logger.info("Daily update scheduler enabled")
    else:
        app.state.scheduler_task = None
        logger.info("Daily update scheduler disabled (set DAILY_UPDATE_ENABLED=true)")

    logger.info(f"Addon ready at {settings.base_url}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Curatio...")

    # Cancel scheduler if running
    if getattr(app.state, "scheduler_task", None) is not None:
        app.state.scheduler_task.cancel()
        try:
            await app.state.scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Scheduler stopped")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Landing page with install options and Trakt connect."""
    return HTMLResponse(content=landing_page_html())


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    db_ok = check_database_connection()

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "version": "1.0.0",
    }


@app.get("/manifest.json")
async def universal_manifest(db: Session = Depends(get_db_dependency)):
    """
    Stremio manifest for anonymous users (universal catalogs only).

    This is installed when users don't sign in with Trakt.
    Stremio derives the base URL by stripping the last path segment,
    so /manifest.json -> base URL is / -> catalogs at /catalog/{type}/{id}.json
    """
    # Get active universal categories
    categories = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.is_active.is_(True))
        .order_by(UniversalCategory.sort_order)
        .all()
    )

    # Build catalogs for manifest
    catalogs = []
    for category in categories:
        catalogs.append(
            {
                "id": category.id,
                "name": category.name,
                "type": _stremio_type(category.media_type),
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    manifest = {
        "id": "ai.recommendations.universal",
        "version": "1.0.0",
        "name": settings.addon_name,
        "description": "AI-powered Netflix-style content discovery",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": catalogs,
        "idPrefixes": ["tmdb"],
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    return JSONResponse(content=manifest)


@app.get("/manifest/universal.json")
async def universal_manifest_redirect():
    """Redirect legacy manifest URL to the correct path."""
    return RedirectResponse(url="/manifest.json", status_code=301)


@app.get("/{user_key}/manifest.json")
async def personalized_manifest(
    user_key: str, db: Session = Depends(get_db_dependency)
):
    """
    Stremio manifest for authenticated users (universal + personalized catalogs).

    URL pattern: /{user_key}/manifest.json
    Stremio derives base URL as /{user_key}/ so catalogs resolve to
    /{user_key}/catalog/{type}/{id}.json

    Args:
        user_key: Unique user identifier
    """
    # Find user
    user = db.query(User).filter(User.user_key == user_key).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get universal categories
    universal_categories = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.is_active.is_(True))
        .order_by(UniversalCategory.sort_order)
        .all()
    )

    # Get user's personalized catalogs
    user_catalogs = (
        db.query(UserCatalog)
        .filter(UserCatalog.user_id == user.id, UserCatalog.is_active.is_(True))
        .all()
    )

    # Build catalogs
    catalogs = []

    # Add universal catalogs
    for category in universal_categories:
        catalogs.append(
            {
                "id": f"universal-{category.id}",
                "name": category.name,
                "type": _stremio_type(category.media_type),
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    # Add personalized catalogs
    for catalog in user_catalogs:
        catalogs.append(
            {
                "id": f"personal-{catalog.slot_id}",
                "name": catalog.name,
                "type": _stremio_type(catalog.media_type),
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    manifest = {
        "id": f"ai.recommendations.{user_key}",
        "version": "1.0.0",
        "name": f"{settings.addon_name} - {user.trakt_username or 'Personal'}",
        "description": "AI-powered Netflix-style content discovery personalized for you",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": catalogs,
        "idPrefixes": ["tmdb"],
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    return JSONResponse(content=manifest)


@app.get("/manifest/{user_key}.json")
async def personalized_manifest_redirect(user_key: str):
    """Redirect legacy personalized manifest URL to the correct path."""
    return RedirectResponse(url=f"/{user_key}/manifest.json", status_code=301)


def _build_stremio_metas(items: list, catalog_type: str) -> list:
    """Convert catalog items to Stremio meta format."""
    metas = []
    for item in items:
        meta = {
            "id": f"tmdb:{item['tmdb_id']}",
            "type": catalog_type,
            "name": item["title"],
        }

        if item.get("poster"):
            meta["poster"] = f"https://image.tmdb.org/t/p/w500{item['poster']}"

        metas.append(meta)
    return metas


# ---- LRU catalog cache with TTL ----
_CACHE_MAX_ENTRIES = 256
_catalog_cache: dict[str, tuple[float, list]] = {}


def _cache_evict():
    """Evict oldest entries when cache exceeds max size."""
    while len(_catalog_cache) > _CACHE_MAX_ENTRIES:
        oldest_key = min(_catalog_cache, key=lambda k: _catalog_cache[k][0])
        del _catalog_cache[oldest_key]


def _get_shuffle_seed(catalog_id: str) -> int:
    """Deterministic shuffle seed based on catalog ID and current time window."""
    hours = settings.catalog_shuffle_hours
    if hours <= 0:
        return 0  # shuffle disabled
    window = int(time()) // (hours * 3600)
    raw = f"{catalog_id}:{window}"
    return int(hashlib.md5(raw.encode()).hexdigest()[:8], 16)


def _shuffle_items(items: list, catalog_id: str) -> list:
    """Shuffle items deterministically for the current time window."""
    seed = _get_shuffle_seed(catalog_id)
    if seed == 0:
        return items
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def _get_cached_catalog(
    catalog_id: str, db: Session, user_id: int | None = None
) -> list:
    """Get catalog items with TTL + LRU caching."""
    cache_key = f"{catalog_id}:user={user_id}"
    now = time()
    ttl = settings.cache_ttl

    if cache_key in _catalog_cache:
        cached_at, items = _catalog_cache[cache_key]
        if now - cached_at < ttl:
            # Touch: move to most-recent by re-inserting
            _catalog_cache[cache_key] = (now, items)
            return items
        # Expired -- remove stale entry
        del _catalog_cache[cache_key]

    generator = CatalogGenerator(db)
    items = generator.get_catalog_content(catalog_id, user_id=user_id)
    _catalog_cache[cache_key] = (now, items)
    _cache_evict()

    return items


def _serve_catalog(items: list, catalog_id: str, catalog_type: str, skip: int):
    """Shuffle, paginate, and return a catalog response with cache headers."""
    shuffled = _shuffle_items(items, catalog_id)
    page_size = settings.catalog_page_size
    paginated = shuffled[skip : skip + page_size]
    metas = _build_stremio_metas(paginated, catalog_type)

    # Cache-Control: let Stremio / browsers cache for a reasonable window
    shuffle_hours = max(settings.catalog_shuffle_hours, 1)
    max_age = shuffle_hours * 3600

    response = JSONResponse(content={"metas": metas})
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    return response


@app.get("/catalog/{catalog_type}/{catalog_id}.json")
async def universal_catalog(
    catalog_type: str,
    catalog_id: str,
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db_dependency),
):
    """
    Universal catalog endpoint (for anonymous users).

    Args:
        catalog_type: 'movie' or 'series'
        catalog_id: Category ID
        skip: Pagination offset
    """
    items = _get_cached_catalog(catalog_id, db)
    return _serve_catalog(items, catalog_id, catalog_type, skip)


@app.get("/{user_key}/catalog/{catalog_type}/{catalog_id}.json")
async def personalized_catalog(
    user_key: str,
    catalog_type: str,
    catalog_id: str,
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db_dependency),
):
    """
    Personalized catalog endpoint (for authenticated users).

    URL pattern: /{user_key}/catalog/{type}/{id}.json
    Matches the base URL derived from /{user_key}/manifest.json

    Args:
        user_key: Unique user identifier
        catalog_type: 'movie' or 'series'
        catalog_id: Category ID
        skip: Pagination offset
    """
    # Find user
    user = db.query(User).filter(User.user_key == user_key).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if universal or personal catalog
    if catalog_id.startswith("universal-"):
        actual_id = catalog_id.replace("universal-", "", 1)
        items = _get_cached_catalog(actual_id, db)
    elif catalog_id.startswith("personal-"):
        actual_id = catalog_id.replace("personal-", "", 1)
        items = _get_cached_catalog(actual_id, db, user_id=user.id)  # type: ignore[arg-type]
    else:
        raise HTTPException(status_code=404, detail="Catalog not found")

    return _serve_catalog(items, catalog_id, catalog_type, skip)


# OAuth endpoints for Trakt authentication
@app.get("/auth/start")
async def start_auth(
    password: str = Query(..., description="Master password"),
    db: Session = Depends(get_db_dependency),
):
    """
    Start Trakt OAuth flow (requires master password).

    Args:
        password: Master password
    """
    # Verify master password
    if password != settings.master_password:
        raise HTTPException(status_code=403, detail="Invalid master password")

    # Generate state for CSRF protection and persist it
    state = secrets.token_urlsafe(32)
    with get_db() as db_session:
        # Clean up expired states (older than 10 minutes)
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        db_session.query(OAuthState).filter(OAuthState.created_at < cutoff).delete()
        db_session.add(OAuthState(state=state))

    # Get Trakt authorization URL
    auth_url = trakt_client.get_authorization_url(state)

    return RedirectResponse(url=auth_url)


@app.get("/auth/trakt/callback")
async def trakt_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db_dependency),
):
    """
    Trakt OAuth callback endpoint.

    Args:
        code: Authorization code
        state: State for CSRF protection
    """
    # Verify CSRF state
    oauth_state = db.query(OAuthState).filter(OAuthState.state == state).first()
    if not oauth_state:
        return HTMLResponse(
            content=auth_error_html("Invalid or expired OAuth state. Please try again."),
            status_code=400,
        )

    # Check if state is expired (10 minute window)
    if datetime.utcnow() - oauth_state.created_at > timedelta(minutes=10):
        db.query(OAuthState).filter(OAuthState.state == state).delete()
        db.commit()
        return HTMLResponse(
            content=auth_error_html("OAuth state expired. Please try again."),
            status_code=400,
        )

    # Consume the state token (one-time use)
    db.query(OAuthState).filter(OAuthState.state == state).delete()
    db.commit()

    try:
        # Exchange code for token
        token_data = await trakt_client.exchange_code_for_token(code)

        # Get user profile
        profile = await trakt_client.get_user_profile(token_data["access_token"])

        # Encrypt tokens before storage
        encrypted_access = encrypt_token(token_data["access_token"])
        encrypted_refresh = encrypt_token(token_data["refresh_token"])
        expires_at = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])

        # Create or update user
        user = (
            db.query(User)
            .filter(User.trakt_user_id == str(profile["ids"]["slug"]))
            .first()
        )

        if user:
            # Update existing user
            user.trakt_access_token = encrypted_access
            user.trakt_refresh_token = encrypted_refresh
            user.trakt_token_expires_at = expires_at
            user.last_login = datetime.utcnow()  # type: ignore[assignment]
        else:
            # Create new user
            user_key = secrets.token_urlsafe(32)
            user = User(
                user_key=user_key,
                trakt_user_id=str(profile["ids"]["slug"]),
                trakt_username=profile.get("username"),
                trakt_access_token=encrypted_access,
                trakt_refresh_token=encrypted_refresh,
                trakt_token_expires_at=expires_at,
            )
            db.add(user)

        db.commit()

        # Return success page
        manifest_url = f"{settings.base_url}/{user.user_key}/manifest.json"
        username = user.trakt_username or user.trakt_user_id

        return HTMLResponse(
            content=auth_success_html(username, manifest_url, user.user_key)
        )

    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        return HTMLResponse(
            content=auth_error_html(
                "Could not complete Trakt authentication. Please try again."
            ),
            status_code=500,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
