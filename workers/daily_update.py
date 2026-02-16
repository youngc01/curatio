"""
Daily content update worker.

Fetches new releases from TMDB, tags them with Gemini, and regenerates catalogs.
Designed to run daily via the in-app scheduler or manually.

Usage (manual):
    python workers/daily_update.py
"""

import asyncio
from datetime import datetime
from typing import Tuple

from loguru import logger
from sqlalchemy.orm import Session

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db, init_database  # noqa: E402
from app.models import MediaMetadata, Tag, MovieTag, TaggingJob  # noqa: E402
from app.tmdb_client import tmdb_client  # noqa: E402
from app.gemini_client import gemini_engine  # noqa: E402
from app.catalog_generator import CatalogGenerator  # noqa: E402


def filter_new_items(db: Session, items: list, media_type: str) -> list:
    """
    Filter out items that already exist in the database.

    Args:
        db: Database session
        items: List of TMDB items
        media_type: 'movie' or 'tv'

    Returns:
        List of items not yet in the database
    """
    existing_ids = {
        row.tmdb_id
        for row in db.query(MediaMetadata.tmdb_id).filter(
            MediaMetadata.media_type == media_type
        )
    }

    new_items = [item for item in items if item["id"] not in existing_ids]
    logger.info(
        f"Filtered {media_type}: {len(items)} fetched, "
        f"{len(items) - len(new_items)} already exist, "
        f"{len(new_items)} new"
    )
    return new_items


async def store_metadata(db: Session, items: list, media_type: str) -> None:
    """Store TMDB metadata in database."""
    logger.info(f"Storing metadata for {len(items)} new {media_type} items...")

    for item in items:
        metadata_dict = tmdb_client.extract_metadata(item, media_type)  # type: ignore[arg-type]
        metadata = MediaMetadata(**metadata_dict)
        db.add(metadata)

    db.commit()
    logger.info(f"Stored metadata for {len(items)} items")


async def tag_items_batch(
    db: Session, items: list, media_type: str, batch_size: int = 50
) -> Tuple[int, int]:
    """
    Tag items in batches using Gemini AI.

    Returns:
        Tuple of (processed_count, failed_count)
    """
    logger.info(
        f"Tagging {len(items)} {media_type} items in batches of {batch_size}..."
    )

    total_processed = 0
    total_failed = 0

    # Build tag name -> id map once
    tag_map = {tag.name: tag.id for tag in db.query(Tag).all()}

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]

        try:
            # Prepare items for Gemini
            gemini_items = []
            for item in batch:
                gemini_items.append(
                    {
                        "tmdb_id": item["id"],
                        "media_type": media_type,
                        "title": item.get("title") or item.get("name", "Unknown"),
                        "overview": item.get("overview", ""),
                        "genres": [g["name"] for g in item.get("genres", [])],
                        "release_date": item.get("release_date")
                        or item.get("first_air_date", ""),
                    }
                )

            # Tag with Gemini
            tagged_items = await gemini_engine.tag_items(gemini_items)

            # Store tags using merge() for safe upsert
            for tagged_item in tagged_items:
                tmdb_id = tagged_item["tmdb_id"]
                tags = tagged_item.get("tags", {})

                for tag_name, confidence in tags.items():
                    if tag_name in tag_map:
                        movie_tag = MovieTag(
                            tmdb_id=tmdb_id,
                            tag_id=tag_map[tag_name],
                            confidence=confidence,
                            media_type=media_type,
                        )
                        db.merge(movie_tag)

            db.commit()
            total_processed += len(batch)
            logger.info(f"Progress: {total_processed}/{len(items)} items tagged")

        except Exception as e:
            logger.error(f"Failed to tag batch: {e}")
            db.rollback()
            total_failed += len(batch)
            continue

    logger.info(f"Tagging complete: {total_processed} succeeded, {total_failed} failed")
    return total_processed, total_failed


async def run_daily_update() -> None:
    """
    Run the daily content update.

    Fetches new releases from TMDB, tags them, and regenerates catalogs.
    Can be called from the scheduler or run manually.
    """
    start_time = datetime.utcnow()

    logger.info("=" * 60)
    logger.info("DAILY CONTENT UPDATE")
    logger.info(f"Started at {start_time.isoformat()}")
    logger.info("=" * 60)

    with get_db() as db:
        # Create job record
        job = TaggingJob(
            job_type="daily_update",
            started_at=start_time,
            status="running",
        )
        db.add(job)
        db.commit()

        try:
            total_processed = 0
            total_failed = 0

            # Fetch and process new movies
            logger.info("Fetching new movie releases...")
            new_movies_raw = await tmdb_client.get_new_releases_this_week("movie")
            new_movies = filter_new_items(db, new_movies_raw, "movie")

            if new_movies:
                await store_metadata(db, new_movies, "movie")
                processed, failed = await tag_items_batch(db, new_movies, "movie")
                total_processed += processed
                total_failed += failed

            # Fetch and process new TV shows
            logger.info("Fetching new TV releases...")
            new_shows_raw = await tmdb_client.get_new_releases_this_week("tv")
            new_shows = filter_new_items(db, new_shows_raw, "tv")

            if new_shows:
                await store_metadata(db, new_shows, "tv")
                processed, failed = await tag_items_batch(db, new_shows, "tv")
                total_processed += processed
                total_failed += failed

            # Regenerate all universal catalogs (free - SQL only)
            logger.info("Regenerating universal catalogs...")
            generator = CatalogGenerator(db)
            generator.regenerate_all_universal_catalogs()

            # Update job record
            job.completed_at = datetime.utcnow()  # type: ignore[assignment]
            job.status = "completed"  # type: ignore[assignment]
            job.items_processed = total_processed  # type: ignore[assignment]
            job.items_failed = total_failed  # type: ignore[assignment]
            job.job_metadata = {  # type: ignore[assignment]
                "new_movies_found": len(new_movies_raw),
                "new_movies_added": len(new_movies),
                "new_shows_found": len(new_shows_raw),
                "new_shows_added": len(new_shows),
            }
            db.commit()

            duration = (datetime.utcnow() - start_time).total_seconds()

            logger.info("=" * 60)
            logger.info("DAILY UPDATE COMPLETE")
            logger.info(f"New movies: {len(new_movies)} tagged")
            logger.info(f"New TV shows: {len(new_shows)} tagged")
            logger.info(f"Total processed: {total_processed}")
            logger.info(f"Failed: {total_failed}")
            logger.info(f"Duration: {duration:.1f}s")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"Daily update failed: {e}")
            job.status = "failed"  # type: ignore[assignment]
            job.error_message = str(e)  # type: ignore[assignment]
            job.completed_at = datetime.utcnow()  # type: ignore[assignment]
            db.commit()
            raise


if __name__ == "__main__":
    init_database()
    asyncio.run(run_daily_update())
