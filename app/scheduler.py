"""
Asyncio-based daily scheduler for content updates.

Runs as a background task inside the FastAPI app.
Controlled by DAILY_UPDATE_ENABLED and DAILY_UPDATE_TIME env vars.
"""

import asyncio
from datetime import datetime, timedelta

from loguru import logger

from app.config import settings


def parse_time(time_str: str) -> tuple[int, int]:
    """
    Parse HH:MM time string into (hour, minute) tuple.

    Args:
        time_str: Time in HH:MM format (e.g., "03:00")

    Returns:
        Tuple of (hour, minute)

    Raises:
        ValueError: If format is invalid
    """
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{time_str}', expected HH:MM")

    hour = int(parts[0])
    minute = int(parts[1])

    if not (0 <= hour <= 23):
        raise ValueError(f"Hour must be 0-23, got {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"Minute must be 0-59, got {minute}")

    return hour, minute


def seconds_until(hour: int, minute: int) -> float:
    """
    Calculate seconds from now until the next occurrence of HH:MM UTC.

    If the target time has already passed today, returns seconds until
    that time tomorrow.

    Args:
        hour: Target hour (0-23)
        minute: Target minute (0-59)

    Returns:
        Seconds until next occurrence
    """
    now = datetime.utcnow()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= now:
        # Already passed today, schedule for tomorrow
        target += timedelta(days=1)

    return (target - now).total_seconds()


async def run_scheduler() -> None:
    """
    Main scheduler loop. Runs daily at the configured time.

    Sleeps until DAILY_UPDATE_TIME, runs the update, then repeats.
    Never raises — logs errors and continues.
    """
    # Import here to avoid circular imports
    from workers.daily_update import run_daily_update

    hour, minute = parse_time(settings.daily_update_time)

    logger.info(
        f"Daily update scheduler started. "
        f"Updates will run at {hour:02d}:{minute:02d} UTC"
    )

    while True:
        try:
            wait_seconds = seconds_until(hour, minute)
            next_run = datetime.utcnow() + timedelta(seconds=wait_seconds)

            logger.info(
                f"Next daily update scheduled for {next_run.isoformat()} UTC "
                f"({wait_seconds / 3600:.1f} hours from now)"
            )

            await asyncio.sleep(wait_seconds)

            logger.info("Starting scheduled daily update...")
            await run_daily_update()

            # Sync personalized Trakt catalogs for all users
            if settings.enable_trakt_sync:
                logger.info("Starting Trakt catalog sync for all users...")
                try:
                    from workers.trakt_sync import sync_all_users
                    from app.database import get_db

                    with get_db() as db:
                        await sync_all_users(db)
                except Exception as e:
                    logger.error(f"Trakt sync failed: {e}")

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled, shutting down")
            break
        except Exception as e:
            logger.error(f"Daily update failed: {e}")
            # Wait 1 hour before retrying on failure
            logger.info("Retrying in 1 hour...")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                logger.info("Scheduler cancelled during retry wait")
                break
