"""
Main FastAPI application for Curatio.

Provides Stremio manifest and catalog endpoints.
"""

import asyncio
import hashlib
import random
from time import time
from urllib.parse import quote_plus

import httpx
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import secrets
from loguru import logger

from app.config import settings, validate_api_keys
from app.database import (
    get_db_dependency,
    get_db,
    init_database,
    check_database_connection,
)
from app.models import (
    User,
    UniversalCategory,
    UserCatalog,
    OAuthState,
    InviteCode,
    AdminSetting,
    MediaMetadata,
    WatchEvent,
)
from app.catalog_generator import CatalogGenerator
from app.trakt_client import trakt_client
from app.crypto import encrypt_token, decrypt_token
from pydantic import BaseModel
from typing import Optional, Literal

from app.landing import landing_page_html, auth_success_html, auth_error_html
from app.admin import router as admin_router, load_settings_from_db
from app.account import router as account_router
from app.auth import (
    hash_password,
    verify_password,
    generate_totp_secret,
    get_totp_provisioning_uri,
    verify_totp,
    generate_totp_qr_data_url,
    encrypt_totp_secret,
    decrypt_totp_secret,
    create_user_session,
    verify_user_session,
    create_pairing_session,
    claim_pairing_session,
    create_device_pairing_session,
    claim_device_pairing_session,
    poll_device_pairing_session,
)
from app.stream_proxy import get_streams
from app.models import UserSession

# ---- Manifest cache (avoids DB query on every manifest request) ----
_manifest_cache: dict[str, tuple[float, dict]] = {}
_MANIFEST_TTL = 300  # 5 minutes


def _invalidate_manifest_cache():
    """Call after admin changes categories to clear cached manifests."""
    _manifest_cache.clear()


# ---- Install token (secret URL segment for universal manifest) ----
_install_token_cache: str | None = None


def get_install_token() -> str:
    """Get or create the install token for the universal manifest URL.

    Generated once and stored in admin_settings. This prevents anyone
    from guessing the manifest URL just by knowing the domain.
    """
    global _install_token_cache
    if _install_token_cache:
        return _install_token_cache

    with get_db() as db:
        row = db.query(AdminSetting).filter(AdminSetting.key == "INSTALL_TOKEN").first()
        if row:
            _install_token_cache = row.value
            return row.value

        # First time: generate and persist
        token = secrets.token_urlsafe(16)
        db.add(AdminSetting(key="INSTALL_TOKEN", value=token))
        db.commit()
        _install_token_cache = token
        return token


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


def _schedule_user_sync(user_id: int, access_token: str):
    """Fire-and-forget background task to sync a user's Trakt catalogs."""

    async def _do_sync():
        from workers.trakt_sync import sync_user_catalogs

        try:
            with get_db() as db:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    await sync_user_catalogs(user, db, access_token)
        except Exception as e:
            logger.error(f"Background sync failed for user {user_id}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_sync())
        logger.info(f"Scheduled background Trakt sync for user {user_id}")
    except RuntimeError:
        logger.warning("No event loop — skipping background sync")


class ScrobbleRequest(BaseModel):
    """Payload sent by the custom Stremio client on playback completion."""

    tmdb_id: int
    media_type: Literal["movie", "tv"]
    action: Literal["complete"] = "complete"
    season: Optional[int] = None
    episode: Optional[int] = None
    title: Optional[str] = None


# Debounce tracking for local catalog rebuilds
_last_local_sync: dict[int, float] = {}
_LOCAL_SYNC_DEBOUNCE = 1800  # 30 minutes


def _schedule_local_sync(user_id: int):
    """Fire-and-forget background task to rebuild a local user's catalogs."""
    now = time()
    last = _last_local_sync.get(user_id, 0)
    if now - last < _LOCAL_SYNC_DEBOUNCE:
        return  # debounce — too soon since last sync

    _last_local_sync[user_id] = now

    async def _do_sync():
        from workers.trakt_sync import sync_local_user_catalogs

        try:
            with get_db() as db:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    await sync_local_user_catalogs(user, db)
        except Exception as e:
            logger.error(f"Background local sync failed for user {user_id}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_sync())
        logger.info(f"Scheduled background local sync for user {user_id}")
    except RuntimeError:
        logger.warning("No event loop — skipping background local sync")


# Rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize FastAPI app
app = FastAPI(
    title="Curatio",
    description="AI-curated cinema for Stremio",
    version="1.0.0",
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
    )


# GZip middleware — compress responses >=500 bytes (big win on mobile)
app.add_middleware(GZipMiddleware, minimum_size=500)

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

# Mount account pages
app.include_router(account_router)


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

    # Close HTTP clients so connections don't leak
    from app.tmdb_client import tmdb_client

    await tmdb_client.close()
    await trakt_client.close()
    logger.info("HTTP clients closed")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Landing page with install options and Trakt connect."""
    response = HTMLResponse(content=landing_page_html())
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/health")
def health_check():
    """Health check endpoint.

    Uses ``def`` so the synchronous DB ping runs in a threadpool and
    does not block the event loop.
    """
    db_ok = check_database_connection()

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "version": "1.0.0",
    }


@app.get("/manifest.json")
async def bare_manifest_blocked():
    """Block bare /manifest.json — install token required."""
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/{user_key}/manifest.json")
async def manifest(user_key: str, db: Session = Depends(get_db_dependency)):
    """
    Stremio manifest endpoint.

    Serves the universal manifest when user_key matches the install token,
    or a personalized manifest when it matches a Trakt user's key.

    URL pattern: /{user_key}/manifest.json
    Stremio derives base URL as /{user_key}/ so catalogs resolve to
    /{user_key}/catalog/{type}/{id}.json
    """
    install_token = get_install_token()

    if user_key == install_token:
        # ---- Universal manifest (install-token access) ----
        now = time()
        cache_key = "universal"
        if cache_key in _manifest_cache:
            cached_at, cached_manifest = _manifest_cache[cache_key]
            if now - cached_at < _MANIFEST_TTL:
                response = JSONResponse(content=cached_manifest)
                response.headers["Cache-Control"] = "public, max-age=3600"
                return response

        categories = (
            db.query(UniversalCategory)
            .filter(UniversalCategory.is_active.is_(True))
            .order_by(UniversalCategory.sort_order)
            .all()
        )

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

        # Search catalogs at the end
        catalogs.append(
            {
                "id": "search",
                "name": settings.addon_name,
                "type": "movie",
                "extra": [{"name": "search", "isRequired": True}],
            }
        )
        catalogs.append(
            {
                "id": "search",
                "name": settings.addon_name,
                "type": "series",
                "extra": [{"name": "search", "isRequired": True}],
            }
        )

        manifest_data = {
            "id": "ai.recommendations.universal",
            "version": "1.0.0",
            "name": settings.addon_name,
            "description": "AI-powered Netflix-style content discovery",
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series"],
            "catalogs": catalogs,
            "idPrefixes": ["tmdb"],
            "behaviorHints": {"configurable": True, "configurationRequired": False},
        }

        _manifest_cache[cache_key] = (now, manifest_data)

        response = JSONResponse(content=manifest_data)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # ---- Personalized manifest (Trakt user) ----
    user = db.query(User).filter(User.user_key == user_key).first()

    if not user:
        raise HTTPException(status_code=404, detail="Not found")

    # Get universal categories
    universal_categories = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.is_active.is_(True))
        .order_by(UniversalCategory.sort_order)
        .all()
    )

    # Get user's personalized catalogs, ordered like Netflix:
    # BYW first, then recommendations, trending, popular, then universal
    from workers.trakt_sync import get_slot_sort_order

    user_catalogs = (
        db.query(UserCatalog)
        .filter(UserCatalog.user_id == user.id, UserCatalog.is_active.is_(True))
        .all()
    )
    user_catalogs.sort(key=lambda c: get_slot_sort_order(c.slot_id))

    catalogs = []

    # Personalized catalogs FIRST (like Netflix/Prime/HBO)
    for catalog in user_catalogs:
        catalogs.append(
            {
                "id": f"personal-{catalog.slot_id}",
                "name": catalog.name,
                "type": _stremio_type(catalog.media_type),
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    # Then universal AI-tag categories
    for category in universal_categories:
        catalogs.append(
            {
                "id": f"universal-{category.id}",
                "name": category.name,
                "type": _stremio_type(category.media_type),
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )

    # Search catalogs at the end
    catalogs.append(
        {
            "id": "search",
            "name": settings.addon_name,
            "type": "movie",
            "extra": [{"name": "search", "isRequired": True}],
        }
    )
    catalogs.append(
        {
            "id": "search",
            "name": settings.addon_name,
            "type": "series",
            "extra": [{"name": "search", "isRequired": True}],
        }
    )

    manifest_data = {
        "id": f"ai.recommendations.{user_key}",
        "version": "1.0.0",
        "name": f"{settings.addon_name} - {user.trakt_username or user.display_name or 'Personal'}",
        "description": "AI-powered Netflix-style content discovery personalized for you",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "catalogs": catalogs,
        "idPrefixes": ["tmdb"],
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    response = JSONResponse(content=manifest_data)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def _build_stremio_metas(items: list, catalog_type: str) -> list:
    """Convert catalog items to Stremio meta format with rich metadata.

    Includes releaseInfo, genres, description, and rating so Stremio can
    render richer catalog cards without extra cinemeta lookups.
    """
    metas = []
    for item in items:
        meta: dict = {
            "id": f"tmdb:{item['tmdb_id']}",
            "type": catalog_type,
            "name": item["title"],
            "posterShape": "poster",
        }

        if item.get("poster"):
            meta["poster"] = f"https://image.tmdb.org/t/p/w500{item['poster']}"

        if item.get("year"):
            meta["releaseInfo"] = item["year"]

        if item.get("description"):
            meta["description"] = item["description"]

        if item.get("rating"):
            meta["imdbRating"] = str(round(item["rating"], 1))

        if item.get("genres"):
            meta["genres"] = item["genres"]

        if item.get("backdrop"):
            meta["background"] = f"https://image.tmdb.org/t/p/w1280{item['backdrop']}"

        if item.get("imdb_id"):
            meta["imdb_id"] = item["imdb_id"]
            if catalog_type == "movie":
                meta["behaviorHints"] = {"defaultVideoId": item["imdb_id"]}

        if item.get("logo"):
            meta["logo"] = f"https://image.tmdb.org/t/p/w500{item['logo']}"

        metas.append(meta)
    return metas


def _build_rich_meta(detail: dict, tmdb_type: str, stremio_type: str) -> dict:
    """Build a rich Stremio meta object from a TMDB detail response.

    Includes cast, director, writer, runtime, logo, and links with profile photos.
    """
    tmdb_id = detail["id"]
    if tmdb_type == "movie":
        title = detail.get("title", "Unknown")
        release_date = detail.get("release_date", "")
    else:
        title = detail.get("name", "Unknown")
        release_date = detail.get("first_air_date", "")

    meta: dict = {
        "id": f"tmdb:{tmdb_id}",
        "type": stremio_type,
        "name": title,
        "posterShape": "poster",
    }

    if detail.get("overview"):
        meta["description"] = detail["overview"]

    if release_date and len(release_date) >= 4:
        meta["releaseInfo"] = release_date[:4]

    if detail.get("vote_average"):
        meta["imdbRating"] = str(round(detail["vote_average"], 1))

    if detail.get("genres"):
        meta["genres"] = [g["name"] for g in detail["genres"] if g.get("name")]

    # Images
    if detail.get("poster_path"):
        meta["poster"] = f"https://image.tmdb.org/t/p/w500{detail['poster_path']}"
    if detail.get("backdrop_path"):
        meta["background"] = (
            f"https://image.tmdb.org/t/p/w1280{detail['backdrop_path']}"
        )

    logos = detail.get("images", {}).get("logos", [])
    if logos:
        logo_path = logos[0].get("file_path")
        if logo_path:
            meta["logo"] = f"https://image.tmdb.org/t/p/w500{logo_path}"

    # Cast, director, writer from credits
    credits = detail.get("credits", {})
    cast_list = credits.get("cast", [])[:20]
    crew_list = credits.get("crew", [])

    if cast_list:
        meta["cast"] = [p["name"] for p in cast_list if p.get("name")]

    directors = [p["name"] for p in crew_list if p.get("job") == "Director"]
    if not directors and tmdb_type == "tv":
        directors = [p["name"] for p in detail.get("created_by", []) if p.get("name")]
    if directors:
        meta["director"] = directors

    writers = list(
        dict.fromkeys(  # dedupe preserving order
            p["name"]
            for p in crew_list
            if p.get("department") == "Writing" and p.get("name")
        )
    )[:5]
    if writers:
        meta["writer"] = writers

    # Runtime
    if tmdb_type == "movie" and detail.get("runtime"):
        mins = detail["runtime"]
        meta["runtime"] = (
            f"{mins // 60}h {mins % 60}min" if mins >= 60 else f"{mins}min"
        )
    elif tmdb_type == "tv":
        run_times = detail.get("episode_run_time", [])
        if run_times:
            mins = run_times[0]
            meta["runtime"] = f"{mins}min"

    # Links (cast + directors with profile photos and search URLs)
    links = []
    for person in cast_list:
        name = person.get("name")
        if not name:
            continue
        link: dict = {
            "name": name,
            "category": "Cast",
            "url": f"stremio:///search?search={quote_plus(name)}",
        }
        if person.get("profile_path"):
            link["poster"] = f"https://image.tmdb.org/t/p/w185{person['profile_path']}"
        links.append(link)

    for name in directors:
        links.append(
            {
                "name": name,
                "category": "Directors",
                "url": f"stremio:///search?search={quote_plus(name)}",
            }
        )

    if links:
        meta["links"] = links

    # IMDb ID and behavior hints
    imdb_id = (
        detail.get("external_ids", {}).get("imdb_id")
        if isinstance(detail.get("external_ids"), dict)
        else None
    )
    if imdb_id:
        meta["imdb_id"] = imdb_id
        if tmdb_type == "movie":
            meta["behaviorHints"] = {"defaultVideoId": imdb_id}

    # Trailers from videos
    videos = (
        detail.get("videos", {}).get("results", [])
        if isinstance(detail.get("videos"), dict)
        else []
    )
    trailers = [
        {"source": v["key"], "type": "Trailer"}
        for v in videos
        if v.get("type") == "Trailer" and v.get("site") == "YouTube" and v.get("key")
    ][:5]
    if trailers:
        meta["trailers"] = trailers

    return meta


# ---- Meta cache (avoids repeated TMDB calls for popular items) ----
_meta_cache: dict[str, tuple[float, dict | list]] = {}
_META_CACHE_TTL = 3600  # 1 hour
_META_CACHE_MAX = 512


def _meta_cache_evict():
    """Evict oldest entries when meta cache exceeds max size."""
    while len(_meta_cache) > _META_CACHE_MAX:
        oldest_key = min(_meta_cache, key=lambda k: _meta_cache[k][0])
        del _meta_cache[oldest_key]


def _opportunistic_backfill(tmdb_id: int, tmdb_type: str, detail: dict):
    """Update DB row with imdb_id/logo_path if missing (fire-and-forget)."""
    try:
        ext_ids = detail.get("external_ids", {})
        imdb_id = ext_ids.get("imdb_id") if isinstance(ext_ids, dict) else None

        logos = (
            detail.get("images", {}).get("logos", [])
            if isinstance(detail.get("images"), dict)
            else []
        )
        logo_path = None
        if logos:
            en_logos = [lg for lg in logos if lg.get("iso_639_1") in ("en", None)]
            chosen = en_logos[0] if en_logos else logos[0]
            logo_path = chosen.get("file_path")

        if not imdb_id and not logo_path:
            return

        with get_db() as db:
            row = (
                db.query(MediaMetadata)
                .filter_by(tmdb_id=tmdb_id, media_type=tmdb_type)
                .first()
            )
            if row and (not row.imdb_id or not row.logo_path):
                if imdb_id and not row.imdb_id:
                    row.imdb_id = imdb_id
                if logo_path and not row.logo_path:
                    row.logo_path = logo_path
                db.commit()
    except Exception as e:
        logger.debug(f"Opportunistic backfill failed for {tmdb_type}/{tmdb_id}: {e}")


# ---- LRU catalog cache with TTL ----
_CACHE_MAX_ENTRIES = 256
_catalog_cache: dict[str, tuple[float, list]] = {}

# ---- User-key → User lookup cache (avoids DB hit on every request) ----
_user_cache: dict[str, tuple[float, "User | None"]] = {}
_USER_CACHE_TTL = 300  # 5 minutes


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
    """Shuffle items deterministically for the current time window.

    Top 10 and Up Next catalogs are never shuffled — they stay in ranked order.
    """
    if "top10-" in catalog_id or "up-next" in catalog_id:
        return items
    seed = _get_shuffle_seed(catalog_id)
    if seed == 0:
        return items
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def _search_media(db: Session, catalog_type: str, query: str) -> list:
    """Search MediaMetadata by title, returning results sorted by popularity."""
    from urllib.parse import unquote

    query = unquote(query).strip()
    if not query:
        return []

    media_type = "tv" if catalog_type == "series" else catalog_type
    search_pattern = f"%{query}%"

    results = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.media_type == media_type,
            MediaMetadata.title.ilike(search_pattern),
        )
        .order_by(MediaMetadata.popularity.desc().nullslast())
        .limit(50)
        .all()
    )

    items = []
    for r in results:
        year = ""
        if r.release_date and len(r.release_date) >= 4:
            year = r.release_date[:4]
        items.append(
            {
                "tmdb_id": r.tmdb_id,
                "title": r.title,
                "poster": r.poster_path,
                "backdrop": r.backdrop_path,
                "year": year,
                "description": r.overview,
                "rating": r.vote_average,
                "genres": r.genres if isinstance(r.genres, list) else [],
                "imdb_id": r.imdb_id,
                "logo": r.logo_path,
            }
        )
    return items


def _get_cached_user(user_key: str, db: Session) -> "User | None":
    """Look up user by key with in-memory caching."""
    now = time()
    if user_key in _user_cache:
        cached_at, user = _user_cache[user_key]
        if now - cached_at < _USER_CACHE_TTL:
            return user

    user = db.query(User).filter(User.user_key == user_key).first()
    _user_cache[user_key] = (now, user)
    return user


def _get_cached_catalog(
    catalog_id: str,
    db: Session | None = None,
    user_id: int | None = None,
    hide_foreign: bool = False,
    hide_adult: bool = False,
    hide_unreleased: bool = False,
) -> list:
    """Get catalog items with TTL + LRU caching.

    Only opens a DB session on cache miss. Serves stale cached data when
    the database is temporarily unreachable (e.g. transient DNS failures).
    """
    cache_key = f"{catalog_id}:user={user_id}:f={hide_foreign}:a={hide_adult}:u={hide_unreleased}"
    now = time()
    ttl = settings.cache_ttl

    if cache_key in _catalog_cache:
        cached_at, items = _catalog_cache[cache_key]
        if now - cached_at < ttl:
            # Touch: move to most-recent by re-inserting
            _catalog_cache[cache_key] = (now, items)
            return items

    # Cache miss or expired — try refreshing from DB
    try:
        if db is None:
            from app.database import get_db_session

            db = get_db_session()
            _close_db = True
        else:
            _close_db = False

        try:
            generator = CatalogGenerator(db)
            items = generator.get_catalog_content(
                catalog_id,
                user_id=user_id,
                hide_foreign=hide_foreign,
                hide_adult=hide_adult,
                hide_unreleased=hide_unreleased,
            )
            _catalog_cache[cache_key] = (now, items)
            _cache_evict()
            return items
        finally:
            if _close_db:
                db.close()
    except Exception as e:
        # DB unreachable — serve stale cache if available
        if cache_key in _catalog_cache:
            _, items = _catalog_cache[cache_key]
            logger.warning(
                f"DB error for catalog '{catalog_id}', serving stale cache: {e}"
            )
            return items
        logger.error(f"DB error for catalog '{catalog_id}', no cached data: {e}")
        return []


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


def _parse_extras(extra_str: str) -> dict:
    """Parse Stremio path-based extras like 'skip=100&genre=Action' into a dict."""
    params: dict = {}
    for part in extra_str.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v
    return params


@app.get("/catalog/{catalog_type}/{catalog_id}.json")
async def bare_catalog_blocked(catalog_type: str, catalog_id: str):
    """Block bare /catalog/ — install token required."""
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/catalog/{catalog_type}/{catalog_id}/{extra}.json")
async def bare_catalog_extra_blocked(catalog_type: str, catalog_id: str, extra: str):
    """Block bare /catalog/ with extras — install token required."""
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/{user_key}/catalog/{catalog_type}/{catalog_id}/{extra}.json")
def catalog_with_extra(
    user_key: str,
    catalog_type: str,
    catalog_id: str,
    extra: str,
    db: Session = Depends(get_db_dependency),
):
    """Catalog endpoint that handles Stremio path-based extras (e.g. skip=100).

    Some Stremio clients pass extras as a path segment:
      /{key}/catalog/{type}/{id}/skip=100.json
    instead of as a query parameter:
      /{key}/catalog/{type}/{id}.json?skip=100

    Uses ``def`` (not ``async def``) so synchronous DB queries run in a
    threadpool and do not block the event loop under heavy load.
    """
    params = _parse_extras(extra)
    skip = int(params.get("skip", 0))
    if skip < 0:
        skip = 0
    search = params.get("search")
    return catalog(user_key, catalog_type, catalog_id, skip, db, search=search)


@app.get("/{user_key}/catalog/{catalog_type}/{catalog_id}.json")
def catalog(
    user_key: str,
    catalog_type: str,
    catalog_id: str,
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db_dependency),
    search: str | None = None,
):
    """
    Catalog endpoint for both universal (install-token) and personalized (user-key) access.

    Uses ``def`` (not ``async def``) so synchronous DB queries run in a
    threadpool and do not block the event loop under heavy load.

    URL pattern: /{user_key}/catalog/{type}/{id}.json
    Matches the base URL derived from /{user_key}/manifest.json
    """
    install_token = get_install_token()

    # Validate user_key
    if user_key != install_token:
        user = _get_cached_user(user_key, db)
        if not user:
            raise HTTPException(status_code=404, detail="Not found")

    # Search catalog
    if catalog_id == "search" and search:
        items = _search_media(db, catalog_type, search)
        metas = _build_stremio_metas(items, catalog_type)
        response = JSONResponse(content={"metas": metas})
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    if user_key == install_token:
        # Universal catalog — use global filter defaults
        items = _get_cached_catalog(
            catalog_id,
            db,
            hide_foreign=settings.hide_foreign,
            hide_adult=settings.hide_adult,
            hide_unreleased=settings.hide_unreleased,
        )
        return _serve_catalog(items, catalog_id, catalog_type, skip)

    # Personalized catalog — find the user (cached to avoid DB hit on every request)
    user = _get_cached_user(user_key, db)

    if not user:
        raise HTTPException(status_code=404, detail="Not found")

    # Use global filter settings (controlled via admin panel)
    hf = settings.hide_foreign
    ha = settings.hide_adult
    hu = settings.hide_unreleased

    if catalog_id.startswith("universal-"):
        actual_id = catalog_id.replace("universal-", "", 1)
        items = _get_cached_catalog(
            actual_id, db, hide_foreign=hf, hide_adult=ha, hide_unreleased=hu
        )
    elif catalog_id.startswith("personal-"):
        actual_id = catalog_id.replace("personal-", "", 1)
        items = _get_cached_catalog(actual_id, db, user_id=user.id, hide_foreign=hf, hide_adult=ha, hide_unreleased=hu)  # type: ignore[arg-type]
    else:
        raise HTTPException(status_code=404, detail="Catalog not found")

    return _serve_catalog(items, catalog_id, catalog_type, skip)


# ---------------------------------------------------------------------------
# Meta endpoint — returns rich detail for a single item (cast, images, etc.)
# ---------------------------------------------------------------------------


@app.get("/meta/{meta_type}/{meta_id}.json")
async def bare_meta_blocked(meta_type: str, meta_id: str):
    """Block bare /meta/ — install token required."""
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/{user_key}/meta/{meta_type}/{meta_id}.json")
async def meta_handler(user_key: str, meta_type: str, meta_id: str):
    """Stremio meta endpoint — returns detailed info with cast, director, images.

    Fetches from TMDB on-demand and caches in memory for 1 hour.
    Uses ``async def`` because the TMDB client is async.
    """
    if not meta_id.startswith("tmdb:"):
        raise HTTPException(status_code=404, detail="Invalid item ID")

    try:
        tmdb_id = int(meta_id.split(":", 1)[1])
    except (ValueError, IndexError):
        raise HTTPException(status_code=404, detail="Invalid item ID")

    tmdb_type = "tv" if meta_type == "series" else meta_type

    # Check in-memory cache
    cache_key = f"meta:{tmdb_type}:{tmdb_id}"
    now = time()
    if cache_key in _meta_cache:
        cached_at, cached_meta = _meta_cache[cache_key]
        if now - cached_at < _META_CACHE_TTL:
            response = JSONResponse(content={"meta": cached_meta})
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response

    from app.tmdb_client import tmdb_client

    try:
        if tmdb_type == "movie":
            detail = await tmdb_client.get_movie(tmdb_id)
        else:
            detail = await tmdb_client.get_tv_show(tmdb_id)

        meta = _build_rich_meta(detail, tmdb_type, meta_type)

        # Series: fetch episode videos for all seasons
        if tmdb_type == "tv" and detail.get("number_of_seasons"):
            seasons = detail.get("seasons", [])
            regular_seasons = [s for s in seasons if s.get("season_number", 0) > 0]

            async def _fetch_season(s_num: int):
                try:
                    return await tmdb_client.get_tv_season(tmdb_id, s_num)
                except Exception:
                    return None

            season_results = await asyncio.gather(
                *[_fetch_season(s["season_number"]) for s in regular_seasons]
            )

            videos = []
            for season_data in season_results:
                if not season_data:
                    continue
                for ep in season_data.get("episodes", []):
                    video = {
                        "id": f"tmdb:{tmdb_id}:{ep.get('season_number', 0)}:{ep['episode_number']}",
                        "title": ep.get("name", f"Episode {ep['episode_number']}"),
                        "season": ep.get("season_number", 0),
                        "episode": ep["episode_number"],
                        "overview": ep.get("overview", ""),
                        "released": ep.get("air_date", ""),
                    }
                    if ep.get("still_path"):
                        video["thumbnail"] = (
                            f"https://image.tmdb.org/t/p/w300{ep['still_path']}"
                        )
                    videos.append(video)

            if videos:
                meta["videos"] = videos

        # Opportunistic backfill: update DB with imdb_id/logo_path if missing
        _opportunistic_backfill(tmdb_id, tmdb_type, detail)

        _meta_cache[cache_key] = (now, meta)
        _meta_cache_evict()

        response = JSONResponse(content={"meta": meta})
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    except Exception as e:
        logger.warning(f"TMDB detail fetch failed for {tmdb_type}/{tmdb_id}: {e}")
        # Return minimal meta so Stremio doesn't show an error
        response = JSONResponse(
            content={
                "meta": {
                    "id": meta_id,
                    "type": meta_type,
                    "name": "",
                }
            }
        )
        return response


# ---------------------------------------------------------------------------
# Similar catalog — returns items similar to the given item
# ---------------------------------------------------------------------------


@app.get("/catalog/{catalog_type}/tmdb-similar/{meta_id}.json")
async def bare_similar_blocked(catalog_type: str, meta_id: str):
    """Block bare /catalog/similar — install token required."""
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/{user_key}/catalog/{catalog_type}/tmdb-similar/{meta_id}.json")
async def similar_catalog(user_key: str, catalog_type: str, meta_id: str):
    """Catalog of items similar to the given item.

    Fetches from TMDB's similar endpoint on-demand.
    Uses ``async def`` because the TMDB client is async.
    """
    if not meta_id.startswith("tmdb:"):
        raise HTTPException(status_code=404, detail="Invalid item ID")

    try:
        tmdb_id = int(meta_id.split(":", 1)[1])
    except (ValueError, IndexError):
        raise HTTPException(status_code=404, detail="Invalid item ID")

    tmdb_type = "tv" if catalog_type == "series" else catalog_type

    # Check in-memory cache
    cache_key = f"similar:{tmdb_type}:{tmdb_id}"
    now = time()
    if cache_key in _meta_cache:
        cached_at, cached_metas = _meta_cache[cache_key]
        if now - cached_at < _META_CACHE_TTL:
            response = JSONResponse(content={"metas": cached_metas})
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response

    from app.tmdb_client import tmdb_client

    try:
        if tmdb_type == "movie":
            data = await tmdb_client.get_similar_movies(tmdb_id)
        else:
            data = await tmdb_client.get_similar_tv_shows(tmdb_id)

        items = []
        for r in data.get("results", [])[:20]:
            item = {
                "tmdb_id": r["id"],
                "title": r.get("title") or r.get("name", "Unknown"),
                "poster": r.get("poster_path"),
                "backdrop": r.get("backdrop_path"),
                "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
                "description": r.get("overview", ""),
                "rating": r.get("vote_average"),
                "genres": None,  # discover results only have genre_ids
            }
            items.append(item)

        metas = _build_stremio_metas(items, catalog_type)

        _meta_cache[cache_key] = (now, metas)
        _meta_cache_evict()

        response = JSONResponse(content={"metas": metas})
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    except Exception as e:
        logger.warning(f"TMDB similar fetch failed for {tmdb_type}/{tmdb_id}: {e}")
        return JSONResponse(content={"metas": []})


# ---------------------------------------------------------------------------
# Scrobble registration and event endpoints
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Account registration request."""

    invite: str
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    """Login request."""

    email: str
    password: str
    totp_code: str = ""


class TOTPConfirmRequest(BaseModel):
    """2FA confirmation request."""

    code: str


class DeviceClaimRequest(BaseModel):
    """Device pairing claim request (authenticated user claims a device code)."""

    short_code: str


def _get_user_from_session(request: Request, db: Session) -> User:
    """Extract and verify user from session cookie."""
    token = request.cookies.get("user_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = verify_user_session(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    return user


@app.post("/auth/register")
@limiter.limit("5/minute")
async def register_account(request: Request, payload: RegisterRequest):
    """Register a new account with email, password, and invite code.

    Creates the user but does not return user_key — user must set up 2FA
    and pair the app separately.
    """
    if not payload.invite:
        raise HTTPException(status_code=403, detail="Invite code required")
    if not payload.email or "@" not in payload.email:
        raise HTTPException(status_code=400, detail="Valid email required")
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )

    with get_db() as db_session:
        # Check email uniqueness
        existing = (
            db_session.query(User)
            .filter(User.email == payload.email.lower().strip())
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")

        # Validate invite code
        invite_row = (
            db_session.query(InviteCode)
            .filter(InviteCode.code == payload.invite, InviteCode.is_used.is_(False))
            .first()
        )
        if invite_row:
            if invite_row.expires_at and datetime.utcnow() > invite_row.expires_at:
                raise HTTPException(status_code=403, detail="Invite code has expired")
            invite_row.is_used = True
            invite_row.used_at = datetime.utcnow()
            invite_row.used_by = payload.email.lower().strip()
        elif payload.invite != settings.master_password:
            raise HTTPException(status_code=403, detail="Invalid invite code")

        user_key = secrets.token_urlsafe(32)
        user = User(
            user_key=user_key,
            auth_source="local",
            email=payload.email.lower().strip(),
            password_hash=hash_password(payload.password),
            display_name=payload.name or None,
        )
        db_session.add(user)
        db_session.commit()

    return {"status": "ok", "message": "Account created. Please log in to set up 2FA."}


@app.post("/auth/login")
@limiter.limit("10/minute")
async def login(
    request: Request, payload: LoginRequest, db: Session = Depends(get_db_dependency)
):
    """Log in with email + password + optional TOTP code.

    Returns a session cookie for web access.
    """
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check 2FA if enabled
    if user.totp_enabled:
        if not payload.totp_code:
            raise HTTPException(status_code=401, detail="2FA code required")
        secret = decrypt_totp_secret(user.totp_secret)
        if not verify_totp(secret, payload.totp_code):
            raise HTTPException(status_code=401, detail="Invalid 2FA code")

    user.last_login = datetime.utcnow()  # type: ignore[assignment]
    token = create_user_session(user.id, db)

    response = JSONResponse(
        content={
            "status": "ok",
            "totp_enabled": user.totp_enabled,
            "display_name": user.display_name or user.email,
        }
    )
    response.set_cookie(
        key="user_session",
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return response


@app.post("/auth/setup-2fa")
async def setup_2fa(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Generate a TOTP secret and QR code for authenticator app enrollment.

    Requires an active user session.
    """
    user = _get_user_from_session(request, db)

    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")

    secret = generate_totp_secret()
    provisioning_uri = get_totp_provisioning_uri(secret, user.email or "user")
    qr_data_url = generate_totp_qr_data_url(provisioning_uri)

    # Store encrypted secret (not yet enabled until confirmed)
    user.totp_secret = encrypt_totp_secret(secret)
    db.commit()

    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_data_url": qr_data_url,
    }


@app.post("/auth/confirm-2fa")
@limiter.limit("10/minute")
async def confirm_2fa(
    payload: TOTPConfirmRequest,
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Verify a TOTP code to confirm 2FA enrollment.

    Requires an active user session with a pending TOTP secret.
    """
    user = _get_user_from_session(request, db)

    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Call /auth/setup-2fa first")

    secret = decrypt_totp_secret(user.totp_secret)
    if not verify_totp(secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid 2FA code")

    user.totp_enabled = True
    db.commit()

    return {"status": "ok", "message": "2FA enabled successfully"}


@app.post("/auth/pair/create")
async def create_pair(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Create a pairing session for signing into the app.

    Returns a QR code and short code. The app scans the QR or user types
    the short code to receive their user_key.
    """
    user = _get_user_from_session(request, db)
    result = create_pairing_session(user.id, db)
    return result


@app.get("/auth/pair/{token}/status")
async def pair_status(token: str, db: Session = Depends(get_db_dependency)):
    """Poll this endpoint from the app to check if a pairing session is ready.

    Returns user_key and manifest_url when the session is valid and unclaimed.
    """
    result = claim_pairing_session(db, token=token)
    if not result:
        return {"status": "pending"}
    return {"status": "ready", **result}


@app.get("/auth/pair/code/{short_code}")
async def pair_by_code(short_code: str, db: Session = Depends(get_db_dependency)):
    """Alternative to QR scanning — user types the 6-character code in the app."""
    result = claim_pairing_session(db, short_code=short_code)
    if not result:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    return {"status": "ready", **result}


# ---------------------------------------------------------------------------
# Device pairing (tvOS / device-initiated flow)
# ---------------------------------------------------------------------------


@app.post("/auth/device/code")
@limiter.limit("10/minute")
async def device_code(request: Request, db: Session = Depends(get_db_dependency)):
    """Create a device pairing session (no auth required).

    The device (e.g. Apple TV) calls this to get a short code to display
    on screen. The user then enters the code on the web or mobile app
    to link their account.

    Returns device_token (for polling), short_code (to display), and expires_at.
    """
    result = create_device_pairing_session(db)
    return result


@app.get("/auth/device/{device_token}/status")
@limiter.limit("30/minute")
async def device_status(
    request: Request,
    device_token: str,
    db: Session = Depends(get_db_dependency),
):
    """Poll this from the device to check if a user has claimed the code.

    Returns {"status": "pending"} while waiting, or
    {"status": "ready", "user_key": "...", "manifest_url": "..."} once claimed.
    """
    result = poll_device_pairing_session(db, device_token)
    if not result:
        return {"status": "pending"}
    return {"status": "ready", **result}


@app.post("/auth/device/claim")
@limiter.limit("10/minute")
async def device_claim(
    request: Request,
    payload: DeviceClaimRequest,
    db: Session = Depends(get_db_dependency),
):
    """Authenticated user claims a device pairing code.

    The user enters the short code displayed on the TV into the web/mobile app.
    Requires a valid user_session cookie.
    """
    user = _get_user_from_session(request, db)
    success = claim_device_pairing_session(db, payload.short_code, user)
    if not success:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    return {"status": "ok", "message": "Device paired successfully"}


@app.post("/auth/logout")
async def user_logout(
    request: Request,
    db: Session = Depends(get_db_dependency),
):
    """Log out by clearing the user session."""
    token = request.cookies.get("user_session")
    if token:
        session = db.query(UserSession).filter(UserSession.token == token).first()
        if session:
            db.delete(session)
            db.commit()
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("user_session")
    return response


@app.post("/{user_key}/scrobble")
async def scrobble(
    user_key: str,
    payload: ScrobbleRequest,
):
    """Record a completed watch event from the custom Stremio client.

    Only ``action='complete'`` events are accepted (client sends this when
    <= 15 minutes of content remain).  Duplicate events for the same item
    within 60 seconds are silently ignored.
    """
    with get_db() as db:
        user = db.query(User).filter(User.user_key == user_key).first()
        if not user:
            raise HTTPException(status_code=404, detail="Not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User is inactive")

        # Deduplicate: ignore if same item completed within 60 seconds
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        existing = (
            db.query(WatchEvent.id)
            .filter(
                WatchEvent.user_id == user.id,
                WatchEvent.tmdb_id == payload.tmdb_id,
                WatchEvent.media_type == payload.media_type,
                WatchEvent.action == "complete",
                WatchEvent.created_at >= cutoff,
            )
            .first()
        )
        if existing:
            return {"status": "ok", "deduplicated": True}

        event = WatchEvent(
            user_id=user.id,
            tmdb_id=payload.tmdb_id,
            media_type=payload.media_type,
            season=payload.season,
            episode=payload.episode,
            action="complete",
            title=payload.title,
        )
        db.add(event)
        db.commit()

        # Trigger debounced catalog rebuild + metadata backfill
        _schedule_local_sync(user.id)

    return {"status": "ok"}


@app.get("/auth/verify-invite")
async def verify_invite(
    invite: str = Query(..., description="Invite code to verify"),
):
    """Verify an invite code is valid without consuming it.

    Returns the install token so the client can build the manifest URL.
    """
    with get_db() as db_session:
        inv = (
            db_session.query(InviteCode)
            .filter(InviteCode.code == invite, InviteCode.is_used.is_(False))
            .first()
        )
        if inv:
            if inv.expires_at and datetime.utcnow() > inv.expires_at:
                raise HTTPException(status_code=403, detail="Invite code has expired")
            return {"status": "valid", "install_token": get_install_token()}
        elif invite == settings.master_password:
            return {"status": "valid", "install_token": get_install_token()}
        else:
            raise HTTPException(status_code=403, detail="Invalid invite code")


# OAuth endpoints for Trakt authentication
@app.get("/auth/start")
async def start_auth(
    invite: str = Query(None, description="One-time invite code"),
    password: str = Query(None, description="Master password (legacy)"),
    db: Session = Depends(get_db_dependency),
):
    """
    Start Trakt OAuth flow.

    Accepts either a one-time invite code (preferred) or the master password
    (legacy fallback). Invite codes are consumed on successful Trakt callback.
    """
    code_value = invite or password
    if not code_value:
        raise HTTPException(status_code=403, detail="Invite code required")

    # Check if it's a valid invite code first
    invite_row = None
    with get_db() as db_session:
        invite_row = (
            db_session.query(InviteCode)
            .filter(InviteCode.code == code_value, InviteCode.is_used.is_(False))
            .first()
        )
        if invite_row:
            # Check expiry
            if invite_row.expires_at and datetime.utcnow() > invite_row.expires_at:
                raise HTTPException(status_code=403, detail="Invite code has expired")
        elif code_value != settings.master_password:
            # Not a valid invite code and not the master password
            raise HTTPException(status_code=403, detail="Invalid invite code")

    # Generate state for CSRF protection and persist it
    state = secrets.token_urlsafe(32)
    with get_db() as db_session:
        # Clean up expired states (older than 10 minutes)
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        db_session.query(OAuthState).filter(OAuthState.created_at < cutoff).delete()
        # Store invite code in state so we can mark it used on callback
        db_session.add(OAuthState(state=state))

    # Store the invite code in a temporary mapping so callback can mark it used
    _pending_invite_codes[state] = code_value if invite_row else None

    # Get Trakt authorization URL
    auth_url = trakt_client.get_authorization_url(state)

    return RedirectResponse(url=auth_url)


# Temporary mapping: OAuth state -> invite code (so callback can mark it used)
_pending_invite_codes: dict[str, str | None] = {}


@app.get("/api/auth/trakt/callback")
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
            content=auth_error_html(
                "Invalid or expired OAuth state. Please try again."
            ),
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

    # Retrieve pending invite code before consuming the state
    pending_invite = _pending_invite_codes.pop(state, None)

    # Consume the state token (one-time use)
    db.query(OAuthState).filter(OAuthState.state == state).delete()
    db.commit()

    try:
        # Exchange code for token
        try:
            token_data = await trakt_client.exchange_code_for_token(code)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error(f"Trakt token exchange failed ({status}): {e.response.text}")
            if status == 401:
                detail = "Trakt rejected the credentials. Check TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET."
            elif status == 403:
                detail = "Trakt denied access. The redirect URI may not match the Trakt app settings."
            else:
                detail = f"Trakt returned HTTP {status} during token exchange."
            return HTMLResponse(content=auth_error_html(detail), status_code=502)
        except Exception as e:
            logger.error(f"Token exchange network error: {e}")
            return HTMLResponse(
                content=auth_error_html(
                    "Could not reach Trakt servers. Please try again in a moment."
                ),
                status_code=502,
            )

        # Get user profile
        try:
            profile = await trakt_client.get_user_profile(token_data["access_token"])
        except Exception as e:
            logger.error(f"Failed to fetch Trakt profile: {e}")
            return HTMLResponse(
                content=auth_error_html(
                    "Authenticated with Trakt but could not fetch your profile. Please try again."
                ),
                status_code=502,
            )

        # Encrypt tokens before storage
        encrypted_access = encrypt_token(token_data["access_token"])
        encrypted_refresh = encrypt_token(token_data["refresh_token"])
        expires_at = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])

        trakt_username = profile.get("username", "")

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
                trakt_username=trakt_username,
                trakt_access_token=encrypted_access,
                trakt_refresh_token=encrypted_refresh,
                trakt_token_expires_at=expires_at,
            )
            db.add(user)

        db.commit()

        # Mark invite code as used (if one was used)
        if pending_invite:
            with get_db() as inv_db:
                inv = (
                    inv_db.query(InviteCode)
                    .filter(InviteCode.code == pending_invite)
                    .first()
                )
                if inv:
                    inv.is_used = True
                    inv.used_at = datetime.utcnow()
                    inv.used_by = trakt_username
                    inv_db.commit()

        # Trigger background Trakt sync to build personalized catalogs
        _schedule_user_sync(user.id, token_data["access_token"])

        # Return success page
        manifest_url = f"{settings.base_url}/{user.user_key}/manifest.json"
        username = user.trakt_username or user.trakt_user_id

        return HTMLResponse(
            content=auth_success_html(username, manifest_url, user.user_key)
        )

    except Exception as e:
        logger.error(f"OAuth callback failed: {type(e).__name__}: {e}")
        return HTMLResponse(
            content=auth_error_html(
                f"Could not complete Trakt authentication: {type(e).__name__}: {e}"
            ),
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Stream proxy endpoint (AIOStreams)
# ---------------------------------------------------------------------------


@app.get("/{user_key}/stream/{stremio_type}/{video_id}.json")
async def stream_handler(
    user_key: str,
    stremio_type: str,
    video_id: str,
    db: Session = Depends(get_db_dependency),
):
    """Proxy stream requests to AIOStreams based on user's bandwidth tier."""
    user = _get_cached_user(user_key, db)
    if not user:
        raise HTTPException(status_code=404, detail="Not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")

    tier = getattr(user, "bandwidth_tier", "high") or "high"
    streams = await get_streams(video_id, stremio_type, tier, db)
    return JSONResponse(
        content={"streams": streams},
        headers={"Cache-Control": "public, max-age=300"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
