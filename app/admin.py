"""
Admin portal for Stremio AI Addon.

Provides a web dashboard to manage settings, trigger builds, and monitor status.
Protected by master password authentication.
"""

import asyncio
import collections
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import func

from app.config import settings
from app.database import get_db
from app.models import (
    AdminSetting,
    Tag,
    MovieTag,
    MediaMetadata,
    UniversalCategory,
    UniversalCatalogContent,
    User,
    TaggingJob,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# In-memory session store and active build task
_admin_sessions: dict[str, datetime] = {}
_active_build_task: Optional[asyncio.Task] = None
SESSION_DURATION = timedelta(hours=24)

# ---- Build log ring buffer ----
_build_logs: collections.deque[str] = collections.deque(maxlen=2000)
_build_log_sink_id: Optional[int] = None


def _start_log_capture():
    """Add a loguru sink that captures build-related log lines."""
    global _build_log_sink_id
    if _build_log_sink_id is not None:
        return  # already capturing

    _build_logs.clear()

    def _sink(message):
        _build_logs.append(str(message).rstrip())

    _build_log_sink_id = logger.add(
        _sink,
        format="{time:HH:mm:ss} | {level:<7} | {message}",
        level="DEBUG",
        filter=lambda record: any(
            kw in record["message"]
            for kw in (
                "Fetch",
                "fetch",
                "Tag",
                "tag",
                "Progress",
                "Stor",
                "stor",
                "Generat",
                "generat",
                "Regenerat",
                "regenerat",
                "Dedup",
                "dedup",
                "Skip",
                "skip",
                "BUILD",
                "build",
                "DAILY",
                "Daily",
                "daily",
                "catalog",
                "Catalog",
                "COMPLETE",
                "complete",
                "Failed",
                "failed",
                "Error",
                "error",
                "items",
                "batch",
                "Batch",
                "TMDB",
                "Gemini",
                "gemini",
                "Step",
                "====",
                "Target",
                "Estimated",
                "Duration",
                "remaining",
                "processed",
                "succeeded",
            )
        ),
    )


def _stop_log_capture():
    """Remove the loguru sink."""
    global _build_log_sink_id
    if _build_log_sink_id is not None:
        logger.remove(_build_log_sink_id)
        _build_log_sink_id = None


# ---- Authentication ----


def verify_admin(request: Request):
    """Verify admin authentication via cookie."""
    token = request.cookies.get("admin_token")
    if not token or token not in _admin_sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if datetime.utcnow() > _admin_sessions[token]:
        del _admin_sessions[token]
        raise HTTPException(status_code=401, detail="Session expired")
    return True


# ---- Settings helpers ----


def _apply_setting(key: str, value: str):
    """Apply a setting to the in-memory config object."""
    try:
        attr = key.lower()
        if hasattr(settings, attr):
            current = getattr(settings, attr)
            if isinstance(current, bool):
                object.__setattr__(
                    settings, attr, value.lower() in ("true", "1", "yes")
                )
            elif isinstance(current, int):
                object.__setattr__(settings, attr, int(value))
            elif isinstance(current, float):
                object.__setattr__(settings, attr, float(value))
            else:
                object.__setattr__(settings, attr, value)
    except Exception as e:
        logger.warning(f"Failed to hot-reload setting {key}: {e}")


def load_settings_from_db():
    """Load admin settings from database and apply to in-memory config."""
    try:
        with get_db() as db:
            for setting in db.query(AdminSetting).all():
                _apply_setting(setting.key, setting.value)
            count = db.query(func.count(AdminSetting.key)).scalar() or 0
            if count:
                logger.info(f"Loaded {count} admin settings from database")
    except Exception as e:
        logger.warning(f"Could not load admin settings from DB: {e}")


def _mask(value: str) -> str:
    """Mask a sensitive value, showing only first/last 4 chars."""
    if not value:
        return ""
    if len(value) <= 8:
        return "********"
    return value[:4] + "********" + value[-4:]


# ---- API Routes ----


@router.post("/api/login")
async def admin_login(request: Request):
    """Authenticate with master password."""
    body = await request.json()
    password = body.get("password", "")

    if password != settings.master_password:
        raise HTTPException(status_code=401, detail="Invalid password")

    token = secrets.token_hex(32)
    _admin_sessions[token] = datetime.utcnow() + SESSION_DURATION

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        max_age=int(SESSION_DURATION.total_seconds()),
        samesite="lax",
    )
    return response


@router.post("/api/logout")
async def admin_logout(request: Request):
    """Clear admin session."""
    token = request.cookies.get("admin_token")
    if token in _admin_sessions:
        del _admin_sessions[token]
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("admin_token")
    return response


@router.get("/api/stats")
async def get_stats(request: Request, _=Depends(verify_admin)):
    """Get database statistics and recent job history."""
    with get_db() as db:
        stats = {
            "movies_tagged": db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == "movie")
            .scalar()
            or 0,
            "shows_tagged": db.query(func.count(func.distinct(MovieTag.tmdb_id)))
            .filter(MovieTag.media_type == "tv")
            .scalar()
            or 0,
            "total_tags": db.query(func.count(Tag.id)).scalar() or 0,
            "total_metadata": db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0,
            "active_categories": db.query(func.count(UniversalCategory.id))
            .filter(UniversalCategory.is_active.is_(True))
            .scalar()
            or 0,
            "catalog_items": db.query(
                func.count(UniversalCatalogContent.tmdb_id)
            ).scalar()
            or 0,
            "total_users": db.query(func.count(User.id)).scalar() or 0,
        }

        jobs = (
            db.query(TaggingJob).order_by(TaggingJob.started_at.desc()).limit(20).all()
        )
        stats["recent_jobs"] = [
            {
                "id": j.id,
                "job_type": j.job_type,
                "status": j.status,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "items_processed": j.items_processed,
                "items_failed": j.items_failed,
                "error_message": (j.error_message or "")[:200],
            }
            for j in jobs
        ]

        running_job = (
            db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        )
        stats["build_running"] = running_job is not None

    return stats


@router.get("/api/settings")
async def get_settings(request: Request, _=Depends(verify_admin)):
    """Get current settings with sensitive values masked."""
    with get_db() as db:
        overrides = {s.key: s.value for s in db.query(AdminSetting).all()}

    def val(key: str):
        return overrides.get(key, str(getattr(settings, key.lower(), "")))

    return {
        "api_keys": {
            "TMDB_API_KEY": _mask(val("TMDB_API_KEY")),
            "GEMINI_API_KEY": _mask(val("GEMINI_API_KEY")),
            "TRAKT_CLIENT_ID": _mask(val("TRAKT_CLIENT_ID")),
            "TRAKT_CLIENT_SECRET": _mask(val("TRAKT_CLIENT_SECRET")),
        },
        "app": {
            "ADDON_NAME": val("ADDON_NAME"),
            "BASE_URL": val("BASE_URL"),
            "CATALOG_SIZE": int(val("CATALOG_SIZE")),
            "GEMINI_MODEL": val("GEMINI_MODEL"),
        },
        "features": {
            "ENABLE_UNIVERSAL_CATALOGS": val("ENABLE_UNIVERSAL_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_PERSONALIZED_CATALOGS": val("ENABLE_PERSONALIZED_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_TRAKT_SYNC": val("ENABLE_TRAKT_SYNC").lower() in ("true", "1"),
        },
        "schedule": {
            "DAILY_UPDATE_ENABLED": val("DAILY_UPDATE_ENABLED").lower()
            in ("true", "1"),
            "DAILY_UPDATE_TIME": val("DAILY_UPDATE_TIME"),
        },
    }


@router.post("/api/settings")
async def update_settings(request: Request, _=Depends(verify_admin)):
    """Update settings. Only non-empty values are saved."""
    body = await request.json()

    ALLOWED = {
        "TMDB_API_KEY",
        "GEMINI_API_KEY",
        "TRAKT_CLIENT_ID",
        "TRAKT_CLIENT_SECRET",
        "ADDON_NAME",
        "BASE_URL",
        "CATALOG_SIZE",
        "GEMINI_MODEL",
        "MASTER_PASSWORD",
        "ENABLE_UNIVERSAL_CATALOGS",
        "ENABLE_PERSONALIZED_CATALOGS",
        "ENABLE_TRAKT_SYNC",
        "DAILY_UPDATE_ENABLED",
        "DAILY_UPDATE_TIME",
    }

    updated = []
    with get_db() as db:
        for key, value in body.items():
            if key not in ALLOWED:
                continue
            str_val = str(value)
            if not str_val:
                continue

            existing = db.query(AdminSetting).filter(AdminSetting.key == key).first()
            if existing:
                existing.value = str_val
                existing.updated_at = datetime.utcnow()
            else:
                db.add(
                    AdminSetting(key=key, value=str_val, updated_at=datetime.utcnow())
                )

            _apply_setting(key, str_val)
            updated.append(key)

        db.commit()

    # Restart scheduler if schedule settings changed
    schedule_keys = {"DAILY_UPDATE_ENABLED", "DAILY_UPDATE_TIME"}
    if schedule_keys & set(updated):
        await _restart_scheduler()

    return {"status": "ok", "updated": updated}


async def _restart_scheduler():
    """Restart the daily update scheduler with current settings."""
    from app.main import app

    # Cancel existing scheduler
    if getattr(app.state, "scheduler_task", None) is not None:
        app.state.scheduler_task.cancel()
        try:
            await app.state.scheduler_task
        except asyncio.CancelledError:
            pass
        app.state.scheduler_task = None
        logger.info("Scheduler stopped for reconfiguration")

    # Start new scheduler if enabled
    if settings.daily_update_enabled:
        from app.scheduler import run_scheduler

        app.state.scheduler_task = asyncio.create_task(run_scheduler())
        logger.info(f"Scheduler restarted: daily at {settings.daily_update_time} UTC")


@router.post("/api/build/start")
async def start_build(request: Request, _=Depends(verify_admin)):
    """Start the initial database build in the background."""
    global _active_build_task

    body = await request.json()
    movies = body.get("movies", 100000)
    shows = body.get("shows", 50000)

    # Check for running builds
    with get_db() as db:
        running = db.query(TaggingJob).filter(TaggingJob.status == "running").first()
        if running:
            raise HTTPException(409, "A build is already running")

    if _active_build_task and not _active_build_task.done():
        raise HTTPException(409, "A build task is already active")

    _start_log_capture()

    async def _run():
        try:
            from workers.initial_build import main as build_main

            await build_main(movies, shows)
        except asyncio.CancelledError:
            logger.warning("Build task was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Build task failed: {e}")
        finally:
            _stop_log_capture()

    _active_build_task = asyncio.create_task(_run())
    logger.info(f"Initial build started: {movies} movies, {shows} shows")
    return {"status": "started", "movies": movies, "shows": shows}


@router.post("/api/build/daily")
async def trigger_daily_update(request: Request, _=Depends(verify_admin)):
    """Manually trigger a daily update."""
    global _active_build_task

    if _active_build_task and not _active_build_task.done():
        raise HTTPException(409, "A build task is already active")

    _start_log_capture()

    async def _run():
        try:
            from workers.daily_update import run_daily_update

            await run_daily_update()
        except asyncio.CancelledError:
            logger.warning("Daily update was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Daily update task failed: {e}")
        finally:
            _stop_log_capture()

    _active_build_task = asyncio.create_task(_run())
    logger.info("Manual daily update triggered")
    return {"status": "started"}


@router.get("/api/build/status")
async def get_build_status(request: Request, _=Depends(verify_admin)):
    """Get current build progress."""
    with get_db() as db:
        running_job = (
            db.query(TaggingJob)
            .filter(TaggingJob.status == "running")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )

        if running_job:
            movies_done = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "movie")
                .scalar()
                or 0
            )
            shows_done = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "tv")
                .scalar()
                or 0
            )
            elapsed = (datetime.utcnow() - running_job.started_at).total_seconds()

            return {
                "running": True,
                "job_type": running_job.job_type,
                "started_at": running_job.started_at.isoformat(),
                "elapsed_seconds": int(elapsed),
                "movies_tagged": movies_done,
                "shows_tagged": shows_done,
            }

    task_running = _active_build_task is not None and not _active_build_task.done()
    return {"running": task_running}


def _mark_running_jobs_cancelled():
    """Mark any running TaggingJob rows as cancelled."""
    try:
        with get_db() as db:
            for job in db.query(TaggingJob).filter(TaggingJob.status == "running"):
                job.status = "cancelled"
                job.error_message = "Cancelled by user"
                job.completed_at = datetime.utcnow()
            db.commit()
    except Exception as exc:
        logger.error(f"Failed to mark jobs cancelled: {exc}")


@router.post("/api/build/stop")
async def stop_build(request: Request, _=Depends(verify_admin)):
    """Cancel the currently running build task.

    Handles two scenarios:
    1. Normal: asyncio task is alive -> cancel it
    2. Orphaned: DB says running but task ref is gone (e.g. after server restart
       or code redeploy) -> just mark DB jobs as cancelled
    """
    global _active_build_task

    has_task = _active_build_task is not None and not _active_build_task.done()

    # Check DB for running jobs even if task ref is gone
    with get_db() as db:
        has_db_job = (
            db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            is not None
        )

    if not has_task and not has_db_job:
        raise HTTPException(404, "No build is currently running")

    # Cancel the asyncio task if it exists
    if has_task:
        _active_build_task.cancel()
        try:
            await asyncio.wait_for(_active_build_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Mark DB jobs as cancelled (safety net for both scenarios)
    _mark_running_jobs_cancelled()
    _stop_log_capture()
    _active_build_task = None

    logger.info("Build stopped by user")
    return {"status": "stopped"}


@router.get("/api/build/logs")
async def get_build_logs(
    request: Request,
    _=Depends(verify_admin),
    after: int = 0,
):
    """
    Return captured build log lines.

    Query param 'after' returns only lines after that index,
    so the UI can poll incrementally without re-fetching everything.
    Auto-starts log capture if a build is running but capture isn't active
    (e.g. build started before code was deployed).
    """
    # Auto-start capture for builds that started before this code was deployed
    if _build_log_sink_id is None:
        with get_db() as db:
            running = (
                db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            )
        if running:
            _start_log_capture()

    logs = list(_build_logs)
    total = len(logs)

    if after > 0:
        logs = logs[after:]

    return {"lines": logs, "total": total}


@router.get("/api/debug")
async def debug_catalogs(request: Request, _=Depends(verify_admin)):
    """Full diagnostic of the catalog pipeline to identify issues."""
    from app.catalog_generator import CatalogGenerator

    try:
        with get_db() as db:
            # Layer 1: Tags
            tag_count = db.query(func.count(Tag.id)).scalar() or 0

            # Layer 2: MovieTags (tagged items)
            movies_tagged = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "movie")
                .scalar()
                or 0
            )
            shows_tagged = (
                db.query(func.count(func.distinct(MovieTag.tmdb_id)))
                .filter(MovieTag.media_type == "tv")
                .scalar()
                or 0
            )

            # Layer 3: Metadata
            metadata_count = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0

            # Layer 4: Universal categories
            categories = (
                db.query(UniversalCategory)
                .filter(UniversalCategory.is_active.is_(True))
                .order_by(UniversalCategory.sort_order)
                .all()
            )

            # Layer 5: Pre-computed catalog content
            total_catalog_items = (
                db.query(func.count(UniversalCatalogContent.tmdb_id)).scalar() or 0
            )

            # Per-category breakdown
            category_details = []
            generator = CatalogGenerator(db)
            for cat in categories:
                try:
                    content_count = (
                        db.query(func.count(UniversalCatalogContent.tmdb_id))
                        .filter(UniversalCatalogContent.category_id == cat.id)
                        .scalar()
                        or 0
                    )
                except Exception:
                    content_count = -1
                # Check how many items would match the formula (live query)
                # Wrapped in try/except because DB contention during active
                # builds can cause this to fail
                try:
                    potential_matches = len(
                        generator.generate_universal_catalog(cat, limit=5)
                    )
                except Exception:
                    potential_matches = -1  # indicates query failed
                category_details.append(
                    {
                        "id": cat.id,
                        "name": cat.name,
                        "media_type": cat.media_type,
                        "tag_formula": cat.tag_formula,
                        "pre_computed_items": content_count,
                        "potential_matches_sample": potential_matches,
                    }
                )

            # Layer 6: Last tagging job status
            last_job = (
                db.query(TaggingJob)
                .order_by(TaggingJob.started_at.desc())
                .first()
            )

            # Build diagnosis
            issues = []
            if tag_count == 0:
                issues.append("No tags exist. Run initial build to create tags.")
            if movies_tagged == 0 and shows_tagged == 0:
                issues.append("No items have been tagged yet.")
            if metadata_count == 0:
                issues.append(
                    "No metadata cached. Items won't show posters/titles in Stremio."
                )
            if len(categories) == 0:
                issues.append("No active universal categories found.")
            if total_catalog_items == 0 and (movies_tagged > 0 or shows_tagged > 0):
                issues.append(
                    "CRITICAL: Items are tagged but catalog content table is empty. "
                    "The catalog generation step likely didn't run. "
                    "Use 'Regenerate Catalogs' to fix this."
                )

        return {
            "pipeline": {
                "tags": tag_count,
                "movies_tagged": movies_tagged,
                "shows_tagged": shows_tagged,
                "metadata_cached": metadata_count,
                "active_categories": len(categories),
                "pre_computed_catalog_items": total_catalog_items,
            },
            "categories": category_details,
            "last_job": {
                "type": last_job.job_type if last_job else None,
                "status": last_job.status if last_job else None,
                "error": (last_job.error_message or "")[:500] if last_job else None,
            },
            "issues": issues,
            "healthy": len(issues) == 0,
        }
    except Exception as e:
        logger.error(f"Diagnostic endpoint failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Diagnostic failed: {type(e).__name__}: {e}",
        )


@router.post("/api/catalogs/regenerate")
async def regenerate_catalogs(request: Request, _=Depends(verify_admin)):
    """Regenerate all universal catalogs from existing tags (no re-tagging needed)."""
    from app.catalog_generator import CatalogGenerator

    with get_db() as db:
        # Sanity check: are there tagged items?
        tagged_count = (
            db.query(func.count(func.distinct(MovieTag.tmdb_id))).scalar() or 0
        )
        if tagged_count == 0:
            raise HTTPException(
                400,
                "No tagged items found. Run a build first to tag movies/shows.",
            )

        generator = CatalogGenerator(db)
        generator.regenerate_all_universal_catalogs()

        new_total = (
            db.query(func.count(UniversalCatalogContent.tmdb_id)).scalar() or 0
        )

    logger.info(f"Catalog regeneration complete: {new_total} total items")
    return {"status": "ok", "total_catalog_items": new_total}


# ---- HTML Page ----


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin dashboard."""
    return HTMLResponse(content=_admin_html())


def _admin_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin - Stremio AI</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
a{color:#58a6ff;text-decoration:none}

/* Login */
#login-screen{display:flex;align-items:center;justify-content:center;min-height:100vh;background:linear-gradient(135deg,#0d1117 0%,#161b22 100%)}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:48px;width:100%;max-width:400px;text-align:center}
.login-box h1{font-size:24px;margin-bottom:8px;color:#e6edf3}
.login-box p{color:#8b949e;margin-bottom:32px;font-size:14px}
.login-box input{width:100%;padding:12px 16px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:16px;margin-bottom:16px;outline:none;transition:border-color .2s}
.login-box input:focus{border-color:#e50914}
.login-box button{width:100%;padding:12px;background:#e50914;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;transition:background .2s}
.login-box button:hover{background:#c40812}
.login-error{color:#f85149;margin-top:12px;font-size:14px;min-height:20px}

/* Dashboard layout */
#dashboard{display:none;min-height:100vh}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:16px 32px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;font-weight:600}
.topbar h1 span{color:#e50914}
.topbar-actions{display:flex;gap:12px;align-items:center}
.topbar-actions button{padding:6px 16px;background:transparent;border:1px solid #30363d;border-radius:6px;color:#8b949e;font-size:13px;cursor:pointer;transition:all .2s}
.topbar-actions button:hover{color:#e6edf3;border-color:#8b949e}
.tab-nav{display:flex;gap:0;background:#161b22;border-bottom:1px solid #30363d;padding:0 32px}
.tab-btn{padding:12px 20px;background:none;border:none;border-bottom:2px solid transparent;color:#8b949e;font-size:14px;font-weight:500;cursor:pointer;transition:all .2s}
.tab-btn:hover{color:#e6edf3}
.tab-btn.active{color:#e6edf3;border-bottom-color:#e50914}
.main{padding:32px;max-width:1200px;margin:0 auto}

/* Tabs */
.tab-panel{display:none}
.tab-panel.active{display:block}

/* Stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:32px}
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px}
.stat-value{font-size:32px;font-weight:700;color:#e6edf3;margin-bottom:4px}
.stat-label{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}

/* Cards */
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px;margin-bottom:24px}
.card h3{font-size:16px;font-weight:600;margin-bottom:16px;color:#e6edf3;display:flex;align-items:center;gap:8px}
.card-row{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:768px){.card-row{grid-template-columns:1fr}}

/* Forms */
.form-group{margin-bottom:20px}
.form-group label{display:block;font-size:13px;color:#8b949e;margin-bottom:6px;font-weight:500}
.form-group input[type="text"],.form-group input[type="password"],.form-group input[type="number"],.form-group input[type="time"],.form-group select{width:100%;padding:10px 14px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:14px;outline:none;transition:border-color .2s}
.form-group input:focus,.form-group select:focus{border-color:#58a6ff}
.form-group .hint{font-size:12px;color:#8b949e;margin-top:4px}
.form-group .current{font-size:12px;color:#58a6ff;margin-bottom:4px}

/* Toggle */
.toggle{display:flex;align-items:center;gap:12px;cursor:pointer}
.toggle input{display:none}
.toggle-track{width:44px;height:24px;background:#30363d;border-radius:12px;position:relative;transition:background .2s}
.toggle input:checked+.toggle-track{background:#e50914}
.toggle-track::after{content:'';position:absolute;width:20px;height:20px;background:#e6edf3;border-radius:50%;top:2px;left:2px;transition:transform .2s}
.toggle input:checked+.toggle-track::after{transform:translateX(20px)}
.toggle-label{font-size:14px;color:#e6edf3}

/* Buttons */
.btn{padding:10px 20px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;display:inline-flex;align-items:center;gap:8px}
.btn-primary{background:#e50914;color:#fff}
.btn-primary:hover{background:#c40812}
.btn-primary:disabled{background:#30363d;color:#8b949e;cursor:not-allowed}
.btn-secondary{background:#21262d;color:#e6edf3;border:1px solid #30363d}
.btn-secondary:hover{background:#30363d}
.btn-danger{background:#da3633;color:#fff}
.btn-danger:hover{background:#b62324}
.btn-sm{padding:6px 14px;font-size:13px}

/* Tables */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 12px;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #30363d}
td{padding:10px 12px;font-size:14px;border-bottom:1px solid #21262d;color:#e6edf3}
tr:last-child td{border-bottom:none}

/* Status badges */
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}
.badge-running{background:#1f6feb33;color:#58a6ff}
.badge-completed{background:#23863533;color:#3fb950}
.badge-failed{background:#da363333;color:#f85149}

/* Build progress */
.progress-bar{width:100%;height:8px;background:#21262d;border-radius:4px;overflow:hidden;margin:12px 0}
.progress-fill{height:100%;background:linear-gradient(90deg,#e50914,#ff6b6b);border-radius:4px;transition:width .5s ease}

/* Log viewer */
.log-viewer{background:#010409;border:1px solid #30363d;border-radius:8px;padding:12px;font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;font-size:12px;line-height:1.6;color:#8b949e;max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.log-viewer .log-error{color:#f85149}
.log-viewer .log-warning{color:#d29922}
.log-viewer .log-info{color:#8b949e}
.log-viewer .log-progress{color:#3fb950}
.build-info{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.build-stat{text-align:center;padding:16px;background:#0d1117;border-radius:8px}
.build-stat .val{font-size:24px;font-weight:700;color:#e6edf3}
.build-stat .lbl{font-size:12px;color:#8b949e;margin-top:4px}

/* Alerts */
.alert{padding:12px 16px;border-radius:8px;font-size:14px;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.alert-success{background:#23863522;border:1px solid #23863555;color:#3fb950}
.alert-error{background:#da363322;border:1px solid #da363355;color:#f85149}
.alert-info{background:#1f6feb22;border:1px solid #1f6feb55;color:#58a6ff}

/* Spinner */
.spinner{display:inline-block;width:18px;height:18px;border:2px solid #30363d;border-top-color:#e50914;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
#toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:14px;transform:translateY(100px);opacity:0;transition:all .3s;z-index:1000}
#toast.show{transform:translateY(0);opacity:1}
</style>
</head>
<body>

<!-- Login Screen -->
<div id="login-screen">
  <div class="login-box">
    <h1>Stremio AI</h1>
    <p>Admin Portal</p>
    <form id="login-form">
      <input type="password" id="login-password" placeholder="Master Password" autocomplete="current-password" autofocus>
      <button type="submit">Sign In</button>
    </form>
    <div class="login-error" id="login-error"></div>
  </div>
</div>

<!-- Dashboard -->
<div id="dashboard">
  <div class="topbar">
    <h1>Stremio <span>AI</span> Admin</h1>
    <div class="topbar-actions">
      <button onclick="loadAll()">Refresh</button>
      <button onclick="logout()">Sign Out</button>
    </div>
  </div>

  <div class="tab-nav">
    <button class="tab-btn active" data-tab="overview">Overview</button>
    <button class="tab-btn" data-tab="settings">Settings</button>
    <button class="tab-btn" data-tab="build">Build</button>
    <button class="tab-btn" data-tab="schedule">Schedule</button>
  </div>

  <div class="main">

    <!-- OVERVIEW TAB -->
    <div class="tab-panel active" id="tab-overview">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-value" id="stat-movies">-</div>
          <div class="stat-label">Movies Tagged</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="stat-shows">-</div>
          <div class="stat-label">TV Shows Tagged</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="stat-tags">-</div>
          <div class="stat-label">Tags</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="stat-categories">-</div>
          <div class="stat-label">Categories</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="stat-metadata">-</div>
          <div class="stat-label">Metadata Entries</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="stat-users">-</div>
          <div class="stat-label">Users</div>
        </div>
      </div>

      <div class="card">
        <h3>Recent Jobs</h3>
        <table>
          <thead><tr><th>Type</th><th>Status</th><th>Started</th><th>Processed</th><th>Failed</th></tr></thead>
          <tbody id="jobs-tbody"><tr><td colspan="5" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- SETTINGS TAB -->
    <div class="tab-panel" id="tab-settings">
      <div id="settings-alert"></div>

      <div class="card">
        <h3>API Keys</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">Leave blank to keep current value. Only non-empty values will be saved.</p>
        <div class="card-row">
          <div class="form-group">
            <label>TMDB API Key</label>
            <div class="current" id="cur-tmdb"></div>
            <input type="password" id="set-TMDB_API_KEY" placeholder="Enter new key to update">
          </div>
          <div class="form-group">
            <label>Gemini API Key</label>
            <div class="current" id="cur-gemini"></div>
            <input type="password" id="set-GEMINI_API_KEY" placeholder="Enter new key to update">
          </div>
        </div>
        <div class="card-row">
          <div class="form-group">
            <label>Trakt Client ID</label>
            <div class="current" id="cur-trakt-id"></div>
            <input type="password" id="set-TRAKT_CLIENT_ID" placeholder="Enter new value to update">
          </div>
          <div class="form-group">
            <label>Trakt Client Secret</label>
            <div class="current" id="cur-trakt-secret"></div>
            <input type="password" id="set-TRAKT_CLIENT_SECRET" placeholder="Enter new value to update">
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Application</h3>
        <div class="card-row">
          <div class="form-group">
            <label>Addon Name</label>
            <input type="text" id="set-ADDON_NAME" placeholder="AI Recommendations">
          </div>
          <div class="form-group">
            <label>Base URL</label>
            <input type="text" id="set-BASE_URL" placeholder="https://your-domain.com">
          </div>
        </div>
        <div class="card-row">
          <div class="form-group">
            <label>Catalog Size (items per catalog)</label>
            <input type="number" id="set-CATALOG_SIZE" min="10" max="500" placeholder="100">
          </div>
          <div class="form-group">
            <label>Gemini Model</label>
            <input type="text" id="set-GEMINI_MODEL" placeholder="gemini-2.0-flash">
          </div>
        </div>
        <div class="form-group">
          <label>New Master Password</label>
          <input type="password" id="set-MASTER_PASSWORD" placeholder="Leave blank to keep current">
          <div class="hint">Changing this will require you to log in again with the new password.</div>
        </div>
      </div>

      <div class="card">
        <h3>Features</h3>
        <div style="display:flex;flex-direction:column;gap:20px">
          <label class="toggle">
            <input type="checkbox" id="set-ENABLE_UNIVERSAL_CATALOGS">
            <span class="toggle-track"></span>
            <span class="toggle-label">Universal Catalogs</span>
          </label>
          <label class="toggle">
            <input type="checkbox" id="set-ENABLE_PERSONALIZED_CATALOGS">
            <span class="toggle-track"></span>
            <span class="toggle-label">Personalized Catalogs</span>
          </label>
          <label class="toggle">
            <input type="checkbox" id="set-ENABLE_TRAKT_SYNC">
            <span class="toggle-track"></span>
            <span class="toggle-label">Trakt Sync</span>
          </label>
        </div>
      </div>

      <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
    </div>

    <!-- BUILD TAB -->
    <div class="tab-panel" id="tab-build">
      <div id="build-alert"></div>

      <div class="card" id="build-status-card">
        <h3>Build Status</h3>
        <div id="build-status-content">
          <p style="color:#8b949e">No build currently running.</p>
        </div>
        <div id="build-stop-row" style="display:none;margin-top:12px">
          <button class="btn btn-danger btn-sm" id="btn-stop-build" onclick="stopBuild()">Stop Build</button>
        </div>
      </div>

      <div class="card" id="build-log-card" style="display:none">
        <h3>Build Log <span style="font-weight:400;font-size:12px;color:#8b949e" id="log-count"></span></h3>
        <div class="log-viewer" id="log-viewer"></div>
      </div>

      <div class="card" id="debug-card">
        <h3>Catalog Pipeline Diagnostic</h3>
        <div id="debug-content"><p style="color:#8b949e">Click to run diagnostic...</p></div>
        <div style="margin-top:16px;display:flex;gap:12px">
          <button class="btn btn-secondary" onclick="runDebug()">Run Diagnostic</button>
          <button class="btn btn-primary" id="btn-regenerate" onclick="regenerateCatalogs()">Regenerate Catalogs</button>
        </div>
      </div>

      <div class="card-row">
        <div class="card">
          <h3>Initial Build</h3>
          <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
            One-time job to tag movies and TV shows. Requires Gemini paid tier for large batches.
          </p>
          <div class="card-row">
            <div class="form-group">
              <label>Movies to fetch</label>
              <input type="number" id="build-movies" value="100000" min="100" step="100">
            </div>
            <div class="form-group">
              <label>TV Shows to fetch</label>
              <input type="number" id="build-shows" value="50000" min="100" step="100">
            </div>
          </div>
          <button class="btn btn-primary" id="btn-start-build" onclick="startBuild()">Start Initial Build</button>
        </div>

        <div class="card">
          <h3>Daily Update</h3>
          <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
            Fetches new releases from this week and tags them. Uses free Gemini tier.
          </p>
          <button class="btn btn-secondary" id="btn-daily-update" onclick="triggerDailyUpdate()">Run Daily Update Now</button>
        </div>
      </div>

      <div class="card">
        <h3>Job History</h3>
        <table>
          <thead><tr><th>ID</th><th>Type</th><th>Status</th><th>Started</th><th>Completed</th><th>Processed</th><th>Failed</th></tr></thead>
          <tbody id="build-jobs-tbody"><tr><td colspan="7" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- SCHEDULE TAB -->
    <div class="tab-panel" id="tab-schedule">
      <div id="schedule-alert"></div>

      <div class="card">
        <h3>Daily Update Schedule</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          Automatically fetch and tag new releases at a scheduled time each day.
        </p>

        <label class="toggle" style="margin-bottom:24px">
          <input type="checkbox" id="schedule-enabled">
          <span class="toggle-track"></span>
          <span class="toggle-label">Enable Daily Updates</span>
        </label>

        <div class="form-group" style="max-width:200px">
          <label>Update Time (UTC)</label>
          <input type="time" id="schedule-time" value="03:00">
        </div>

        <button class="btn btn-primary" onclick="saveSchedule()">Save Schedule</button>
      </div>
    </div>

  </div>
</div>

<div id="toast"></div>

<script>
// ---- Helpers ----
async function api(method, url, data) {
  const opts = { method, headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' };
  if (data) opts.body = JSON.stringify(data);
  const res = await fetch(url, opts);
  if (res.status === 401) {
    document.getElementById('dashboard').style.display = 'none';
    document.getElementById('login-screen').style.display = 'flex';
    throw new Error('auth');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.borderColor = type === 'error' ? '#da3633' : type === 'success' ? '#238636' : '#30363d';
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

function fmtNum(n) { return (n || 0).toLocaleString(); }

function fmtDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso + 'Z');
  return d.toLocaleString();
}

function fmtDuration(secs) {
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs/60) + 'm ' + (secs%60) + 's';
  return Math.floor(secs/3600) + 'h ' + Math.floor((secs%3600)/60) + 'm';
}

function badge(status) {
  const cls = status === 'running' ? 'badge-running' : status === 'completed' ? 'badge-completed' : 'badge-failed';
  const label = status === 'cancelled' ? 'cancelled' : status;
  return '<span class="badge ' + cls + '">' + label + '</span>';
}

// ---- Tab Navigation ----
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ---- Auth ----
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pw = document.getElementById('login-password').value;
  try {
    await api('POST', '/admin/api/login', { password: pw });
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    document.getElementById('login-error').textContent = '';
    loadAll();
  } catch (err) {
    document.getElementById('login-error').textContent = 'Invalid password';
  }
});

async function logout() {
  await fetch('/admin/api/logout', { method: 'POST', credentials: 'same-origin' });
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('login-password').value = '';
}

async function checkAuth() {
  try {
    await api('GET', '/admin/api/stats');
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadAll();
  } catch { /* show login */ }
}

// ---- Data Loading ----
async function loadAll() {
  await Promise.all([loadStats(), loadSettings(), loadBuildStatus()]);
  // If a build is running, start polling logs automatically
  try {
    const s = await api('GET', '/admin/api/build/status');
    if (s.running && !logPollTimer) {
      logOffset = 0;
      document.getElementById('log-viewer').innerHTML = '';
      logPollTimer = setInterval(pollLogs, 3000);
      pollLogs();
    }
  } catch(e) { /* ignore */ }
}

async function loadStats() {
  try {
    const s = await api('GET', '/admin/api/stats');
    document.getElementById('stat-movies').textContent = fmtNum(s.movies_tagged);
    document.getElementById('stat-shows').textContent = fmtNum(s.shows_tagged);
    document.getElementById('stat-tags').textContent = fmtNum(s.total_tags);
    document.getElementById('stat-categories').textContent = fmtNum(s.active_categories);
    document.getElementById('stat-metadata').textContent = fmtNum(s.total_metadata);
    document.getElementById('stat-users').textContent = fmtNum(s.total_users);

    // Jobs tables
    const jobsHtml = (s.recent_jobs || []).map(j =>
      '<tr><td>' + j.job_type + '</td><td>' + badge(j.status) + '</td><td>' +
      fmtDate(j.started_at) + '</td><td>' + fmtNum(j.items_processed) + '</td><td>' +
      fmtNum(j.items_failed) + '</td></tr>'
    ).join('') || '<tr><td colspan="5" style="color:#8b949e">No jobs yet</td></tr>';
    document.getElementById('jobs-tbody').innerHTML = jobsHtml;

    const detailHtml = (s.recent_jobs || []).map(j =>
      '<tr><td>' + j.id + '</td><td>' + j.job_type + '</td><td>' + badge(j.status) +
      '</td><td>' + fmtDate(j.started_at) + '</td><td>' + fmtDate(j.completed_at) +
      '</td><td>' + fmtNum(j.items_processed) + '</td><td>' + fmtNum(j.items_failed) + '</td></tr>'
    ).join('') || '<tr><td colspan="7" style="color:#8b949e">No jobs yet</td></tr>';
    document.getElementById('build-jobs-tbody').innerHTML = detailHtml;
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

async function loadSettings() {
  try {
    const s = await api('GET', '/admin/api/settings');

    // API key current values (masked)
    document.getElementById('cur-tmdb').textContent = 'Current: ' + s.api_keys.TMDB_API_KEY;
    document.getElementById('cur-gemini').textContent = 'Current: ' + s.api_keys.GEMINI_API_KEY;
    document.getElementById('cur-trakt-id').textContent = 'Current: ' + s.api_keys.TRAKT_CLIENT_ID;
    document.getElementById('cur-trakt-secret').textContent = 'Current: ' + s.api_keys.TRAKT_CLIENT_SECRET;

    // App settings
    document.getElementById('set-ADDON_NAME').value = s.app.ADDON_NAME || '';
    document.getElementById('set-BASE_URL').value = s.app.BASE_URL || '';
    document.getElementById('set-CATALOG_SIZE').value = s.app.CATALOG_SIZE || 100;
    document.getElementById('set-GEMINI_MODEL').value = s.app.GEMINI_MODEL || '';

    // Features
    document.getElementById('set-ENABLE_UNIVERSAL_CATALOGS').checked = s.features.ENABLE_UNIVERSAL_CATALOGS;
    document.getElementById('set-ENABLE_PERSONALIZED_CATALOGS').checked = s.features.ENABLE_PERSONALIZED_CATALOGS;
    document.getElementById('set-ENABLE_TRAKT_SYNC').checked = s.features.ENABLE_TRAKT_SYNC;

    // Schedule
    document.getElementById('schedule-enabled').checked = s.schedule.DAILY_UPDATE_ENABLED;
    document.getElementById('schedule-time').value = s.schedule.DAILY_UPDATE_TIME || '03:00';
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

// ---- Save Settings ----
async function saveSettings() {
  const data = {};

  // Only send API keys if user typed something new
  const apiFields = ['TMDB_API_KEY', 'GEMINI_API_KEY', 'TRAKT_CLIENT_ID', 'TRAKT_CLIENT_SECRET', 'MASTER_PASSWORD'];
  apiFields.forEach(k => {
    const v = document.getElementById('set-' + k).value.trim();
    if (v) data[k] = v;
  });

  // Always send text/number fields
  const textFields = ['ADDON_NAME', 'BASE_URL', 'CATALOG_SIZE', 'GEMINI_MODEL'];
  textFields.forEach(k => {
    const v = document.getElementById('set-' + k).value.trim();
    if (v) data[k] = v;
  });

  // Toggle fields
  data.ENABLE_UNIVERSAL_CATALOGS = document.getElementById('set-ENABLE_UNIVERSAL_CATALOGS').checked;
  data.ENABLE_PERSONALIZED_CATALOGS = document.getElementById('set-ENABLE_PERSONALIZED_CATALOGS').checked;
  data.ENABLE_TRAKT_SYNC = document.getElementById('set-ENABLE_TRAKT_SYNC').checked;

  try {
    const res = await api('POST', '/admin/api/settings', data);
    toast('Settings saved (' + (res.updated || []).length + ' updated)', 'success');
    // Clear password fields
    apiFields.forEach(k => { document.getElementById('set-' + k).value = ''; });
    loadSettings();
  } catch (e) {
    toast('Failed to save: ' + e.message, 'error');
  }
}

// ---- Schedule ----
async function saveSchedule() {
  const data = {
    DAILY_UPDATE_ENABLED: document.getElementById('schedule-enabled').checked,
    DAILY_UPDATE_TIME: document.getElementById('schedule-time').value,
  };
  try {
    await api('POST', '/admin/api/settings', data);
    toast('Schedule saved and applied', 'success');
  } catch (e) {
    toast('Failed to save schedule: ' + e.message, 'error');
  }
}

// ---- Build ----
let buildPollTimer = null;
let logPollTimer = null;
let logOffset = 0;

async function startBuild() {
  const movies = parseInt(document.getElementById('build-movies').value) || 100000;
  const shows = parseInt(document.getElementById('build-shows').value) || 50000;

  if (!confirm('Start initial build?\\n\\nMovies: ' + fmtNum(movies) + '\\nTV Shows: ' + fmtNum(shows) +
    '\\n\\nThis may take several hours and will use Gemini API calls.')) return;

  try {
    document.getElementById('btn-start-build').disabled = true;
    await api('POST', '/admin/api/build/start', { movies, shows });
    toast('Build started', 'success');
    startBuildPolling();
  } catch (e) {
    toast('Failed to start build: ' + e.message, 'error');
    document.getElementById('btn-start-build').disabled = false;
  }
}

async function triggerDailyUpdate() {
  if (!confirm('Run daily update now?')) return;
  try {
    document.getElementById('btn-daily-update').disabled = true;
    await api('POST', '/admin/api/build/daily');
    toast('Daily update started', 'success');
    startBuildPolling();
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
    document.getElementById('btn-daily-update').disabled = false;
  }
}

async function stopBuild() {
  if (!confirm('Stop the current build? Progress so far will be preserved.')) return;
  const btn = document.getElementById('btn-stop-build');
  btn.disabled = true;
  btn.textContent = 'Stopping...';
  try {
    await api('POST', '/admin/api/build/stop');
    toast('Build stopped', 'success');
    loadBuildStatus();
    loadStats();
  } catch (e) {
    toast('Failed to stop: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Stop Build';
  }
}

function startBuildPolling() {
  logOffset = 0;
  document.getElementById('log-viewer').innerHTML = '';
  if (buildPollTimer) clearInterval(buildPollTimer);
  if (logPollTimer) clearInterval(logPollTimer);
  buildPollTimer = setInterval(loadBuildStatus, 5000);
  logPollTimer = setInterval(pollLogs, 3000);
  pollLogs(); // fetch immediately
}

function stopBuildPolling() {
  if (buildPollTimer) { clearInterval(buildPollTimer); buildPollTimer = null; }
  if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; }
}

function colorLogLine(line) {
  if (/ERROR|Failed|failed|error/i.test(line)) return '<span class="log-error">' + line + '</span>';
  if (/WARNING|warn/i.test(line)) return '<span class="log-warning">' + line + '</span>';
  if (/Progress:|processed|items tagged|complete|COMPLETE|succeeded/i.test(line)) return '<span class="log-progress">' + line + '</span>';
  return '<span class="log-info">' + line + '</span>';
}

async function pollLogs() {
  try {
    const res = await api('GET', '/admin/api/build/logs?after=' + logOffset);
    if (res.lines.length > 0) {
      const viewer = document.getElementById('log-viewer');
      const wasScrolled = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
      res.lines.forEach(function(line) {
        // Escape HTML
        const safe = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        viewer.innerHTML += colorLogLine(safe) + '\\n';
      });
      logOffset = res.total;
      document.getElementById('log-count').textContent = '(' + res.total + ' lines)';
      if (wasScrolled) viewer.scrollTop = viewer.scrollHeight;
    }
  } catch(e) { /* ignore polling errors */ }
}

async function loadBuildStatus() {
  try {
    const s = await api('GET', '/admin/api/build/status');
    const el = document.getElementById('build-status-content');
    const stopRow = document.getElementById('build-stop-row');
    const logCard = document.getElementById('build-log-card');

    if (s.running) {
      document.getElementById('btn-start-build').disabled = true;
      document.getElementById('btn-daily-update').disabled = true;
      stopRow.style.display = 'block';
      logCard.style.display = 'block';

      // Ensure log polling is running
      if (!logPollTimer) {
        logPollTimer = setInterval(pollLogs, 3000);
        pollLogs();
      }

      el.innerHTML =
        '<div class="alert alert-info"><span class="spinner"></span> Build in progress (' + (s.job_type || 'build') + ')</div>' +
        '<div class="build-info">' +
          '<div class="build-stat"><div class="val">' + fmtNum(s.movies_tagged) + '</div><div class="lbl">Movies Tagged</div></div>' +
          '<div class="build-stat"><div class="val">' + fmtNum(s.shows_tagged) + '</div><div class="lbl">Shows Tagged</div></div>' +
          '<div class="build-stat"><div class="val">' + fmtDuration(s.elapsed_seconds || 0) + '</div><div class="lbl">Elapsed</div></div>' +
          '<div class="build-stat"><div class="val">' + fmtDate(s.started_at) + '</div><div class="lbl">Started</div></div>' +
        '</div>';
    } else {
      document.getElementById('btn-start-build').disabled = false;
      document.getElementById('btn-daily-update').disabled = false;
      stopRow.style.display = 'none';
      el.innerHTML = '<p style="color:#8b949e">No build currently running.</p>';
      stopBuildPolling();
      // Do one final log poll then show card if there are logs
      pollLogs().then(function() {
        logCard.style.display = logOffset > 0 ? 'block' : 'none';
      });
      loadStats();
    }
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

// ---- Diagnostic & Regenerate ----
async function runDebug() {
  const el = document.getElementById('debug-content');
  el.innerHTML = '<div class="alert alert-info"><span class="spinner"></span> Running diagnostic...</div>';
  try {
    const d = await api('GET', '/admin/api/debug');
    const p = d.pipeline;
    let html = '<div class="build-info" style="grid-template-columns:repeat(3,1fr);margin-bottom:16px">' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.tags) + '</div><div class="lbl">Tags</div></div>' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.movies_tagged) + '</div><div class="lbl">Movies Tagged</div></div>' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.shows_tagged) + '</div><div class="lbl">Shows Tagged</div></div>' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.metadata_cached) + '</div><div class="lbl">Metadata Cached</div></div>' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.active_categories) + '</div><div class="lbl">Active Categories</div></div>' +
      '<div class="build-stat"><div class="val">' + fmtNum(p.pre_computed_catalog_items) + '</div><div class="lbl">Catalog Items</div></div>' +
      '</div>';

    if (d.issues.length > 0) {
      html += d.issues.map(function(i) { return '<div class="alert alert-error">' + i + '</div>'; }).join('');
    } else {
      html += '<div class="alert alert-success">All pipeline stages healthy.</div>';
    }

    if (d.categories.length > 0) {
      html += '<details style="margin-top:12px"><summary style="cursor:pointer;color:#58a6ff;font-size:14px">Category Breakdown (' + d.categories.length + ' categories)</summary>' +
        '<table style="margin-top:8px"><thead><tr><th>Category</th><th>Type</th><th>Pre-computed</th><th>Sample Match</th></tr></thead><tbody>';
      d.categories.forEach(function(c) {
        html += '<tr><td>' + c.name + '</td><td>' + c.media_type + '</td><td>' + fmtNum(c.pre_computed_items) + '</td><td>' + c.potential_matches_sample + '</td></tr>';
      });
      html += '</tbody></table></details>';
    }

    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div class="alert alert-error">Diagnostic failed: ' + (e.message || 'Unknown error - check server logs') + '</div>';
  }
}

async function regenerateCatalogs() {
  if (!confirm('Regenerate all universal catalogs from existing tags?\\n\\nThis rebuilds the pre-computed catalog content using your tagged items. No re-tagging needed.')) return;
  const btn = document.getElementById('btn-regenerate');
  btn.disabled = true;
  btn.textContent = 'Regenerating...';
  try {
    const res = await api('POST', '/admin/api/catalogs/regenerate');
    toast('Catalogs regenerated: ' + fmtNum(res.total_catalog_items) + ' items', 'success');
    loadStats();
    runDebug();
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Regenerate Catalogs';
  }
}

// ---- Init ----
checkAuth();
</script>
</body>
</html>"""
