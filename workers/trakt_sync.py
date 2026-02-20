"""
Trakt sync worker — builds personalized catalogs from Trakt watch history.

Uses the AI tag database for recommendations (not Trakt's /related endpoint).
Flow: Trakt tells us WHAT they watched → tag DB powers the recommendations.

Generates Netflix/Prime/HBO-style catalog rows:
  1. "Up Next"                         (series watched in last 2 weeks)
  2. "Because You Watched [Title]" x5  (tag-based similarity)
  3. "Recommended For You"             (tag-based taste profile)
  4. "Top 10 Today"                    (daily most-watched, ranked 1-10)
  5. "Trending Now"                    (Trakt community)
  6. "Popular"                         (Trakt all-time favorites)

Only digitally-released content — no anticipated/unreleased titles.
"""

import asyncio
from datetime import datetime
from typing import List, Dict, Set

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import User, MovieTag, MediaMetadata
from app.trakt_client import trakt_client
from app.tmdb_client import tmdb_client
from app.catalog_generator import CatalogGenerator

# ---------------------------------------------------------------------------
# Catalog slot ordering — controls row order in Stremio (lower = higher up)
# ---------------------------------------------------------------------------
SLOT_ORDER = {
    "up-next": 0,
    "byw-1": 1,
    "byw-2": 2,
    "byw-3": 3,
    "byw-4": 4,
    "byw-5": 5,
    "rec-movie": 6,
    "rec-series": 7,
    "top10-movie": 8,
    "top10-series": 9,
    "trakt-trending-movie": 10,
    "trakt-trending-series": 11,
    "popular-movie": 12,
    "popular-series": 13,
}


def get_slot_sort_order(slot_id: str) -> int:
    """Return sort order for a catalog slot. Unknown slots go last."""
    return SLOT_ORDER.get(slot_id, 100)


# ---------------------------------------------------------------------------
# Tag-based "Because You Watched" generation
# ---------------------------------------------------------------------------
def find_similar_by_tags(
    db: Session,
    reference_tmdb_id: int,
    media_type: str,
    exclude_ids: Set[int],
    limit: int = 40,
) -> List[int]:
    """
    Find items with similar tags to a reference item.

    Uses the AI tag database — looks up the reference item's tags, then
    finds other items of the same media_type that share the most tags
    with the highest confidence.

    Args:
        db: Database session
        reference_tmdb_id: TMDB ID of the seed item
        media_type: 'movie' or 'tv' — results are filtered to this type
        exclude_ids: TMDB IDs to exclude (e.g. user's watched items)
        limit: Max results

    Returns:
        List of TMDB IDs ranked by tag similarity
    """
    # Get the reference item's tags
    reference_tags = (
        db.query(MovieTag.tag_id, MovieTag.confidence)
        .filter(
            MovieTag.tmdb_id == reference_tmdb_id,
            MovieTag.media_type == media_type,
        )
        .all()
    )

    if not reference_tags:
        logger.debug(f"No tags found for {media_type} {reference_tmdb_id}")
        return []

    tag_ids = [t.tag_id for t in reference_tags]

    # Find items sharing the most tags, same media type, excluding watched
    query = (
        db.query(
            MovieTag.tmdb_id,
            func.count(MovieTag.tag_id).label("matching_tags"),
            func.avg(MovieTag.confidence).label("avg_confidence"),
        )
        .filter(
            MovieTag.tag_id.in_(tag_ids),
            MovieTag.media_type == media_type,
            MovieTag.tmdb_id != reference_tmdb_id,
        )
        .group_by(MovieTag.tmdb_id)
        .order_by(
            func.count(MovieTag.tag_id).desc(),
            func.avg(MovieTag.confidence).desc(),
        )
        .limit(limit + len(exclude_ids))  # over-fetch to account for exclusions
    )

    results = query.all()

    # Filter out excluded IDs and trim to limit
    tmdb_ids = [r.tmdb_id for r in results if r.tmdb_id not in exclude_ids]
    return tmdb_ids[:limit]


def build_taste_profile(
    db: Session,
    watched_tmdb_ids: List[int],
    media_type: str,
    top_n_tags: int = 15,
) -> List[int]:
    """
    Build a user's taste profile from their watched items' tags.

    Aggregates the most frequent + highest confidence tags across all
    watched items to identify the user's preferences.

    Returns:
        List of tag IDs representing the user's taste profile
    """
    if not watched_tmdb_ids:
        return []

    user_tags = (
        db.query(
            MovieTag.tag_id,
            func.avg(MovieTag.confidence).label("avg_conf"),
            func.count().label("cnt"),
        )
        .filter(
            MovieTag.tmdb_id.in_(watched_tmdb_ids),
            MovieTag.media_type == media_type,
        )
        .group_by(MovieTag.tag_id)
        .order_by(func.count().desc(), func.avg(MovieTag.confidence).desc())
        .limit(top_n_tags)
        .all()
    )

    return [t.tag_id for t in user_tags]


def find_recommendations_by_taste(
    db: Session,
    watched_tmdb_ids: Set[int],
    media_type: str,
    top_n_tags: int = 15,
    limit: int = 40,
) -> List[int]:
    """
    Generate tag-based recommendations from a user's taste profile.

    Aggregates the user's most frequent + highest-confidence tags across all
    watched items, then finds unwatched items that best match that profile.

    Args:
        db: Database session
        watched_tmdb_ids: TMDB IDs of items the user has watched
        media_type: 'movie' or 'tv'
        top_n_tags: Number of top tags to use from taste profile
        limit: Max results

    Returns:
        List of TMDB IDs ranked by taste-profile similarity
    """
    taste_tags = build_taste_profile(
        db, list(watched_tmdb_ids), media_type, top_n_tags=top_n_tags
    )
    if not taste_tags:
        logger.debug(f"No taste profile for {media_type} — skipping tag recs")
        return []

    results = (
        db.query(
            MovieTag.tmdb_id,
            func.count(MovieTag.tag_id).label("matching_tags"),
            func.avg(MovieTag.confidence).label("avg_confidence"),
        )
        .filter(
            MovieTag.tag_id.in_(taste_tags),
            MovieTag.media_type == media_type,
            ~MovieTag.tmdb_id.in_(watched_tmdb_ids),
        )
        .group_by(MovieTag.tmdb_id)
        .order_by(
            func.count(MovieTag.tag_id).desc(),
            func.avg(MovieTag.confidence).desc(),
        )
        .limit(limit)
        .all()
    )

    return [r.tmdb_id for r in results]


# ---------------------------------------------------------------------------
# Metadata backfill — ensure TMDB metadata exists for catalog items
# ---------------------------------------------------------------------------
async def _backfill_metadata(
    db: Session,
    tmdb_ids_by_type: Dict[str, Set[int]],
) -> int:
    """Fetch and store MediaMetadata for TMDB IDs that are missing from the DB.

    This is needed because Trakt returns TMDB IDs that may not have been
    processed by initial_build or daily_update.  Without metadata the
    INNER JOIN in get_catalog_content() silently drops those items.

    Returns the number of items backfilled.
    """
    backfilled = 0

    for media_type, all_ids in tmdb_ids_by_type.items():
        if not all_ids:
            continue

        # Find which IDs already have metadata
        existing: Set[int] = set()
        id_list = list(all_ids)
        batch_sz = 500
        for i in range(0, len(id_list), batch_sz):
            batch = id_list[i : i + batch_sz]
            rows = (
                db.query(MediaMetadata.tmdb_id)
                .filter(
                    MediaMetadata.media_type == media_type,
                    MediaMetadata.tmdb_id.in_(batch),
                )
                .all()
            )
            existing.update(r.tmdb_id for r in rows)

        missing = all_ids - existing
        if not missing:
            continue

        logger.info(
            f"Backfilling metadata for {len(missing)} {media_type} items "
            f"({len(existing)} already cached)"
        )

        for tmdb_id in missing:
            try:
                if media_type == "movie":
                    details = await tmdb_client.get_movie(tmdb_id)
                else:
                    details = await tmdb_client.get_tv_show(tmdb_id)

                meta_dict = tmdb_client.extract_metadata(
                    details, media_type  # type: ignore[arg-type]
                )
                db.merge(MediaMetadata(**meta_dict))
                backfilled += 1
            except Exception as e:
                logger.debug(
                    f"Could not fetch metadata for {media_type}/{tmdb_id}: {e}"
                )

        db.flush()

    if backfilled:
        logger.info(f"Backfilled metadata for {backfilled} items total")

    return backfilled


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------
async def sync_user_catalogs(
    user: User,
    db: Session,
    access_token: str,
) -> int:
    """
    Sync a single user's personalized catalogs from Trakt + tag DB.

    Returns the number of catalogs generated.
    """
    catalog_gen = CatalogGenerator(db)
    catalogs_created = 0

    # ------------------------------------------------------------------
    # Step 1: Fetch watch history from Trakt
    # ------------------------------------------------------------------
    logger.info(f"Syncing catalogs for user {user.trakt_username}...")

    history_movies = await trakt_client.get_user_watched_movies(access_token, limit=200)
    history_shows = await trakt_client.get_user_watched_shows(access_token, limit=200)

    watched_movie_ids = set(trakt_client.extract_tmdb_ids(history_movies, "movie"))
    watched_show_ids = set(trakt_client.extract_tmdb_ids(history_shows, "show"))

    logger.info(
        f"User {user.trakt_username}: "
        f"{len(watched_movie_ids)} watched movies, "
        f"{len(watched_show_ids)} watched shows"
    )

    # ------------------------------------------------------------------
    # Step 2: "Up Next" — series actively watched in last 2 weeks
    # ------------------------------------------------------------------
    try:
        recent_shows = await trakt_client.get_recent_show_history(
            access_token, days=14, limit=100
        )
        # Deduplicate by show, preserving recency order
        seen_show_ids: Set[int] = set()
        up_next_ids: List[int] = []
        for item in recent_shows:
            show = item.get("show", {})
            tmdb_id = show.get("ids", {}).get("tmdb")
            if tmdb_id and tmdb_id not in seen_show_ids:
                seen_show_ids.add(tmdb_id)
                up_next_ids.append(tmdb_id)

        if up_next_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id="up-next",
                name="Up Next",
                media_type="tv",
                tmdb_ids=up_next_ids,
                generation_method="up_next",
            )
            catalogs_created += 1
            logger.info(f"  Up Next: {len(up_next_ids)} shows")
    except Exception as e:
        logger.warning(f"  Up Next failed: {e}")

    # ------------------------------------------------------------------
    # Step 3: "Because You Watched [Title]" — tag-based similarity
    # ------------------------------------------------------------------
    # Get RECENT history (ordered by recency) for BYW seeds
    recent_history = await trakt_client.get_user_history(access_token, limit=50)

    byw_seeds = _pick_byw_seeds(
        db, recent_history, watched_movie_ids | watched_show_ids, max_seeds=5
    )

    for i, seed in enumerate(byw_seeds):
        exclude = (
            watched_movie_ids if seed["media_type"] == "movie" else watched_show_ids
        )
        similar_ids = find_similar_by_tags(
            db,
            reference_tmdb_id=seed["tmdb_id"],
            media_type=seed["media_type"],
            exclude_ids=exclude,
            limit=100,
        )

        if similar_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=f"byw-{i + 1}",
                name=f"Because You Watched {seed['title']}",
                media_type=seed["media_type"],
                tmdb_ids=similar_ids,
                generation_method="because_you_watched",
                generation_params={
                    "seed_title": seed["title"],
                    "seed_tmdb_id": seed["tmdb_id"],
                },
            )
            catalogs_created += 1
            logger.info(f"  BYW '{seed['title']}': {len(similar_ids)} items")

    # ------------------------------------------------------------------
    # Step 4: "Recommended For You" — tag-based taste profile
    # ------------------------------------------------------------------
    for media_label, watched_ids, media_type, slot in [
        ("movies", watched_movie_ids, "movie", "rec-movie"),
        ("shows", watched_show_ids, "tv", "rec-series"),
    ]:
        try:
            tmdb_ids = find_recommendations_by_taste(
                db,
                watched_tmdb_ids=watched_ids,
                media_type=media_type,
                limit=100,
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Recommended For You",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="tag_recommendations",
                )
                catalogs_created += 1
                logger.info(f"  Tag recs ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Tag recs ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 5: "Trending Now" — what the Trakt community is watching
    # ------------------------------------------------------------------
    for media_label, fetch_fn, media_type, slot in [
        ("movies", trakt_client.get_trending_movies, "movie", "trakt-trending-movie"),
        ("shows", trakt_client.get_trending_shows, "tv", "trakt-trending-series"),
    ]:
        try:
            items = await fetch_fn(access_token, limit=100)
            tmdb_ids = trakt_client.extract_tmdb_ids(
                items, "movie" if media_type == "movie" else "show"
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Trending Now",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="trakt_trending",
                )
                catalogs_created += 1
                logger.info(f"  Trending ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Trending ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 6: "Top 10 Today" — most watched today (ranked, not shuffled)
    # ------------------------------------------------------------------
    for media_label, fetch_fn, media_type, slot in [
        ("movies", trakt_client.get_watched_daily_movies, "movie", "top10-movie"),
        ("shows", trakt_client.get_watched_daily_shows, "tv", "top10-series"),
    ]:
        try:
            items = await fetch_fn(access_token, limit=10)
            tmdb_ids = trakt_client.extract_tmdb_ids(
                items, "movie" if media_type == "movie" else "show"
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Top 10 Today",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="trakt_top10_daily",
                )
                catalogs_created += 1
                logger.info(f"  Top 10 ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Top 10 ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 7: "Popular" — all-time popular on Trakt
    # ------------------------------------------------------------------
    for media_label, fetch_fn, media_type, slot in [
        ("movies", trakt_client.get_popular_movies, "movie", "popular-movie"),
        ("shows", trakt_client.get_popular_shows, "tv", "popular-series"),
    ]:
        try:
            items = await fetch_fn(access_token, limit=100)
            tmdb_ids = trakt_client.extract_tmdb_ids(
                items, "movie" if media_type == "movie" else "show"
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Popular",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="trakt_popular",
                )
                catalogs_created += 1
                logger.info(f"  Popular ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Popular ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Backfill metadata for any TMDB IDs not yet in MediaMetadata.
    # Without this, get_catalog_content()'s INNER JOIN drops items.
    # ------------------------------------------------------------------
    from app.models import UserCatalog, UserCatalogContent

    user_catalog_ids = [
        c.id
        for c in db.query(UserCatalog.id).filter(UserCatalog.user_id == user.id).all()
    ]
    if user_catalog_ids:
        rows = (
            db.query(UserCatalogContent.tmdb_id, UserCatalogContent.media_type)
            .filter(UserCatalogContent.catalog_id.in_(user_catalog_ids))
            .distinct()
            .all()
        )
        ids_by_type: Dict[str, Set[int]] = {"movie": set(), "tv": set()}
        for r in rows:
            ids_by_type.setdefault(r.media_type, set()).add(r.tmdb_id)
        try:
            await _backfill_metadata(db, ids_by_type)
        except Exception as e:
            logger.warning(f"Metadata backfill had errors: {e}")

    # ------------------------------------------------------------------
    # Done — update sync timestamp
    # ------------------------------------------------------------------
    user.last_sync = datetime.utcnow()
    db.commit()

    logger.info(
        f"Sync complete for {user.trakt_username}: "
        f"{catalogs_created} catalogs generated"
    )
    return catalogs_created


def _pick_byw_seeds(
    db: Session,
    recent_history: List[Dict],
    all_watched_ids: Set[int],
    max_seeds: int = 3,
) -> List[Dict]:
    """
    Pick the best seed items for 'Because You Watched' catalogs.

    Selects recently watched items that exist in our tag database,
    preferring items with more tags (= richer recommendations).
    """
    seen_ids: Set[int] = set()
    seeds: List[Dict] = []

    for item in recent_history:
        movie = item.get("movie")
        show = item.get("show")

        if movie:
            media = movie
            media_type = "movie"
        elif show:
            media = show
            media_type = "tv"
        else:
            continue

        tmdb_id = media.get("ids", {}).get("tmdb")
        if not tmdb_id or tmdb_id in seen_ids:
            continue
        seen_ids.add(tmdb_id)

        # Check if this item has tags in our database
        tag_count = (
            db.query(func.count(MovieTag.tag_id))
            .filter(
                MovieTag.tmdb_id == tmdb_id,
                MovieTag.media_type == media_type,
            )
            .scalar()
        )

        if tag_count and tag_count > 0:
            title = media.get("title") or media.get("name") or "Unknown"
            seeds.append(
                {
                    "title": title,
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "tag_count": tag_count,
                }
            )

        if len(seeds) >= max_seeds:
            break

    logger.info(f"Selected {len(seeds)} BYW seeds: {[s['title'] for s in seeds]}")
    return seeds


# ---------------------------------------------------------------------------
# Bulk sync for all users (called by scheduler)
# ---------------------------------------------------------------------------
async def sync_all_users(db: Session) -> Dict:
    """
    Sync personalized catalogs for all active Trakt users.

    Returns summary stats.
    """
    from app.main import ensure_valid_trakt_token

    users = db.query(User).filter(User.is_active.is_(True)).all()

    logger.info(f"Starting Trakt sync for {len(users)} active users...")

    stats = {"total": len(users), "synced": 0, "failed": 0, "catalogs": 0}

    for user in users:
        try:
            access_token = await ensure_valid_trakt_token(user, db)
            count = await sync_user_catalogs(user, db, access_token)
            stats["synced"] += 1
            stats["catalogs"] += count
        except Exception as e:
            logger.error(f"Sync failed for user {user.trakt_username}: {e}")
            stats["failed"] += 1
            db.rollback()

        # Brief pause between users to avoid hammering Trakt API
        await asyncio.sleep(1)

    logger.info(
        f"Trakt sync complete: {stats['synced']}/{stats['total']} users synced, "
        f"{stats['catalogs']} total catalogs, {stats['failed']} failures"
    )
    return stats
