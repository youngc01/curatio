"""
Main FastAPI application for Stremio AI Addon.

Provides Stremio manifest and catalog endpoints.
"""

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import secrets
from loguru import logger

from app.config import settings, validate_api_keys
from app.database import get_db_dependency, init_database, check_database_connection
from app.models import User, UniversalCategory, UserCatalog
from app.catalog_generator import CatalogGenerator
from app.trakt_client import trakt_client

# Initialize FastAPI app
app = FastAPI(
    title="Stremio AI Recommendations",
    description="Netflix-style AI-powered content discovery for Stremio",
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


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    logger.info("Starting Stremio AI Addon...")

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

    logger.info(f"Addon ready at {settings.base_url}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Stremio AI Addon...")


@app.get("/")
async def root():
    """Root endpoint - redirect to addon configuration page."""
    return {
        "name": settings.addon_name,
        "version": "1.0.0",
        "description": "AI-powered Netflix-style content discovery",
        "manifest_url": f"{settings.base_url}/manifest/universal.json",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    db_ok = check_database_connection()

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "version": "1.0.0",
    }


@app.get("/manifest/universal.json")
async def universal_manifest(db: Session = Depends(get_db_dependency)):
    """
    Stremio manifest for anonymous users (universal catalogs only).

    This is installed when users don't sign in with Trakt.
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
                "type": category.media_type,
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
        "idPrefixes": ["tt"],  # IMDB IDs
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    return JSONResponse(content=manifest)


@app.get("/manifest/{user_key}.json")
async def personalized_manifest(
    user_key: str, db: Session = Depends(get_db_dependency)
):
    """
    Stremio manifest for authenticated users (universal + personalized catalogs).

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
                "type": category.media_type,
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    # Add personalized catalogs
    for catalog in user_catalogs:
        catalogs.append(
            {
                "id": f"personal-{catalog.slot_id}",
                "name": catalog.name,
                "type": catalog.media_type,
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
        "idPrefixes": ["tt"],
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    return JSONResponse(content=manifest)


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
    generator = CatalogGenerator(db)

    # Get catalog content
    items = generator.get_catalog_content(catalog_id)

    # Apply pagination
    paginated_items = items[skip : skip + 100]

    # Convert to Stremio format
    metas = []
    for item in paginated_items:
        meta = {
            "id": f"tmdb:{item['tmdb_id']}",
            "type": catalog_type,
            "name": item["title"],
        }

        if item.get("poster"):
            meta["poster"] = f"https://image.tmdb.org/t/p/w500{item['poster']}"

        metas.append(meta)

    return JSONResponse(content={"metas": metas})


@app.get("/catalog/{user_key}/{catalog_type}/{catalog_id}.json")
async def personalized_catalog(
    user_key: str,
    catalog_type: str,
    catalog_id: str,
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db_dependency),
):
    """
    Personalized catalog endpoint (for authenticated users).

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

    generator = CatalogGenerator(db)

    # Check if universal or personal catalog
    if catalog_id.startswith("universal-"):
        actual_id = catalog_id.replace("universal-", "")
        items = generator.get_catalog_content(actual_id)
    elif catalog_id.startswith("personal-"):
        actual_id = catalog_id.replace("personal-", "")
        items = generator.get_catalog_content(actual_id, user_id=user.id)  # type: ignore[arg-type]
    else:
        raise HTTPException(status_code=404, detail="Catalog not found")

    # Apply pagination
    paginated_items = items[skip : skip + 100]

    # Convert to Stremio format
    metas = []
    for item in paginated_items:
        meta = {
            "id": f"tmdb:{item['tmdb_id']}",
            "type": catalog_type,
            "name": item["title"],
        }

        if item.get("poster"):
            meta["poster"] = f"https://image.tmdb.org/t/p/w500{item['poster']}"

        metas.append(meta)

    return JSONResponse(content={"metas": metas})


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

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # TODO: Store state in session or database

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
    # TODO: Verify state

    try:
        # Exchange code for token
        token_data = await trakt_client.exchange_code_for_token(code)

        # Get user profile
        profile = await trakt_client.get_user_profile(token_data["access_token"])

        # Create or update user
        user = (
            db.query(User)
            .filter(User.trakt_user_id == str(profile["ids"]["slug"]))
            .first()
        )

        if user:
            # Update existing user
            user.trakt_access_token = token_data["access_token"]
            user.trakt_refresh_token = token_data["refresh_token"]
            user.last_login = datetime.utcnow()  # type: ignore[assignment]
        else:
            # Create new user
            user_key = secrets.token_urlsafe(32)
            user = User(
                user_key=user_key,
                trakt_user_id=str(profile["ids"]["slug"]),
                trakt_username=profile.get("username"),
                trakt_access_token=token_data["access_token"],
                trakt_refresh_token=token_data["refresh_token"],
                trakt_token_expires_at=datetime.utcnow()
                + timedelta(seconds=token_data["expires_in"]),
            )
            db.add(user)

        db.commit()

        # Return installation URL
        manifest_url = f"{settings.base_url}/manifest/{user.user_key}.json"

        return {
            "success": True,
            "manifest_url": manifest_url,
            "install_url": f"stremio://{settings.base_url.replace('https://', '')}/manifest/{user.user_key}.json/manifest.json",
            "message": "Authentication successful! Click the install URL to add to Stremio.",
        }

    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
