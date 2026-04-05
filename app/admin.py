"""
Admin portal for Curatio.

Provides a web dashboard to manage settings, trigger builds, and monitor status.
Protected by master password authentication.
"""

import asyncio
import collections
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import func

from app.auth import verify_password, verify_totp, decrypt_totp_secret
from app.config import settings
from app.database import get_db
from app.models import (
    AdminSetting,
    AdminSession,
    AppPairingSession,
    DevicePairingSession,
    InviteCode,
    Tag,
    MovieTag,
    MediaMetadata,
    UniversalCategory,
    UniversalCatalogContent,
    User,
    UserCatalog,
    UserSession,
    TaggingJob,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _run_build_sync(build_main, movies: int, shows: int):
    """Run the async build worker in a dedicated event loop inside a thread.

    This keeps synchronous DB calls off the main event loop so the web UI
    stays responsive during long builds.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(build_main(movies, shows))
    finally:
        loop.close()


def _run_daily_sync(run_daily_update):
    """Run the async daily update in a dedicated event loop inside a thread."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run_daily_update())
    finally:
        loop.close()


# Active build task (in-memory, intentionally not persisted)
_active_build_task: Optional[asyncio.Task] = None
SESSION_DURATION = timedelta(hours=24)

# ---- Build log ring buffer ----
_build_logs: collections.deque[str] = collections.deque(maxlen=2000)
_build_log_sink_id: Optional[int] = None

# ---- WebSocket clients for live build logs ----
_ws_clients: set = set()


def _start_log_capture():
    """Add a loguru sink that captures build-related log lines."""
    global _build_log_sink_id
    if _build_log_sink_id is not None:
        return  # already capturing

    _build_logs.clear()

    # Capture the main event loop so we can schedule WS broadcasts
    # from the build thread (which has its own loop).
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = None

    def _sink(message):
        line = str(message).rstrip()
        _build_logs.append(line)
        # Broadcast to WebSocket clients (fire-and-forget)
        if _ws_clients and _main_loop and _main_loop.is_running():
            _main_loop.call_soon_threadsafe(
                _main_loop.create_task, _broadcast_log(line)
            )

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
                "paused",
                "Paused",
                "resumed",
                "Resumed",
                "Auto-resum",
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
    """Verify admin authentication via cookie (DB-backed sessions)."""
    token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with get_db() as db:
        session = db.query(AdminSession).filter(AdminSession.token == token).first()
        if not session:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if datetime.utcnow() > session.expires_at:
            db.query(AdminSession).filter(AdminSession.token == token).delete()
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
    """Authenticate via user credentials (email + password + 2FA) or master password.

    User-based login: send {"email", "password", "totp_code"}.
    Master password fallback: send {"password"} only (no email).
    """
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    totp_code = body.get("totp_code", "")

    user_id = None

    if email:
        # --- User-based admin login ---
        with get_db() as db:
            user = db.query(User).filter(User.email == email).first()
            if not user or not user.password_hash:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not verify_password(password, user.password_hash):
                raise HTTPException(status_code=401, detail="Invalid credentials")
            if not user.is_admin:
                raise HTTPException(status_code=403, detail="Not an admin")
            if user.totp_enabled:
                if not totp_code:
                    raise HTTPException(status_code=401, detail="2FA code required")
                secret = decrypt_totp_secret(user.totp_secret)
                if not verify_totp(secret, totp_code):
                    raise HTTPException(status_code=401, detail="Invalid 2FA code")
            user_id = user.id
    else:
        # --- Master password fallback ---
        if password != settings.master_password:
            raise HTTPException(status_code=401, detail="Invalid password")

    token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + SESSION_DURATION

    with get_db() as db:
        db.query(AdminSession).filter(
            AdminSession.expires_at < datetime.utcnow()
        ).delete()
        db.add(AdminSession(token=token, user_id=user_id, expires_at=expires_at))

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
    if token:
        with get_db() as db:
            db.query(AdminSession).filter(AdminSession.token == token).delete()
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
            "CATALOG_PAGE_SIZE": int(val("CATALOG_PAGE_SIZE")),
            "CATALOG_SHUFFLE_HOURS": int(val("CATALOG_SHUFFLE_HOURS")),
            "GEMINI_MODEL": val("GEMINI_MODEL"),
        },
        "features": {
            "ENABLE_UNIVERSAL_CATALOGS": val("ENABLE_UNIVERSAL_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_PERSONALIZED_CATALOGS": val("ENABLE_PERSONALIZED_CATALOGS").lower()
            in ("true", "1"),
            "ENABLE_TRAKT_SYNC": val("ENABLE_TRAKT_SYNC").lower() in ("true", "1"),
            "HIDE_FOREIGN": val("HIDE_FOREIGN").lower() in ("true", "1"),
            "HIDE_ADULT": val("HIDE_ADULT").lower() in ("true", "1"),
            "HIDE_UNRELEASED": val("HIDE_UNRELEASED").lower() in ("true", "1"),
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
        "CATALOG_PAGE_SIZE",
        "CATALOG_SHUFFLE_HOURS",
        "GEMINI_MODEL",
        "MASTER_PASSWORD",
        "ENABLE_UNIVERSAL_CATALOGS",
        "ENABLE_PERSONALIZED_CATALOGS",
        "ENABLE_TRAKT_SYNC",
        "HIDE_FOREIGN",
        "HIDE_ADULT",
        "HIDE_UNRELEASED",
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

            # Run in a thread so synchronous DB calls don't block the event loop
            await asyncio.to_thread(_run_build_sync, build_main, movies, shows)
        except asyncio.CancelledError:
            logger.warning("Build task was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Build task failed: {e}")
        finally:
            _stop_log_capture()

    _active_build_task = asyncio.create_task(_run())
    logger.info(f"Database build started: {movies} movies, {shows} shows")
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

            await asyncio.to_thread(_run_daily_sync, run_daily_update)
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


@router.post("/api/sync-users")
async def sync_all_users_endpoint(request: Request, _=Depends(verify_admin)):
    """Sync personalized catalogs for all users."""
    from workers.trakt_sync import sync_all_users

    with get_db() as db:
        stats = await sync_all_users(db)

    return stats


@router.get("/api/build/status")
async def get_build_status(request: Request, _=Depends(verify_admin)):
    """Get current build progress."""
    from workers.initial_build import pause_event

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
                "paused": not pause_event.is_set(),
                "job_type": running_job.job_type,
                "started_at": running_job.started_at.isoformat(),
                "elapsed_seconds": int(elapsed),
                "movies_tagged": movies_done,
                "shows_tagged": shows_done,
            }

    task_running = _active_build_task is not None and not _active_build_task.done()
    return {"running": task_running, "paused": False}


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


@router.post("/api/build/pause")
async def pause_build(request: Request, _=Depends(verify_admin)):
    """Pause the currently running build between batches."""
    from workers.initial_build import pause_event

    if _active_build_task is None or _active_build_task.done():
        # Check if there's a DB-level running job (orphaned after restart)
        with get_db() as db:
            running = (
                db.query(TaggingJob).filter(TaggingJob.status == "running").first()
            )
            if not running:
                raise HTTPException(404, "No build is currently running")

    pause_event.clear()

    # Persist to DB so pause survives container restart
    with get_db() as db:
        existing = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if existing:
            existing.value = "true"
            existing.updated_at = datetime.utcnow()
        else:
            db.add(
                AdminSetting(
                    key="BUILD_PAUSED", value="true", updated_at=datetime.utcnow()
                )
            )
        db.commit()

    logger.info("Build paused by user")
    return {"status": "paused"}


@router.post("/api/build/resume")
async def resume_build(request: Request, _=Depends(verify_admin)):
    """Resume a paused build. If the container restarted while paused, re-launches it."""
    global _active_build_task
    from workers.initial_build import pause_event

    # Clear the pause flag in DB
    with get_db() as db:
        existing = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if existing:
            existing.value = "false"
            existing.updated_at = datetime.utcnow()
            db.commit()

    # If the build task is alive, just unpause it
    if _active_build_task is not None and not _active_build_task.done():
        pause_event.set()
        logger.info("Build resumed by user")
        return {"status": "resumed"}

    # No live task — check for an orphaned running job (container restarted while paused)
    with get_db() as db:
        orphaned = (
            db.query(TaggingJob)
            .filter(TaggingJob.status == "running")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )
        if orphaned and orphaned.job_metadata:
            movies = orphaned.job_metadata.get("movies_target", 100000)
            shows = orphaned.job_metadata.get("shows_target", 50000)
        else:
            raise HTTPException(404, "No paused build found to resume")

    # Mark orphaned job and re-launch
    pause_event.set()
    _relaunch_build(movies, shows, reason="Resumed after container restart")
    logger.info("Build re-launched after resume (container had restarted)")
    return {"status": "resumed", "relaunched": True}


def _relaunch_build(movies: int, shows: int, reason: str = "Auto-resumed"):
    """Re-launch the build, marking any orphaned jobs."""
    global _active_build_task

    with get_db() as db:
        for job in db.query(TaggingJob).filter(TaggingJob.status == "running"):
            job.status = "cancelled"
            job.error_message = reason
            job.completed_at = datetime.utcnow()
        db.commit()

    _start_log_capture()

    async def _run():
        try:
            from workers.initial_build import main as build_main

            await asyncio.to_thread(_run_build_sync, build_main, movies, shows)
        except asyncio.CancelledError:
            logger.warning("Build task was cancelled by user")
            _mark_running_jobs_cancelled()
        except Exception as e:
            logger.error(f"Build task failed: {e}")
        finally:
            _stop_log_capture()

    _active_build_task = asyncio.create_task(_run())


async def auto_resume_build():
    """Called on app startup. Re-launches orphaned builds if not paused."""
    from workers.initial_build import pause_event

    with get_db() as db:
        orphaned = (
            db.query(TaggingJob)
            .filter(TaggingJob.status == "running")
            .order_by(TaggingJob.started_at.desc())
            .first()
        )
        if not orphaned:
            return

        # Check if build was paused when container went down
        paused_setting = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        is_paused = paused_setting and paused_setting.value == "true"

        if not orphaned.job_metadata:
            # Can't resume without params — mark as interrupted
            orphaned.status = "cancelled"
            orphaned.error_message = (
                "Interrupted by container restart (no params to resume)"
            )
            orphaned.completed_at = datetime.utcnow()
            db.commit()
            logger.warning(
                "Found orphaned build job but no metadata — marked cancelled"
            )
            return

        movies = orphaned.job_metadata.get("movies_target", 100000)
        shows = orphaned.job_metadata.get("shows_target", 50000)

    if is_paused:
        pause_event.clear()
        logger.info(
            f"Found paused build (movies={movies}, shows={shows}). "
            "Waiting for manual resume via admin panel."
        )
        # Re-launch the build but it will immediately block on pause_event
        _relaunch_build(movies, shows, reason="Container restarted while paused")
    else:
        logger.info(
            f"Auto-resuming interrupted build (movies={movies}, shows={shows})..."
        )
        _relaunch_build(movies, shows, reason="Container restarted — auto-resuming")


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
    if has_task and _active_build_task is not None:
        _active_build_task.cancel()
        try:
            await asyncio.wait_for(_active_build_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Mark DB jobs as cancelled (safety net for both scenarios)
    _mark_running_jobs_cancelled()
    _stop_log_capture()
    _active_build_task = None

    # Clear pause flag so a future build starts clean
    with get_db() as db:
        paused = (
            db.query(AdminSetting).filter(AdminSetting.key == "BUILD_PAUSED").first()
        )
        if paused:
            paused.value = "false"
            db.commit()

    # Ensure pause_event is reset for next build
    from workers.initial_build import pause_event

    pause_event.set()

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


@router.websocket("/ws/build-logs")
async def ws_build_logs(ws):
    """WebSocket endpoint for real-time build log streaming."""
    from starlette.websockets import WebSocketDisconnect

    await ws.accept()
    _ws_clients.add(ws)
    # Send existing logs as initial payload
    for line in list(_build_logs):
        await ws.send_text(line)
    try:
        while True:
            # Keep connection alive; client doesn't send data
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(ws)


async def _broadcast_log(line: str):
    """Send a log line to all connected WebSocket clients."""
    global _ws_clients
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(line)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


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
            # Snapshot category attributes up-front so a later rollback
            # can't trigger DetachedInstanceError on lazy-refresh.
            cat_snapshots = [
                {
                    "id": cat.id,
                    "name": cat.name,
                    "media_type": cat.media_type,
                    "tag_formula": cat.tag_formula,
                }
                for cat in categories
            ]

            category_details = []
            generator = CatalogGenerator(db)
            for cat_info in cat_snapshots:
                try:
                    content_count = (
                        db.query(func.count(UniversalCatalogContent.tmdb_id))
                        .filter(UniversalCatalogContent.category_id == cat_info["id"])
                        .scalar()
                        or 0
                    )
                except Exception:
                    db.rollback()
                    content_count = -1
                # Check how many items would match the formula (live query)
                # Wrapped in try/except because DB contention during active
                # builds can cause this to fail
                try:
                    cat_obj = (
                        db.query(UniversalCategory)
                        .filter(UniversalCategory.id == cat_info["id"])
                        .first()
                    )
                    if cat_obj:
                        potential_matches = len(
                            generator.generate_universal_catalog(cat_obj, limit=5)
                        )
                    else:
                        potential_matches = -1
                except Exception:
                    db.rollback()
                    potential_matches = -1  # indicates query failed
                category_details.append(
                    {
                        "id": cat_info["id"],
                        "name": cat_info["name"],
                        "media_type": cat_info["media_type"],
                        "tag_formula": cat_info["tag_formula"],
                        "pre_computed_items": content_count,
                        "potential_matches_sample": potential_matches,
                    }
                )

            # Layer 6: Last tagging job status
            last_job = (
                db.query(TaggingJob).order_by(TaggingJob.started_at.desc()).first()
            )

            # Build diagnosis
            issues = []
            if tag_count == 0:
                issues.append("No tags exist. Run a database build to create tags.")
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

        new_total = db.query(func.count(UniversalCatalogContent.tmdb_id)).scalar() or 0

    logger.info(f"Catalog regeneration complete: {new_total} total items")
    return {"status": "ok", "total_catalog_items": new_total}


# ---- Invite Codes ----


@router.get("/api/invites")
async def list_invites(request: Request, _=Depends(verify_admin)):
    """List all invite codes."""
    with get_db() as db:
        codes = db.query(InviteCode).order_by(InviteCode.created_at.desc()).all()
        return [
            {
                "code": c.code,
                "label": c.label,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "is_used": c.is_used,
                "used_at": c.used_at.isoformat() if c.used_at else None,
                "used_by": c.used_by,
            }
            for c in codes
        ]


@router.post("/api/invites")
async def create_invite(request: Request, _=Depends(verify_admin)):
    """Generate a new one-time invite code."""
    body = await request.json()
    label = body.get("label", "")
    expires_hours = body.get("expires_hours")  # None = never expires

    code = secrets.token_urlsafe(24)

    expires_at = None
    if expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=int(expires_hours))

    with get_db() as db:
        db.add(
            InviteCode(
                code=code,
                label=label,
                expires_at=expires_at,
            )
        )

    return {"code": code, "expires_at": expires_at.isoformat() if expires_at else None}


@router.delete("/api/invites/{code}")
async def delete_invite(code: str, request: Request, _=Depends(verify_admin)):
    """Delete an invite code."""
    with get_db() as db:
        deleted = db.query(InviteCode).filter(InviteCode.code == code).delete()
        if not deleted:
            raise HTTPException(404, "Invite code not found")
    return {"status": "ok"}


# ---- Trakt Users ----


@router.get("/api/users")
async def list_users(request: Request, _=Depends(verify_admin)):
    """List all Trakt-authenticated users."""
    with get_db() as db:
        users = db.query(User).order_by(User.created_at.desc()).all()

        user_list = []
        for u in users:
            catalog_count = (
                db.query(func.count(UserCatalog.id))
                .filter(UserCatalog.user_id == u.id, UserCatalog.is_active.is_(True))
                .scalar()
                or 0
            )
            user_list.append(
                {
                    "id": u.id,
                    "trakt_username": u.trakt_username,
                    "trakt_user_id": u.trakt_user_id,
                    "user_key": u.user_key[:8] + "...",
                    "is_active": u.is_active,
                    "auth_source": getattr(u, "auth_source", "trakt"),
                    "email": getattr(u, "email", None),
                    "display_name": getattr(u, "display_name", None),
                    "totp_enabled": getattr(u, "totp_enabled", False),
                    "bandwidth_tier": getattr(u, "bandwidth_tier", "high"),
                    "is_admin": getattr(u, "is_admin", False),
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_login": u.last_login.isoformat() if u.last_login else None,
                    "last_sync": u.last_sync.isoformat() if u.last_sync else None,
                    "catalog_count": catalog_count,
                }
            )

        return user_list


@router.post("/api/users/{user_id}/toggle")
async def toggle_user(user_id: int, request: Request, _=Depends(verify_admin)):
    """Enable or disable a user."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.is_active = not user.is_active
        new_status = user.is_active
        db.commit()
    return {"status": "ok", "is_active": new_status}


@router.delete("/api/users/{user_id}")
async def delete_user(user_id: int, request: Request, _=Depends(verify_admin)):
    """Delete a user and their catalogs."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        db.delete(user)
        db.commit()
    return {"status": "ok"}


@router.post("/api/users/{user_id}/bandwidth")
async def set_user_bandwidth(user_id: int, request: Request, _=Depends(verify_admin)):
    """Set a user's bandwidth tier (low or high)."""
    body = await request.json()
    tier = body.get("tier", "high")
    if tier not in ("low", "high"):
        raise HTTPException(400, "tier must be 'low' or 'high'")

    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.bandwidth_tier = tier
        db.commit()
    return {"status": "ok", "bandwidth_tier": tier}


@router.get("/api/aiostreams-config")
async def get_aiostreams_config(request: Request, _=Depends(verify_admin)):
    """Get current AIOStreams URLs."""
    with get_db() as db:
        low = (
            db.query(AdminSetting)
            .filter(AdminSetting.key == "aiostreams_low_bw_url")
            .first()
        )
        high = (
            db.query(AdminSetting)
            .filter(AdminSetting.key == "aiostreams_high_bw_url")
            .first()
        )
    return {
        "low_bw_url": low.value if low else "",
        "high_bw_url": high.value if high else "",
    }


@router.post("/api/aiostreams-config")
async def set_aiostreams_config(request: Request, _=Depends(verify_admin)):
    """Save AIOStreams URLs (low and high bandwidth)."""
    body = await request.json()
    low_url = body.get("low_bw_url", "").strip()
    high_url = body.get("high_bw_url", "").strip()

    with get_db() as db:
        for key, value in [
            ("aiostreams_low_bw_url", low_url),
            ("aiostreams_high_bw_url", high_url),
        ]:
            row = db.query(AdminSetting).filter(AdminSetting.key == key).first()
            if row:
                row.value = value
            else:
                db.add(AdminSetting(key=key, value=value))
        db.commit()

    # Clear stream proxy URL cache
    from app.stream_proxy import _aiostreams_url_cache

    _aiostreams_url_cache.clear()

    return {"status": "ok"}


# ---- 2FA Reset ----


@router.post("/api/users/{user_id}/reset-2fa")
async def reset_user_2fa(user_id: int, request: Request, _=Depends(verify_admin)):
    """Disable 2FA for a user (admin override)."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        if not user.totp_enabled:
            raise HTTPException(400, "User does not have 2FA enabled")
        user.totp_enabled = False
        user.totp_secret = None
        db.commit()
        logger.info(f"Admin reset 2FA for user {user_id}")
    return {"status": "ok"}


@router.post("/api/users/{user_id}/admin")
async def set_user_admin(user_id: int, request: Request, _=Depends(verify_admin)):
    """Promote or demote a user as admin."""
    body = await request.json()
    is_admin = body.get("is_admin", False)
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.is_admin = bool(is_admin)
        db.commit()
        logger.info(f"User {user_id} admin status set to {user.is_admin}")
    return {"status": "ok", "is_admin": is_admin}


# ---- Sessions Management ----


@router.get("/api/sessions")
async def list_sessions(request: Request, _=Depends(verify_admin)):
    """List all active user sessions and pairing sessions."""
    now = datetime.utcnow()
    with get_db() as db:
        # User web sessions
        user_sessions = (
            db.query(UserSession, User)
            .join(User, UserSession.user_id == User.id)
            .filter(UserSession.expires_at > now)
            .order_by(UserSession.created_at.desc())
            .all()
        )
        web_sessions = [
            {
                "token": s.token[:8] + "...",
                "token_full": s.token,
                "user_id": s.user_id,
                "username": u.trakt_username
                or u.display_name
                or u.email
                or f"User #{u.id}",
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "web",
            }
            for s, u in user_sessions
        ]

        # Pairing sessions (only unclaimed and not expired)
        pairing_sessions = (
            db.query(AppPairingSession, User)
            .join(User, AppPairingSession.user_id == User.id)
            .filter(
                AppPairingSession.expires_at > now,
                AppPairingSession.claimed == False,  # noqa: E712
            )
            .order_by(AppPairingSession.created_at.desc())
            .all()
        )
        pairing = [
            {
                "token": s.token[:8] + "...",
                "token_full": s.token,
                "user_id": s.user_id,
                "username": u.trakt_username
                or u.display_name
                or u.email
                or f"User #{u.id}",
                "short_code": s.short_code,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "pairing",
            }
            for s, u in pairing_sessions
        ]

        # Device pairing sessions (unclaimed and not expired)
        device_sessions = (
            db.query(DevicePairingSession)
            .filter(
                DevicePairingSession.expires_at > now,
                DevicePairingSession.claimed == False,  # noqa: E712
            )
            .order_by(DevicePairingSession.created_at.desc())
            .all()
        )
        device = [
            {
                "token": s.device_token[:8] + "...",
                "token_full": s.device_token,
                "short_code": s.short_code,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "type": "device",
            }
            for s in device_sessions
        ]

    return {
        "web_sessions": web_sessions,
        "pairing_sessions": pairing,
        "device_sessions": device,
    }


@router.delete("/api/sessions/web/{token}")
async def revoke_web_session(token: str, request: Request, _=Depends(verify_admin)):
    """Revoke a user's web login session."""
    with get_db() as db:
        deleted = db.query(UserSession).filter(UserSession.token == token).delete()
        if not deleted:
            raise HTTPException(404, "Session not found")
    logger.info(f"Admin revoked web session {token[:8]}...")
    return {"status": "ok"}


@router.delete("/api/sessions/pairing/{token}")
async def revoke_pairing_session(token: str, request: Request, _=Depends(verify_admin)):
    """Revoke an active pairing session."""
    with get_db() as db:
        deleted = (
            db.query(AppPairingSession)
            .filter(AppPairingSession.token == token)
            .delete()
        )
        if not deleted:
            raise HTTPException(404, "Pairing session not found")
    logger.info(f"Admin revoked pairing session {token[:8]}...")
    return {"status": "ok"}


@router.get("/api/active-streams")
async def active_streams(request: Request, _=Depends(verify_admin)):
    """Return currently active stream sessions (last 5 minutes)."""
    from app.stream_proxy import get_active_streams

    streams = get_active_streams()
    return {
        "streams": [
            {
                "user": s["user"],
                "video_id": s["video_id"],
                "type": s["type"],
                "tier": s["tier"],
                "title": s.get("title"),
                "started_at": datetime.utcfromtimestamp(s["ts"]).isoformat(),
                "ago_seconds": int(time.time() - s["ts"]),
            }
            for s in streams
        ]
    }


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
<title>Admin - Curatio</title>
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
.login-box input:focus{border-color:#a855f7}
.login-box button{width:100%;padding:12px;background:#a855f7;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;transition:background .2s}
.login-box button:hover{background:#7c3aed}
.login-error{color:#f85149;margin-top:12px;font-size:14px;min-height:20px}

/* Dashboard layout */
#dashboard{display:none;min-height:100vh}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:16px 32px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;font-weight:600}
.topbar h1 span{color:#a855f7}
.topbar-actions{display:flex;gap:12px;align-items:center}
.topbar-actions button{padding:6px 16px;background:transparent;border:1px solid #30363d;border-radius:6px;color:#8b949e;font-size:13px;cursor:pointer;transition:all .2s}
.topbar-actions button:hover{color:#e6edf3;border-color:#8b949e}
.tab-nav{display:flex;gap:0;background:#161b22;border-bottom:1px solid #30363d;padding:0 32px}
.tab-btn{padding:12px 20px;background:none;border:none;border-bottom:2px solid transparent;color:#8b949e;font-size:14px;font-weight:500;cursor:pointer;transition:all .2s}
.tab-btn:hover{color:#e6edf3}
.tab-btn.active{color:#e6edf3;border-bottom-color:#a855f7}
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
.toggle input:checked+.toggle-track{background:#a855f7}
.toggle-track::after{content:'';position:absolute;width:20px;height:20px;background:#e6edf3;border-radius:50%;top:2px;left:2px;transition:transform .2s}
.toggle input:checked+.toggle-track::after{transform:translateX(20px)}
.toggle-label{font-size:14px;color:#e6edf3}

/* Buttons */
.btn{padding:10px 20px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;display:inline-flex;align-items:center;gap:8px}
.btn-primary{background:#a855f7;color:#fff}
.btn-primary:hover{background:#7c3aed}
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
.progress-fill{height:100%;background:linear-gradient(90deg,#a855f7,#c084fc);border-radius:4px;transition:width .5s ease}

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
.spinner{display:inline-block;width:18px;height:18px;border:2px solid #30363d;border-top-color:#a855f7;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
#toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:14px;transform:translateY(100px);opacity:0;transition:all .3s;z-index:1000}
#toast.show{transform:translateY(0);opacity:1}

/* ---- Mobile responsive ---- */
@media(max-width:768px){
  /* Topbar */
  .topbar{padding:12px 16px;flex-wrap:wrap;gap:8px}
  .topbar h1{font-size:16px}
  .topbar-actions{gap:8px}
  .topbar-actions button{padding:6px 12px;font-size:12px}

  /* Tab navigation — horizontally scrollable */
  .tab-nav{padding:0 8px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;-ms-overflow-style:none}
  .tab-nav::-webkit-scrollbar{display:none}
  .tab-btn{padding:10px 14px;font-size:13px;white-space:nowrap;flex-shrink:0}

  /* Main content area */
  .main{padding:16px}

  /* Stats */
  .stats-grid{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
  .stat-card{padding:16px}
  .stat-value{font-size:24px}
  .stat-label{font-size:11px}

  /* Cards */
  .card{padding:16px;margin-bottom:16px;border-radius:8px}
  .card h3{font-size:15px;margin-bottom:12px}
  .card-row{grid-template-columns:1fr;gap:16px}

  /* Tables — horizontal scroll wrapper */
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap}
  thead{display:table;width:100%;table-layout:auto}
  tbody{display:table;width:100%;table-layout:auto}
  th{padding:8px 10px;font-size:11px}
  td{padding:8px 10px;font-size:13px}

  /* Forms */
  .form-group input[type="text"],.form-group input[type="password"],.form-group input[type="number"],.form-group input[type="time"],.form-group select{font-size:16px;padding:12px 14px}

  /* Buttons */
  .btn{padding:12px 16px;font-size:13px;min-height:44px;-webkit-tap-highlight-color:transparent}
  .btn:active{opacity:.85}
  .btn-sm{padding:8px 12px;font-size:12px;min-height:40px}

  /* Build info grid */
  .build-info{grid-template-columns:1fr 1fr;gap:12px}
  .build-stat .val{font-size:20px}

  /* Log viewer */
  .log-viewer{font-size:11px;max-height:300px;padding:10px}

  /* Login */
  .login-box{padding:32px 24px;margin:16px;max-width:none}

  /* Toggle */
  .toggle-label{font-size:13px}

  /* Alerts */
  .alert{font-size:13px;padding:10px 14px}

  /* Toast */
  #toast{bottom:16px;right:16px;left:16px;text-align:center;font-size:13px}
}

@media(max-width:400px){
  .topbar{padding:10px 12px}
  .topbar h1{font-size:14px}
  .tab-btn{padding:8px 10px;font-size:12px}
  .main{padding:12px}
  .stats-grid{grid-template-columns:1fr 1fr;gap:8px}
  .stat-card{padding:12px}
  .stat-value{font-size:20px}
  .card{padding:14px}
  .build-info{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- Login Screen -->
<div id="login-screen">
  <div class="login-box">
    <h1>Curatio</h1>
    <p>Admin Portal</p>
    <form id="login-form">
      <div id="user-login-fields">
        <input type="email" id="login-email" placeholder="Email" autocomplete="email" autofocus>
        <input type="password" id="login-password" placeholder="Password" autocomplete="current-password">
        <input type="text" id="login-totp" placeholder="2FA Code" maxlength="6" inputmode="numeric" autocomplete="one-time-code" style="display:none">
      </div>
      <button type="submit">Sign In</button>
    </form>
    <div class="login-error" id="login-error"></div>
    <p style="margin-top:20px"><a href="#" id="toggle-master-pw" style="color:#8b949e;font-size:13px;text-decoration:none">Use master password instead</a></p>
  </div>
</div>

<!-- Dashboard -->
<div id="dashboard">
  <div class="topbar">
    <h1><span>Curatio</span> Admin</h1>
    <div class="topbar-actions">
      <button onclick="loadAll()">Refresh</button>
      <button onclick="logout()">Sign Out</button>
    </div>
  </div>

  <div class="tab-nav">
    <button class="tab-btn active" data-tab="overview">Overview</button>
    <button class="tab-btn" data-tab="invites">Invites</button>
    <button class="tab-btn" data-tab="users">Users</button>
    <button class="tab-btn" data-tab="streams">Streams</button>
    <button class="tab-btn" data-tab="sessions">Sessions</button>
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

    <!-- INVITES TAB -->
    <div class="tab-panel" id="tab-invites">
      <div class="card">
        <h3>Generate Invite Code</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          Create one-time invite codes for new users. Each code can only be used once.
        </p>
        <div class="card-row">
          <div class="form-group">
            <label>Label (optional)</label>
            <input type="text" id="invite-label" placeholder="e.g. For John">
          </div>
          <div class="form-group">
            <label>Expires After (hours)</label>
            <input type="number" id="invite-expires" placeholder="Leave blank for no expiry" min="1">
            <div class="hint">Leave empty for codes that never expire.</div>
          </div>
        </div>
        <button class="btn btn-primary" onclick="createInvite()">Generate Invite Code</button>
        <div id="invite-result" style="display:none;margin-top:16px">
          <div class="alert alert-success" style="font-family:monospace;font-size:16px;word-break:break-all;user-select:all" id="invite-code-display"></div>
          <div class="hint" style="margin-top:8px">Share this code with the user. It can only be used once.</div>
        </div>
      </div>

      <div class="card">
        <h3>Invite Codes</h3>
        <table>
          <thead><tr><th>Code</th><th>Label</th><th>Status</th><th>Created</th><th>Expires</th><th>Used By</th><th>Actions</th></tr></thead>
          <tbody id="invites-tbody"><tr><td colspan="7" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- USERS TAB -->
    <div class="tab-panel" id="tab-users">
      <div class="card">
        <h3>Users</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          All registered users (Trakt OAuth and local email/password accounts).
        </p>
        <div style="overflow-x:auto">
        <table>
          <thead><tr><th>User</th><th>Email</th><th>Source</th><th>2FA</th><th>Admin</th><th>Bandwidth</th><th>Status</th><th>Created</th><th>Last Login</th><th>Catalogs</th><th>Actions</th></tr></thead>
          <tbody id="users-tbody"><tr><td colspan="11" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
        </div>
      </div>
    </div>

    <!-- STREAMS TAB -->
    <div class="tab-panel" id="tab-streams">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div>
            <h3>Active Streams</h3>
            <p style="color:#8b949e;font-size:13px;margin-top:4px">
              Stream requests in the last 5 minutes. Auto-refreshes every 10 seconds.
            </p>
          </div>
          <button class="btn btn-sm" onclick="loadActiveStreams()">Refresh</button>
        </div>
        <table>
          <thead><tr><th>User</th><th>Title</th><th>Type</th><th>Tier</th><th>Video ID</th><th>When</th></tr></thead>
          <tbody id="active-streams-tbody"><tr><td colspan="6" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- SESSIONS TAB -->
    <div class="tab-panel" id="tab-sessions">
      <div class="card">
        <h3>Active Web Sessions</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          Active user login sessions. Revoking a session will force the user to log in again.
        </p>
        <table>
          <thead><tr><th>User</th><th>Token</th><th>Created</th><th>Expires</th><th>Actions</th></tr></thead>
          <tbody id="web-sessions-tbody"><tr><td colspan="5" style="color:#8b949e">Loading...</td></tr></tbody>
        </table>
      </div>

      <div class="card">
        <h3>Active Pairing Sessions</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          Pending app pairing sessions (QR code / short code). These expire after 5 minutes.
        </p>
        <table>
          <thead><tr><th>User</th><th>Short Code</th><th>Token</th><th>Created</th><th>Expires</th><th>Actions</th></tr></thead>
          <tbody id="pairing-sessions-tbody"><tr><td colspan="6" style="color:#8b949e">Loading...</td></tr></tbody>
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
            <input type="text" id="set-ADDON_NAME" placeholder="Curatio">
          </div>
          <div class="form-group">
            <label>Base URL</label>
            <input type="text" id="set-BASE_URL" placeholder="https://your-domain.com">
          </div>
        </div>
        <div class="card-row">
          <div class="form-group">
            <label>Catalog Size (total items stored per catalog)</label>
            <input type="number" id="set-CATALOG_SIZE" min="10" max="1000" placeholder="200">
            <div class="hint">Total items generated into each catalog. Default: 200. Requires &ldquo;Regenerate Catalogs&rdquo; on the Build tab to take effect.</div>
          </div>
          <div class="form-group">
            <label>Page Size (items per Stremio page)</label>
            <input type="number" id="set-CATALOG_PAGE_SIZE" min="10" max="200" placeholder="100">
            <div class="hint">Items returned per request. Default: 100</div>
          </div>
        </div>
        <div class="card-row">
          <div class="form-group">
            <label>Shuffle Interval (hours)</label>
            <input type="number" id="set-CATALOG_SHUFFLE_HOURS" min="0" max="168" placeholder="3">
            <div class="hint">Randomize catalog order every N hours. 0 = disabled. Default: 3</div>
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

      <div class="card">
        <h3>Content Filters</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">Global content filters applied to all catalogs for every user.</p>
        <div style="display:flex;flex-direction:column;gap:20px">
          <label class="toggle">
            <input type="checkbox" id="set-HIDE_FOREIGN">
            <span class="toggle-track"></span>
            <span class="toggle-label">Hide Foreign Films</span>
          </label>
          <div class="hint" style="margin-top:-12px">Only show English-language content in all catalogs.</div>
          <label class="toggle">
            <input type="checkbox" id="set-HIDE_ADULT">
            <span class="toggle-track"></span>
            <span class="toggle-label">Hide Explicit Content (18+)</span>
          </label>
          <div class="hint" style="margin-top:-12px">Filter out adult-rated titles from all catalogs.</div>
          <label class="toggle">
            <input type="checkbox" id="set-HIDE_UNRELEASED">
            <span class="toggle-track"></span>
            <span class="toggle-label">Hide Unreleased / In Theaters</span>
          </label>
          <div class="hint" style="margin-top:-12px">Filter out movies not yet available digitally (unreleased or still in theatrical window).</div>
        </div>
      </div>

      <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>

      <div class="card" style="margin-top:24px">
        <h3>AIOStreams Configuration</h3>
        <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
          Stream proxy URLs for AIOStreams. Separate URLs for low and high bandwidth tiers.
        </p>
        <div class="card-row">
          <div class="form-group">
            <label>Low Bandwidth URL</label>
            <input type="text" id="set-aio-low-url" placeholder="https://...">
            <div class="hint">Stream proxy URL for users on the low bandwidth tier.</div>
          </div>
          <div class="form-group">
            <label>High Bandwidth URL</label>
            <input type="text" id="set-aio-high-url" placeholder="https://...">
            <div class="hint">Stream proxy URL for users on the high bandwidth tier.</div>
          </div>
        </div>
        <button class="btn btn-primary" onclick="saveAIOStreams()">Save AIOStreams Config</button>
      </div>
    </div>

    <!-- BUILD TAB -->
    <div class="tab-panel" id="tab-build">
      <div id="build-alert"></div>

      <div class="card" id="build-status-card">
        <h3>Build Status</h3>
        <div id="build-status-content">
          <p style="color:#8b949e">No build currently running.</p>
        </div>
        <div id="build-stop-row" style="display:none;margin-top:12px;gap:12px">
          <button class="btn btn-secondary btn-sm" id="btn-pause-build" onclick="pauseBuild()" style="display:none">Pause Build</button>
          <button class="btn btn-primary btn-sm" id="btn-resume-build" onclick="resumeBuild()" style="display:none">Resume Build</button>
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
          <h3>Database Build</h3>
          <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
            Tag movies and TV shows using the Gemini API. Requires paid tier for large batches.
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
          <button class="btn btn-primary" id="btn-start-build" onclick="startBuild()">Start Database Build</button>
        </div>

        <div class="card">
          <h3>Daily Update</h3>
          <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
            Fetches new releases from this week and tags them. Uses free Gemini tier.
          </p>
          <button class="btn btn-secondary" id="btn-daily-update" onclick="triggerDailyUpdate()">Run Daily Update Now</button>
        </div>

        <div class="card">
          <h3>Sync User Catalogs</h3>
          <p style="color:#8b949e;font-size:13px;margin-bottom:20px">
            Re-sync personalized catalogs for all users (trending, new releases, recommendations, etc.).
          </p>
          <button class="btn btn-secondary" id="btn-sync-users" onclick="syncAllUsers()">Sync All Users</button>
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
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail || res.statusText;
    if (res.status === 401 && document.getElementById('dashboard').style.display !== 'none') {
      // Session expired — show login screen
      document.getElementById('dashboard').style.display = 'none';
      document.getElementById('login-screen').style.display = 'flex';
      throw new Error('auth');
    }
    throw new Error(detail);
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
let _checkAuthAbort = null;
let _masterPwMode = false;

document.getElementById('toggle-master-pw').addEventListener('click', (e) => {
  e.preventDefault();
  _masterPwMode = !_masterPwMode;
  const emailEl = document.getElementById('login-email');
  const totpEl = document.getElementById('login-totp');
  const link = document.getElementById('toggle-master-pw');
  if (_masterPwMode) {
    emailEl.style.display = 'none';
    totpEl.style.display = 'none';
    document.getElementById('login-password').placeholder = 'Master Password';
    link.textContent = 'Use email login instead';
    document.getElementById('login-password').focus();
  } else {
    emailEl.style.display = '';
    document.getElementById('login-password').placeholder = 'Password';
    link.textContent = 'Use master password instead';
    emailEl.focus();
  }
});

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (_checkAuthAbort) { _checkAuthAbort.abort(); _checkAuthAbort = null; }
  const btn = e.target.querySelector('button');
  const errEl = document.getElementById('login-error');
  btn.disabled = true;
  btn.textContent = 'Signing in...';
  errEl.textContent = '';

  const pw = document.getElementById('login-password').value;
  const payload = _masterPwMode
    ? { password: pw }
    : {
        email: document.getElementById('login-email').value,
        password: pw,
        totp_code: document.getElementById('login-totp').value
      };

  try {
    await api('POST', '/admin/api/login', payload);
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadAll();
  } catch (err) {
    if (err.message === '2FA code required') {
      document.getElementById('login-totp').style.display = '';
      document.getElementById('login-totp').focus();
      errEl.textContent = 'Enter your 2FA code.';
    } else {
      errEl.textContent =
        err.message === 'auth' ? 'Invalid credentials' : err.message;
    }
    btn.disabled = false;
    btn.textContent = 'Sign In';
  }
});

async function logout() {
  await fetch('/admin/api/logout', { method: 'POST', credentials: 'same-origin' });
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('login-password').value = '';
  document.getElementById('login-email').value = '';
  document.getElementById('login-totp').value = '';
  document.getElementById('login-totp').style.display = 'none';
}

async function checkAuth() {
  // Use fetch directly (not api()) so a slow 401 response cannot
  // trigger the global 401 handler and re-show the login screen
  // after the user has already logged in via the form.
  _checkAuthAbort = new AbortController();
  try {
    const res = await fetch('/admin/api/stats', {
      credentials: 'same-origin',
      signal: _checkAuthAbort.signal
    });
    if (res.ok) {
      document.getElementById('login-screen').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      loadAll();
    }
  } catch(e) { /* show login */ }
  finally { _checkAuthAbort = null; }
}

// ---- Data Loading ----
async function loadAll() {
  await Promise.all([loadStats(), loadSettings(), loadBuildStatus(), loadInvites(), loadUsers(), loadSessions(), loadActiveStreams(), loadAIOStreams()]);
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
    document.getElementById('set-CATALOG_SIZE').value = s.app.CATALOG_SIZE || 200;
    document.getElementById('set-CATALOG_PAGE_SIZE').value = s.app.CATALOG_PAGE_SIZE || 100;
    document.getElementById('set-CATALOG_SHUFFLE_HOURS').value = s.app.CATALOG_SHUFFLE_HOURS || 3;
    document.getElementById('set-GEMINI_MODEL').value = s.app.GEMINI_MODEL || '';

    // Features
    document.getElementById('set-ENABLE_UNIVERSAL_CATALOGS').checked = s.features.ENABLE_UNIVERSAL_CATALOGS;
    document.getElementById('set-ENABLE_PERSONALIZED_CATALOGS').checked = s.features.ENABLE_PERSONALIZED_CATALOGS;
    document.getElementById('set-ENABLE_TRAKT_SYNC').checked = s.features.ENABLE_TRAKT_SYNC;

    // Content Filters
    document.getElementById('set-HIDE_FOREIGN').checked = s.features.HIDE_FOREIGN;
    document.getElementById('set-HIDE_ADULT').checked = s.features.HIDE_ADULT;
    document.getElementById('set-HIDE_UNRELEASED').checked = s.features.HIDE_UNRELEASED;

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
  const textFields = ['ADDON_NAME', 'BASE_URL', 'CATALOG_SIZE', 'CATALOG_PAGE_SIZE', 'CATALOG_SHUFFLE_HOURS', 'GEMINI_MODEL'];
  textFields.forEach(k => {
    const v = document.getElementById('set-' + k).value.trim();
    if (v) data[k] = v;
  });

  // Toggle fields
  data.ENABLE_UNIVERSAL_CATALOGS = document.getElementById('set-ENABLE_UNIVERSAL_CATALOGS').checked;
  data.ENABLE_PERSONALIZED_CATALOGS = document.getElementById('set-ENABLE_PERSONALIZED_CATALOGS').checked;
  data.ENABLE_TRAKT_SYNC = document.getElementById('set-ENABLE_TRAKT_SYNC').checked;
  data.HIDE_FOREIGN = document.getElementById('set-HIDE_FOREIGN').checked;
  data.HIDE_ADULT = document.getElementById('set-HIDE_ADULT').checked;
  data.HIDE_UNRELEASED = document.getElementById('set-HIDE_UNRELEASED').checked;

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

  if (!confirm('Start database build?\\n\\nMovies: ' + fmtNum(movies) + '\\nTV Shows: ' + fmtNum(shows) +
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

async function syncAllUsers() {
  if (!confirm('Sync catalogs for all users? This may take a few minutes.')) return;
  try {
    document.getElementById('btn-sync-users').disabled = true;
    const data = await api('POST', '/admin/api/sync-users');
    toast(`Sync complete: ${data.synced}/${data.total} users, ${data.catalogs} catalogs`, 'success');
  } catch (e) {
    toast('Sync failed: ' + e.message, 'error');
  } finally {
    document.getElementById('btn-sync-users').disabled = false;
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

async function pauseBuild() {
  const btn = document.getElementById('btn-pause-build');
  btn.disabled = true;
  btn.textContent = 'Pausing...';
  try {
    await api('POST', '/admin/api/build/pause');
    toast('Build paused — will pause after current batch finishes', 'success');
    loadBuildStatus();
  } catch (e) {
    toast('Failed to pause: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Pause Build';
  }
}

async function resumeBuild() {
  const btn = document.getElementById('btn-resume-build');
  btn.disabled = true;
  btn.textContent = 'Resuming...';
  try {
    const res = await api('POST', '/admin/api/build/resume');
    toast(res.relaunched ? 'Build re-launched and resumed' : 'Build resumed', 'success');
    loadBuildStatus();
    if (res.relaunched) startBuildPolling();
  } catch (e) {
    toast('Failed to resume: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Resume Build';
  }
}

let buildWs = null;

function startBuildPolling() {
  logOffset = 0;
  document.getElementById('log-viewer').innerHTML = '';
  if (buildPollTimer) clearInterval(buildPollTimer);
  if (logPollTimer) clearInterval(logPollTimer);
  buildPollTimer = setInterval(loadBuildStatus, 5000);

  // Try WebSocket first, fall back to polling
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    buildWs = new WebSocket(proto + '//' + location.host + '/admin/ws/build-logs');
    buildWs.onmessage = function(e) {
      const viewer = document.getElementById('log-viewer');
      const wasScrolled = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
      const safe = e.data.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      viewer.innerHTML += colorLogLine(safe) + '\\n';
      logOffset++;
      document.getElementById('log-count').textContent = '(' + logOffset + ' lines)';
      if (wasScrolled) viewer.scrollTop = viewer.scrollHeight;
    };
    buildWs.onerror = function() { _fallbackToPoll(); };
    buildWs.onclose = function() { buildWs = null; };
  } catch(e) {
    _fallbackToPoll();
  }
}

function _fallbackToPoll() {
  if (buildWs) { try { buildWs.close(); } catch(e){} buildWs = null; }
  if (!logPollTimer) {
    logPollTimer = setInterval(pollLogs, 3000);
    pollLogs();
  }
}

function stopBuildPolling() {
  if (buildPollTimer) { clearInterval(buildPollTimer); buildPollTimer = null; }
  if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; }
  if (buildWs) { try { buildWs.close(); } catch(e){} buildWs = null; }
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
      stopRow.style.display = 'flex';
      logCard.style.display = 'block';

      // Show/hide pause vs resume button
      document.getElementById('btn-pause-build').style.display = s.paused ? 'none' : '';
      document.getElementById('btn-resume-build').style.display = s.paused ? '' : 'none';

      // Ensure log polling is running
      if (!logPollTimer && !s.paused) {
        logPollTimer = setInterval(pollLogs, 3000);
        pollLogs();
      }

      var statusMsg = s.paused
        ? '<div class="alert" style="background:#d2992222;border:1px solid #d2992255;color:#d29922">Build paused — click Resume to continue. Safe to restart container.</div>'
        : '<div class="alert alert-info"><span class="spinner"></span> Build in progress (' + (s.job_type || 'build') + ')</div>';

      el.innerHTML = statusMsg +
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

// ---- Invites ----
async function loadInvites() {
  try {
    const invites = await api('GET', '/admin/api/invites');
    const tbody = document.getElementById('invites-tbody');
    if (invites.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e">No invite codes yet. Generate one above.</td></tr>';
      return;
    }
    tbody.innerHTML = invites.map(function(inv) {
      var status = inv.is_used
        ? '<span class="badge badge-completed">Used</span>'
        : (inv.expires_at && new Date(inv.expires_at + 'Z') < new Date()
          ? '<span class="badge badge-failed">Expired</span>'
          : '<span class="badge badge-running">Active</span>');
      var codeShort = inv.code.length > 16 ? inv.code.substring(0, 16) + '...' : inv.code;
      return '<tr>' +
        '<td style="font-family:monospace;font-size:12px" title="' + inv.code + '">' + codeShort + '</td>' +
        '<td>' + (inv.label || '-') + '</td>' +
        '<td>' + status + '</td>' +
        '<td>' + fmtDate(inv.created_at) + '</td>' +
        '<td>' + (inv.expires_at ? fmtDate(inv.expires_at) : 'Never') + '</td>' +
        '<td>' + (inv.used_by || '-') + '</td>' +
        '<td><button class="btn btn-danger btn-sm" onclick="deleteInvite(\\'' + inv.code + '\\')">Delete</button></td>' +
        '</tr>';
    }).join('');
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

async function createInvite() {
  var label = document.getElementById('invite-label').value.trim();
  var expiresHours = document.getElementById('invite-expires').value;
  var data = { label: label };
  if (expiresHours) data.expires_hours = parseInt(expiresHours);

  try {
    var res = await api('POST', '/admin/api/invites', data);
    document.getElementById('invite-code-display').textContent = res.code;
    document.getElementById('invite-result').style.display = 'block';
    document.getElementById('invite-label').value = '';
    document.getElementById('invite-expires').value = '';
    toast('Invite code generated', 'success');
    loadInvites();
  } catch(e) {
    toast('Failed to generate invite: ' + e.message, 'error');
  }
}

async function deleteInvite(code) {
  if (!confirm('Delete this invite code?')) return;
  try {
    await api('DELETE', '/admin/api/invites/' + encodeURIComponent(code));
    toast('Invite code deleted', 'success');
    loadInvites();
  } catch(e) {
    toast('Failed to delete: ' + e.message, 'error');
  }
}

// ---- Users ----
async function loadUsers() {
  try {
    var users = await api('GET', '/admin/api/users');
    var tbody = document.getElementById('users-tbody');
    if (users.length === 0) {
      tbody.innerHTML = '<tr><td colspan="11" style="color:#8b949e">No users yet. Share invite codes to get started.</td></tr>';
      return;
    }
    tbody.innerHTML = users.map(function(u) {
      var status = u.is_active
        ? '<span class="badge badge-completed">Active</span>'
        : '<span class="badge badge-failed">Disabled</span>';
      var toggleLabel = u.is_active ? 'Disable' : 'Enable';
      var toggleClass = u.is_active ? 'btn-secondary' : 'btn-primary';
      var displayName = u.display_name || u.trakt_username || 'User #' + u.id;
      var sourceBadge = u.auth_source === 'local'
        ? '<span class="badge" style="background:#1f6feb33;color:#58a6ff">Local</span>'
        : '<span class="badge" style="background:#23863533;color:#3fb950">Trakt</span>';
      var tfaBadge = u.totp_enabled
        ? '<span class="badge badge-completed">On</span>'
        : '<span style="color:#8b949e">Off</span>';
      var adminBadge = u.is_admin
        ? '<button class="btn btn-sm" style="background:#238636;color:#fff;padding:2px 8px;font-size:11px" onclick="toggleAdmin(' + u.id + ',false)">Admin</button>'
        : '<button class="btn btn-sm btn-secondary" style="padding:2px 8px;font-size:11px" onclick="toggleAdmin(' + u.id + ',true)">-</button>';
      var bwSelect = '<select onchange="setBandwidth(' + u.id + ',this.value)" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 8px;font-size:12px">' +
        '<option value="high"' + (u.bandwidth_tier === 'high' ? ' selected' : '') + '>High</option>' +
        '<option value="low"' + (u.bandwidth_tier === 'low' ? ' selected' : '') + '>Low</option></select>';
      var actions = '<button class="btn ' + toggleClass + ' btn-sm" onclick="toggleUser(' + u.id + ')" style="margin-right:4px">' + toggleLabel + '</button>';
      if (u.totp_enabled) {
        actions += '<button class="btn btn-secondary btn-sm" onclick="reset2FA(' + u.id + ',\\'' + displayName.replace(/'/g, "\\\\'") + '\\')" style="margin-right:4px">Reset 2FA</button>';
      }
      actions += '<button class="btn btn-danger btn-sm" onclick="deleteUser(' + u.id + ',\\'' + (u.trakt_username || '').replace(/'/g, "\\\\'") + '\\')">Delete</button>';
      return '<tr>' +
        '<td><strong>' + displayName + '</strong>' +
          (u.trakt_user_id ? '<div style="font-size:11px;color:#8b949e">Trakt: ' + u.trakt_user_id + '</div>' : '') +
          '<div style="font-family:monospace;font-size:11px;color:#8b949e">' + u.user_key + '</div>' +
        '</td>' +
        '<td>' + (u.email || '<span style="color:#8b949e">-</span>') + '</td>' +
        '<td>' + sourceBadge + '</td>' +
        '<td>' + tfaBadge + '</td>' +
        '<td>' + adminBadge + '</td>' +
        '<td>' + bwSelect + '</td>' +
        '<td>' + status + '</td>' +
        '<td>' + fmtDate(u.created_at) + '</td>' +
        '<td>' + fmtDate(u.last_login) + '</td>' +
        '<td>' + u.catalog_count + '</td>' +
        '<td style="white-space:nowrap">' + actions + '</td>' +
        '</tr>';
    }).join('');
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

async function toggleUser(userId) {
  try {
    var res = await api('POST', '/admin/api/users/' + userId + '/toggle');
    toast('User ' + (res.is_active ? 'enabled' : 'disabled'), 'success');
    loadUsers();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function deleteUser(userId, username) {
  if (!confirm('Delete user ' + (username || userId) + '? This will remove all their personalized catalogs.')) return;
  try {
    await api('DELETE', '/admin/api/users/' + userId);
    toast('User deleted', 'success');
    loadUsers();
    loadStats();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

// ---- Bandwidth ----
async function setBandwidth(userId, tier) {
  try {
    await api('POST', '/admin/api/users/' + userId + '/bandwidth', { tier: tier });
    toast('Bandwidth set to ' + tier, 'success');
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
    loadUsers();
  }
}

// ---- 2FA Reset ----
async function reset2FA(userId, username) {
  if (!confirm('Reset 2FA for ' + username + '?\\n\\nThis will disable their two-factor authentication. They will need to set it up again.')) return;
  try {
    await api('POST', '/admin/api/users/' + userId + '/reset-2fa');
    toast('2FA reset for ' + username, 'success');
    loadUsers();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function toggleAdmin(userId, makeAdmin) {
  var action = makeAdmin ? 'Promote' : 'Demote';
  if (!confirm(action + ' user #' + userId + ' as admin?')) return;
  try {
    await api('POST', '/admin/api/users/' + userId + '/admin', { is_admin: makeAdmin });
    toast('User ' + (makeAdmin ? 'promoted to' : 'removed from') + ' admin', 'success');
    loadUsers();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

// ---- Sessions ----
async function loadSessions() {
  try {
    var data = await api('GET', '/admin/api/sessions');

    // Web sessions
    var wTbody = document.getElementById('web-sessions-tbody');
    if (data.web_sessions.length === 0) {
      wTbody.innerHTML = '<tr><td colspan="5" style="color:#8b949e">No active web sessions.</td></tr>';
    } else {
      wTbody.innerHTML = data.web_sessions.map(function(s) {
        return '<tr>' +
          '<td><strong>' + s.username + '</strong></td>' +
          '<td style="font-family:monospace;font-size:12px">' + s.token + '</td>' +
          '<td>' + fmtDate(s.created_at) + '</td>' +
          '<td>' + fmtDate(s.expires_at) + '</td>' +
          '<td><button class="btn btn-danger btn-sm" onclick="revokeWebSession(\\'' + s.token_full + '\\',\\'' + s.username.replace(/'/g, "\\\\'") + '\\')">Revoke</button></td>' +
          '</tr>';
      }).join('');
    }

    // Pairing sessions
    var pTbody = document.getElementById('pairing-sessions-tbody');
    if (data.pairing_sessions.length === 0) {
      pTbody.innerHTML = '<tr><td colspan="6" style="color:#8b949e">No active pairing sessions.</td></tr>';
    } else {
      pTbody.innerHTML = data.pairing_sessions.map(function(s) {
        return '<tr>' +
          '<td><strong>' + s.username + '</strong></td>' +
          '<td style="font-family:monospace;font-size:14px;letter-spacing:2px">' + s.short_code + '</td>' +
          '<td style="font-family:monospace;font-size:12px">' + s.token + '</td>' +
          '<td>' + fmtDate(s.created_at) + '</td>' +
          '<td>' + fmtDate(s.expires_at) + '</td>' +
          '<td><button class="btn btn-danger btn-sm" onclick="revokePairingSession(\\'' + s.token_full + '\\',\\'' + s.username.replace(/'/g, "\\\\'") + '\\')">Revoke</button></td>' +
          '</tr>';
      }).join('');
    }
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

async function revokeWebSession(token, username) {
  if (!confirm('Revoke web session for ' + username + '? They will be logged out.')) return;
  try {
    await api('DELETE', '/admin/api/sessions/web/' + encodeURIComponent(token));
    toast('Session revoked for ' + username, 'success');
    loadSessions();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function revokePairingSession(token, username) {
  if (!confirm('Revoke pairing session for ' + username + '?')) return;
  try {
    await api('DELETE', '/admin/api/sessions/pairing/' + encodeURIComponent(token));
    toast('Pairing session revoked', 'success');
    loadSessions();
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

// ---- Active Streams ----
var _streamRefreshTimer = null;

async function loadActiveStreams() {
  try {
    var data = await api('GET', '/admin/api/active-streams');
    var tbody = document.getElementById('active-streams-tbody');
    if (data.streams.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="color:#8b949e">No active streams right now.</td></tr>';
    } else {
      tbody.innerHTML = data.streams.map(function(s) {
        var ago = s.ago_seconds;
        var when = ago < 60 ? ago + 's ago' : Math.floor(ago / 60) + 'm ' + (ago % 60) + 's ago';
        var title = s.title || s.video_id;
        var typeBadge = s.type === 'movie'
          ? '<span style="background:#1a3a5c;color:#58a6ff;padding:2px 8px;border-radius:4px;font-size:11px">movie</span>'
          : '<span style="background:#3a1a5c;color:#a855f7;padding:2px 8px;border-radius:4px;font-size:11px">series</span>';
        var tierBadge = s.tier === 'high'
          ? '<span style="background:#1a3c1a;color:#4caf50;padding:2px 8px;border-radius:4px;font-size:11px">high</span>'
          : '<span style="background:#3c3a1a;color:#ffc107;padding:2px 8px;border-radius:4px;font-size:11px">low</span>';
        return '<tr>' +
          '<td><strong>' + s.user + '</strong></td>' +
          '<td>' + title + '</td>' +
          '<td>' + typeBadge + '</td>' +
          '<td>' + tierBadge + '</td>' +
          '<td style="font-family:monospace;font-size:12px">' + s.video_id + '</td>' +
          '<td>' + when + '</td>' +
          '</tr>';
      }).join('');
    }
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

// Auto-refresh streams tab when visible
document.querySelectorAll('.tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    if (btn.dataset.tab === 'streams') {
      loadActiveStreams();
      if (!_streamRefreshTimer) _streamRefreshTimer = setInterval(loadActiveStreams, 10000);
    } else {
      if (_streamRefreshTimer) { clearInterval(_streamRefreshTimer); _streamRefreshTimer = null; }
    }
  });
});

// ---- AIOStreams ----
async function loadAIOStreams() {
  try {
    var data = await api('GET', '/admin/api/aiostreams-config');
    document.getElementById('set-aio-low-url').value = data.low_bw_url || '';
    document.getElementById('set-aio-high-url').value = data.high_bw_url || '';
  } catch(e) { if (e.message !== 'auth') console.error(e); }
}

async function saveAIOStreams() {
  var data = {
    low_bw_url: document.getElementById('set-aio-low-url').value.trim(),
    high_bw_url: document.getElementById('set-aio-high-url').value.trim(),
  };
  try {
    await api('POST', '/admin/api/aiostreams-config', data);
    toast('AIOStreams config saved', 'success');
  } catch(e) {
    toast('Failed: ' + e.message, 'error');
  }
}

// ---- Init ----
checkAuth();
</script>
</body>
</html>"""
