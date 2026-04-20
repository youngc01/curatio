"""
Trakt sync worker — builds personalized catalogs from Trakt watch history.

Uses the AI tag database for recommendations (not Trakt's /related endpoint).
Flow: Trakt tells us WHAT they watched → tag DB powers the recommendations.

Generates Netflix/Prime/HBO-style catalog rows:
  1. "Up Next"                         (series watched in last 2 weeks)
  2. "Trending Now"                    (TMDB daily trending)
  3. "New Releases"                    (last 90 days by popularity)
  4. "Because You Watched [Title]" x5  (tag-based similarity)
  5. "Top Picks / Recommended For You" (tag-based taste profile)
  6. "Genre/Mood For You" x4           (taste-specific catalogs)
  7. "Hidden Gems For You"             (high-quality low-popularity)

Only digitally-released content — no anticipated/unreleased titles.
"""

import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Set

from loguru import logger
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

# Add parent directory to path
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import User, MovieTag, MediaMetadata, WatchEvent, Tag  # noqa: E402
from app.models import UniversalCategory, UniversalCatalogContent  # noqa: E402
from app.trakt_client import trakt_client  # noqa: E402
from app.tmdb_client import tmdb_client  # noqa: E402
from app.catalog_generator import CatalogGenerator  # noqa: E402

# ---------------------------------------------------------------------------
# Catalog slot ordering — controls row order in Stremio (lower = higher up)
# ---------------------------------------------------------------------------
SLOT_ORDER = {
    "up-next": 0,
    "trending-movie": 1,
    "trending-series": 2,
    "new-releases-movie": 3,
    "new-releases-series": 4,
    "popular-movie": 5,
    "popular-series": 6,
    "discover-movie": 7,
    "discover-series": 8,
    "discover-acclaimed": 9,
    "byw-1": 10,
    "byw-2": 11,
    "byw-3": 12,
    "byw-4": 13,
    "byw-5": 14,
    "picks-movie": 15,
    "picks-series": 16,
    "rec-movie": 17,
    "rec-series": 18,
    "taste-0": 19,
    "taste-1": 20,
    "taste-2": 21,
    "taste-3": 22,
    "gems-movie": 23,
    "gems-series": 24,
}


def get_slot_sort_order(slot_id: str) -> int:
    """Return sort order for a catalog slot. Unknown slots go last."""
    return SLOT_ORDER.get(slot_id, 100)


# Valid slot IDs — used to clean up stale catalogs from previous code versions
VALID_SLOTS = set(SLOT_ORDER.keys())


def _cleanup_stale_catalogs(user_id: int, db: Session) -> int:
    """Delete user catalogs with slot IDs that are no longer in SLOT_ORDER.

    Returns the number of catalogs removed.
    """
    from app.models import UserCatalog, UserCatalogContent

    stale = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user_id,
            UserCatalog.slot_id.notin_(VALID_SLOTS),
        )
        .all()
    )
    if not stale:
        return 0

    stale_ids = [c.id for c in stale]
    slot_names = [c.slot_id for c in stale]
    db.query(UserCatalogContent).filter(
        UserCatalogContent.catalog_id.in_(stale_ids)
    ).delete(synchronize_session=False)
    db.query(UserCatalog).filter(UserCatalog.id.in_(stale_ids)).delete(
        synchronize_session=False
    )
    db.flush()
    logger.info(
        f"  Cleaned up {len(stale)} stale catalogs for user {user_id}: {slot_names}"
    )
    return len(stale)


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
                existing_meta = (
                    db.query(MediaMetadata)
                    .filter(
                        MediaMetadata.tmdb_id == meta_dict["tmdb_id"],
                        MediaMetadata.media_type == meta_dict["media_type"],
                    )
                    .first()
                )
                if existing_meta:
                    for key, val in meta_dict.items():
                        if val is not None:
                            setattr(existing_meta, key, val)
                else:
                    db.add(MediaMetadata(**meta_dict))
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
    common_data: "dict[str, list[int]] | None" = None,
) -> int:
    """
    Sync a single user's personalized catalogs from Trakt + tag DB.

    Returns the number of catalogs generated.
    """
    catalog_gen = CatalogGenerator(db)
    catalogs_created = 0

    # Clean up stale catalogs from previous code versions
    _cleanup_stale_catalogs(user.id, db)

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
    # Steps 5-7: Trending/New Releases/Popular + backfill + commit
    # ------------------------------------------------------------------
    return await _generate_common_catalogs(
        user,
        db,
        catalog_gen,
        catalogs_created,
        user.trakt_username or "trakt-user",
        common_data=common_data,
    )


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

        candidates.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": media.get("title") or media.get("name") or "Unknown",
            }
        )

    # Batch tag-count query instead of per-item queries
    if candidates:
        candidate_pairs = [(c["tmdb_id"], c["media_type"]) for c in candidates]
        tag_counts_rows = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type, func.count(MovieTag.tag_id))
            .filter(tuple_(MovieTag.tmdb_id, MovieTag.media_type).in_(candidate_pairs))
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .all()
        )
        tag_counts = {(r[0], r[1]): r[2] for r in tag_counts_rows}

        for c in candidates:
            tc = tag_counts.get((c["tmdb_id"], c["media_type"]), 0)
            if tc > 0:
                seeds.append(
                    {
                        "title": c["title"],
                        "tmdb_id": c["tmdb_id"],
                        "media_type": c["media_type"],
                        "tag_count": tc,
                    }
                )
                if len(seeds) >= max_seeds:
                    break

    logger.info(f"Selected {len(seeds)} BYW seeds: {[s['title'] for s in seeds]}")
    return seeds


# ---------------------------------------------------------------------------
# Local (scrobble) sync — builds catalogs from WatchEvent table
# ---------------------------------------------------------------------------


def _pick_byw_seeds_local(
    db: Session,
    user_id: int,
    all_watched_ids: Set[int],
    max_seeds: int = 5,
) -> List[Dict]:
    """Pick BYW seed items from local watch events (completions + imports)."""

    recent = (
        db.query(
            WatchEvent.tmdb_id,
            WatchEvent.media_type,
            WatchEvent.title,
            func.max(WatchEvent.created_at).label("last_watched"),
        )
        .filter(
            WatchEvent.user_id == user_id,
            WatchEvent.action.in_(["complete", "imported"]),
        )
        .group_by(WatchEvent.tmdb_id, WatchEvent.media_type, WatchEvent.title)
        .order_by(func.max(WatchEvent.created_at).desc())
        .limit(50)
        .all()
    )

    seen_ids: Set[int] = set()
    seeds: List[Dict] = []
    candidates: List[Dict] = []

    for row in recent:
        if row.tmdb_id in seen_ids:
            continue
        seen_ids.add(row.tmdb_id)
        candidates.append(
            {
                "tmdb_id": row.tmdb_id,
                "media_type": row.media_type,
                "title": row.title,
            }
        )

    if candidates:
        # Batch tag-count query
        candidate_pairs = [(c["tmdb_id"], c["media_type"]) for c in candidates]
        tag_counts_rows = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type, func.count(MovieTag.tag_id))
            .filter(tuple_(MovieTag.tmdb_id, MovieTag.media_type).in_(candidate_pairs))
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .all()
        )
        tag_counts = {(r[0], r[1]): r[2] for r in tag_counts_rows}

        # Batch title lookup for items missing WatchEvent.title
        missing_title_pairs = [
            (c["tmdb_id"], c["media_type"])
            for c in candidates
            if not c["title"] and tag_counts.get((c["tmdb_id"], c["media_type"]), 0) > 0
        ]
        title_map: dict = {}
        if missing_title_pairs:
            title_rows = (
                db.query(
                    MediaMetadata.tmdb_id, MediaMetadata.media_type, MediaMetadata.title
                )
                .filter(
                    tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(
                        missing_title_pairs
                    )
                )
                .all()
            )
            title_map = {(r.tmdb_id, r.media_type): r.title for r in title_rows}

        for c in candidates:
            tc = tag_counts.get((c["tmdb_id"], c["media_type"]), 0)
            if tc > 0:
                title = c["title"] or title_map.get(
                    (c["tmdb_id"], c["media_type"]), "Unknown"
                )
                seeds.append(
                    {
                        "title": title,
                        "tmdb_id": c["tmdb_id"],
                        "media_type": c["media_type"],
                        "tag_count": tc,
                    }
                )
                if len(seeds) >= max_seeds:
                    break

    logger.info(f"Selected {len(seeds)} local BYW seeds: {[s['title'] for s in seeds]}")
    return seeds


def _find_hidden_gems_by_taste(
    db: Session,
    taste_tag_ids: List[int],
    media_type: str,
    exclude_ids: Set[int],
    limit: int = 100,
) -> List[int]:
    """Find high-quality, low-popularity items matching the user's taste."""
    if not taste_tag_ids:
        return []

    results = (
        db.query(
            MovieTag.tmdb_id,
            func.count(func.distinct(MovieTag.tag_id)).label("tag_hits"),
            func.avg(MovieTag.confidence).label("avg_conf"),
        )
        .join(
            MediaMetadata,
            (MovieTag.tmdb_id == MediaMetadata.tmdb_id)
            & (MovieTag.media_type == MediaMetadata.media_type),
        )
        .filter(
            MovieTag.tag_id.in_(taste_tag_ids),
            MovieTag.media_type == media_type,
            MediaMetadata.vote_average >= 7.0,
            MediaMetadata.vote_count >= 100,
            MediaMetadata.popularity < 30,
        )
        .group_by(MovieTag.tmdb_id)
        .order_by(
            func.count(func.distinct(MovieTag.tag_id)).desc(),
            func.avg(MovieTag.confidence).desc(),
        )
        .limit(limit + len(exclude_ids))
        .all()
    )

    return [r.tmdb_id for r in results if r.tmdb_id not in exclude_ids][:limit]


def _find_genre_taste_catalogs(
    db: Session,
    all_watched_ids: Set[int],
    taste_tag_ids: List[int],
    exclude_ids: Set[int],
    max_catalogs: int = 4,
    limit: int = 100,
) -> List[Dict]:
    """Generate genre/mood-specific taste catalogs.

    Returns a list of dicts with keys: slot_id, name, media_type, tmdb_ids.
    """
    if not taste_tag_ids or not all_watched_ids:
        return []

    # Find user's top genre and mood tags from their watched items
    top_tags = (
        db.query(
            Tag.id,
            Tag.name,
            Tag.category,
            func.count().label("cnt"),
        )
        .join(MovieTag, MovieTag.tag_id == Tag.id)
        .filter(
            MovieTag.tmdb_id.in_(all_watched_ids),
            Tag.category.in_(["genre", "mood"]),
        )
        .group_by(Tag.id, Tag.name, Tag.category)
        .order_by(func.count().desc())
        .limit(10)
        .all()
    )

    # Pick top 2 genre + top 2 mood tags
    genres = [t for t in top_tags if t.category == "genre"][:2]
    moods = [t for t in top_tags if t.category == "mood"][:2]
    selected = genres + moods

    catalogs: List[Dict] = []
    taste_set = set(taste_tag_ids)

    for i, tag_row in enumerate(selected[:max_catalogs]):
        # Find items with this specific tag + at least 1 other taste tag
        results = (
            db.query(
                MovieTag.tmdb_id,
                MovieTag.media_type,
                func.count(func.distinct(MovieTag.tag_id)).label("tag_hits"),
                func.avg(MovieTag.confidence).label("avg_conf"),
            )
            .filter(
                MovieTag.tag_id.in_(taste_set | {tag_row.id}),
                MovieTag.tmdb_id.notin_(exclude_ids),
            )
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .having(func.count(func.distinct(MovieTag.tag_id)) >= 2)
            .order_by(
                func.count(func.distinct(MovieTag.tag_id)).desc(),
                func.avg(MovieTag.confidence).desc(),
            )
            .limit(limit)
            .all()
        )

        if not results:
            continue

        # Determine dominant media type from results
        movie_count = sum(1 for r in results if r.media_type == "movie")
        tv_count = sum(1 for r in results if r.media_type == "tv")
        media_type = "movie" if movie_count >= tv_count else "tv"

        tmdb_ids = [r.tmdb_id for r in results if r.media_type == media_type]
        if not tmdb_ids:
            tmdb_ids = [r.tmdb_id for r in results][:limit]

        catalogs.append(
            {
                "slot_id": f"taste-{i}",
                "name": f"{tag_row.name} For You",
                "media_type": media_type,
                "tmdb_ids": tmdb_ids[:limit],
            }
        )

    return catalogs


async def _save_metadata_from_results(
    db: Session, results: list, media_type: str
) -> list[int]:
    """Extract IDs and save metadata from TMDB list results.

    Filters to English-language only. Uses selective non-null updates so
    richer detail-level fields (imdb_id, logo) from prior backfills aren't
    overwritten with None from list responses.
    """
    tmdb_ids: list[int] = []
    for r in results:
        if not r.get("id"):
            continue
        tid = r["id"]
        if tid in tmdb_ids:
            continue
        if r.get("original_language") != "en":
            continue
        tmdb_ids.append(tid)
        try:
            meta_dict = tmdb_client.extract_metadata(
                r, media_type  # type: ignore[arg-type]
            )
            existing = (
                db.query(MediaMetadata)
                .filter(
                    MediaMetadata.tmdb_id == tid,
                    MediaMetadata.media_type == media_type,
                )
                .first()
            )
            if existing:
                for key, val in meta_dict.items():
                    if val is not None:
                        setattr(existing, key, val)
            else:
                db.add(MediaMetadata(**meta_dict))
        except Exception as e:
            logger.debug(f"Metadata save failed for {media_type}/{tid}: {e}")
    db.flush()
    return tmdb_ids


async def _filter_home_released_movies(tmdb_ids: list[int]) -> list[int]:
    """Post-filter movies to those with confirmed US digital or physical release.

    Calls /movie/{id}/release_dates concurrently (max 10 in-flight) and keeps
    only IDs that have release type 4 (digital) or 5 (physical) for the US.
    On any per-item error the movie is kept (fail-open) to avoid over-filtering.
    """
    if not tmdb_ids:
        return tmdb_ids

    sem = asyncio.Semaphore(10)

    async def check(tmdb_id: int) -> tuple[int, bool]:
        async with sem:
            try:
                data = await tmdb_client.get_movie_release_dates(tmdb_id)
                return tmdb_id, tmdb_client.has_home_release(data)
            except Exception as e:
                logger.debug(f"Release date check skipped for movie/{tmdb_id}: {e}")
                return tmdb_id, True  # fail-open: include on error

    checks = await asyncio.gather(*[check(tid) for tid in tmdb_ids])
    valid = {tid for tid, ok in checks if ok}
    filtered = [tid for tid in tmdb_ids if tid in valid]
    excluded = len(tmdb_ids) - len(filtered)
    if excluded:
        logger.info(
            f"Home release post-filter removed {excluded}/{len(tmdb_ids)} theatrical-only movies"
        )
    return filtered


async def _prefetch_common_catalog_data(db: Session) -> dict[str, list[int]]:
    """Fetch trending/new releases/popular from TMDB once for all users.

    Called once at the start of a bulk sync and shared across all users so
    we make ~18 TMDB requests total instead of 18 × N (one set per user).
    Returns a dict mapping slot_id → ordered list of tmdb_ids, with
    MediaMetadata already saved/updated in the database.
    """
    result: dict[str, list[int]] = {}
    cutoff_90d = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Trending (day + week windows, 3 pages each)
    for media_type, slot in [("movie", "trending-movie"), ("tv", "trending-series")]:
        try:
            all_results: list = []
            for time_window in ["day", "week"]:
                for page in range(1, 4):
                    data = (
                        await tmdb_client.get_trending_movies(time_window, page=page)
                        if media_type == "movie"
                        else await tmdb_client.get_trending_tv_shows(
                            time_window, page=page
                        )
                    )
                    all_results.extend(data.get("results", []))
            result[slot] = await _save_metadata_from_results(
                db, all_results, media_type
            )
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    # New Releases — last 90 days, US digital release
    for media_type, slot in [
        ("movie", "new-releases-movie"),
        ("tv", "new-releases-series"),
    ]:
        try:
            all_results = []
            for page in range(1, 4):
                if media_type == "movie":
                    data = await tmdb_client._request(
                        "/discover/movie",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "release_date.gte": cutoff_90d,
                            "release_date.lte": today,
                            "region": "US",
                            "with_release_type": "4|5",
                            "with_original_language": "en",
                            "without_keywords": "210024",
                            "include_adult": False,
                        },
                    )
                else:
                    data = await tmdb_client._request(
                        "/discover/tv",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "first_air_date.gte": cutoff_90d,
                            "first_air_date.lte": today,
                            "with_original_language": "en",
                            "without_keywords": "210024",
                            "include_adult": False,
                        },
                    )
                all_results.extend(data.get("results", []))
            ids = await _save_metadata_from_results(db, all_results, media_type)
            if media_type == "movie":
                ids = await _filter_home_released_movies(ids)
            result[slot] = ids
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    # Popular
    for media_type, slot in [("movie", "popular-movie"), ("tv", "popular-series")]:
        try:
            all_results = []
            for page in range(1, 4):
                if media_type == "movie":
                    data = await tmdb_client._request(
                        "/discover/movie",
                        params={
                            "page": page,
                            "sort_by": "popularity.desc",
                            "region": "US",
                            "with_release_type": "4|5",
                            "with_original_language": "en",
                            "without_keywords": "210024",
                            "include_adult": False,
                        },
                    )
                else:
                    data = await tmdb_client.get_popular_tv_shows(page)
                all_results.extend(data.get("results", []))
            ids = await _save_metadata_from_results(db, all_results, media_type)
            if media_type == "movie":
                ids = await _filter_home_released_movies(ids)
            result[slot] = ids
            logger.info(f"Prefetched {slot}: {len(result[slot])} items")
        except Exception as e:
            logger.warning(f"Prefetch {slot} failed: {e}")
            result[slot] = []

    db.commit()
    return result


async def _generate_common_catalogs(
    user: User,
    db: Session,
    catalog_gen: CatalogGenerator,
    catalogs_created: int,
    display: str,
    common_data: "dict[str, list[int]] | None" = None,
) -> int:
    """Write pre-fetched common catalog data to a user's rows, then backfill and commit.

    common_data comes from _prefetch_common_catalog_data() which is called
    once per bulk sync run. If None (e.g. single-user refresh), fetches inline.
    """
    from app.models import UserCatalog, UserCatalogContent

    if common_data is None:
        common_data = await _prefetch_common_catalog_data(db)

    catalog_configs = [
        ("trending-movie", "Trending Now", "movie", "movies", "tmdb_trending"),
        ("trending-series", "Trending Now", "tv", "shows", "tmdb_trending"),
        ("new-releases-movie", "New Releases", "movie", "movies", "tmdb_new_releases"),
        ("new-releases-series", "New Releases", "tv", "shows", "tmdb_new_releases"),
        ("popular-movie", "Popular", "movie", "movies", "tmdb_popular"),
        ("popular-series", "Popular", "tv", "shows", "tmdb_popular"),
    ]
    for slot, name, media_type, media_label, gen_method in catalog_configs:
        tmdb_ids = common_data.get(slot, [])
        if tmdb_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=slot,
                name=name,
                media_type=media_type,
                tmdb_ids=tmdb_ids,
                generation_method=gen_method,
            )
            catalogs_created += 1
            logger.info(f"  {name} ({media_label}): {len(tmdb_ids)} items")

    # --- Backfill metadata for other user catalog items (BYW, recs, etc.) ---
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

    user.last_sync = datetime.utcnow()
    db.commit()

    logger.info(
        f"Local sync complete for {display}: {catalogs_created} catalogs generated"
    )
    return catalogs_created


async def _generate_discovery_catalogs(
    user: User,
    db: Session,
    catalog_gen: CatalogGenerator,
    display: str,
) -> int:
    """Generate discovery catalogs for users with no watch history.

    Creates daily-rotating catalogs from universal content + quality picks.
    """
    import hashlib
    import random as _random
    from datetime import date

    catalogs_created = 0
    daily_seed = int(hashlib.md5(date.today().isoformat().encode()).hexdigest()[:8], 16)

    # "Discover Today" — sample from random universal categories, rotated daily
    for media_type, slot in [("movie", "discover-movie"), ("tv", "discover-series")]:
        try:
            categories = (
                db.query(UniversalCategory.id)
                .filter(
                    UniversalCategory.is_active.is_(True),
                    UniversalCategory.media_type == media_type,
                )
                .all()
            )
            cat_ids = [c.id for c in categories]
            if cat_ids:
                rng = _random.Random(daily_seed + hash(media_type))
                picked = rng.sample(cat_ids, min(5, len(cat_ids)))
                tmdb_ids_set: set[int] = set()
                for cid in picked:
                    rows = (
                        db.query(UniversalCatalogContent.tmdb_id)
                        .filter(UniversalCatalogContent.category_id == cid)
                        .order_by(UniversalCatalogContent.rank)
                        .limit(40)
                        .all()
                    )
                    tmdb_ids_set.update(r.tmdb_id for r in rows)

                tmdb_ids = list(tmdb_ids_set)
                rng.shuffle(tmdb_ids)
                tmdb_ids = tmdb_ids[:100]

                if tmdb_ids:
                    catalog_gen.save_user_catalog(
                        user_id=user.id,
                        slot_id=slot,
                        name="Discover Today",
                        media_type=media_type,
                        tmdb_ids=tmdb_ids,
                        generation_method="discovery_daily",
                    )
                    catalogs_created += 1
                    logger.info(
                        f"  Discover Today ({media_type}): {len(tmdb_ids)} items"
                    )
        except Exception as e:
            logger.warning(f"  Discover Today ({media_type}) failed: {e}")

    # "Critically Acclaimed" — high-rated English films, shuffled daily
    try:
        acclaimed = (
            db.query(MediaMetadata.tmdb_id)
            .filter(
                MediaMetadata.vote_average >= 7.5,
                MediaMetadata.vote_count >= 1000,
                MediaMetadata.original_language == "en",
            )
            .order_by(MediaMetadata.vote_average.desc())
            .limit(200)
            .all()
        )
        tmdb_ids = [r.tmdb_id for r in acclaimed]
        _random.Random(daily_seed).shuffle(tmdb_ids)
        tmdb_ids = tmdb_ids[:100]
        if tmdb_ids:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id="discover-acclaimed",
                name="Critically Acclaimed",
                media_type="movie",
                tmdb_ids=tmdb_ids,
                generation_method="discovery_acclaimed",
            )
            catalogs_created += 1
            logger.info(f"  Critically Acclaimed: {len(tmdb_ids)} items")
    except Exception as e:
        logger.warning(f"  Critically Acclaimed failed: {e}")

    logger.info(f"  Generated {catalogs_created} discovery catalogs for {display}")
    return catalogs_created


async def sync_local_user_catalogs(
    user: User,
    db: Session,
    common_data: "dict[str, list[int]] | None" = None,
) -> int:
    """Build personalized catalogs from local watch events (no Trakt).

    Mirrors sync_user_catalogs() but reads WatchEvent instead of Trakt API.
    """

    catalog_gen = CatalogGenerator(db)
    catalogs_created = 0
    display = user.display_name or user.user_key[:8]

    # Clean up stale catalogs from previous code versions
    _cleanup_stale_catalogs(user.id, db)

    logger.info(f"Syncing local catalogs for user {display}...")

    # ------------------------------------------------------------------
    # Step 1: Derive watched items from WatchEvent (action='complete')
    # ------------------------------------------------------------------
    completed = (
        db.query(WatchEvent.tmdb_id, WatchEvent.media_type)
        .filter(WatchEvent.user_id == user.id, WatchEvent.action == "complete")
        .distinct()
        .all()
    )

    watched_movie_ids: Set[int] = {
        r.tmdb_id for r in completed if r.media_type == "movie"
    }
    watched_show_ids: Set[int] = {r.tmdb_id for r in completed if r.media_type == "tv"}

    # Include imported history for recommendation building (not for Up Next)
    imported = (
        db.query(WatchEvent.tmdb_id, WatchEvent.media_type)
        .filter(WatchEvent.user_id == user.id, WatchEvent.action == "imported")
        .distinct()
        .all()
    )
    all_movie_ids: Set[int] = watched_movie_ids | {
        r.tmdb_id for r in imported if r.media_type == "movie"
    }
    all_show_ids: Set[int] = watched_show_ids | {
        r.tmdb_id for r in imported if r.media_type == "tv"
    }

    logger.info(
        f"User {display}: "
        f"{len(all_movie_ids)} movies ({len(watched_movie_ids)} watched + "
        f"{len(all_movie_ids) - len(watched_movie_ids)} imported), "
        f"{len(all_show_ids)} shows ({len(watched_show_ids)} watched + "
        f"{len(all_show_ids) - len(watched_show_ids)} imported)"
    )

    has_history = bool(all_movie_ids or all_show_ids)

    if not has_history:
        # No watch history at all — generate discovery catalogs instead
        catalogs_created = await _generate_discovery_catalogs(
            user, db, catalog_gen, display
        )
        # Still generate trending + new releases below
        return await _generate_common_catalogs(
            user, db, catalog_gen, catalogs_created, display, common_data=common_data
        )

    # ------------------------------------------------------------------
    # Step 2: "Up Next" — TV shows with recent completions (last 14 days)
    # ------------------------------------------------------------------
    try:
        cutoff = datetime.utcnow() - timedelta(days=14)
        recent_tv = (
            db.query(
                WatchEvent.tmdb_id,
                func.max(WatchEvent.created_at).label("last_watched"),
            )
            .filter(
                WatchEvent.user_id == user.id,
                WatchEvent.media_type == "tv",
                WatchEvent.action == "complete",
                WatchEvent.created_at >= cutoff,
            )
            .group_by(WatchEvent.tmdb_id)
            .order_by(func.max(WatchEvent.created_at).desc())
            .all()
        )

        up_next_ids = [r.tmdb_id for r in recent_tv]

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
    byw_seeds = _pick_byw_seeds_local(
        db, user.id, all_movie_ids | all_show_ids, max_seeds=5
    )

    for i, seed in enumerate(byw_seeds):
        exclude = all_movie_ids if seed["media_type"] == "movie" else all_show_ids
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
        ("movies", all_movie_ids, "movie", "rec-movie"),
        ("shows", all_show_ids, "tv", "rec-series"),
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
    # Step 4a: "Top Picks For You" — best taste matches (broader tags)
    # ------------------------------------------------------------------
    for media_label, watched_ids, media_type, slot in [
        ("movies", all_movie_ids, "movie", "picks-movie"),
        ("shows", all_show_ids, "tv", "picks-series"),
    ]:
        try:
            tmdb_ids = find_recommendations_by_taste(
                db,
                watched_tmdb_ids=watched_ids,
                media_type=media_type,
                top_n_tags=20,
                limit=100,
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Top Picks For You",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="top_picks",
                )
                catalogs_created += 1
                logger.info(f"  Top Picks ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Top Picks ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Step 4b: Genre/mood taste catalogs — "Sci-Fi For You", etc.
    # ------------------------------------------------------------------
    taste_tag_ids = build_taste_profile(
        db, list(all_movie_ids | all_show_ids), "movie", top_n_tags=15
    ) + build_taste_profile(db, list(all_movie_ids | all_show_ids), "tv", top_n_tags=15)
    # Deduplicate while preserving order
    seen_tags: set[int] = set()
    unique_taste_tags: list[int] = []
    for tid in taste_tag_ids:
        if tid not in seen_tags:
            seen_tags.add(tid)
            unique_taste_tags.append(tid)
    taste_tag_ids = unique_taste_tags

    try:
        taste_catalogs = _find_genre_taste_catalogs(
            db,
            all_watched_ids=all_movie_ids | all_show_ids,
            taste_tag_ids=taste_tag_ids,
            exclude_ids=all_movie_ids | all_show_ids,
            max_catalogs=4,
            limit=100,
        )
        for tc in taste_catalogs:
            catalog_gen.save_user_catalog(
                user_id=user.id,
                slot_id=tc["slot_id"],
                name=tc["name"],
                media_type=tc["media_type"],
                tmdb_ids=tc["tmdb_ids"],
                generation_method="taste_genre",
            )
            catalogs_created += 1
            logger.info(f"  Taste '{tc['name']}': {len(tc['tmdb_ids'])} items")
    except Exception as e:
        logger.warning(f"  Taste catalogs failed: {e}")

    # ------------------------------------------------------------------
    # Step 4c: "Hidden Gems For You" — high quality, low popularity
    # ------------------------------------------------------------------
    for media_label, media_type, slot in [
        ("movies", "movie", "gems-movie"),
        ("shows", "tv", "gems-series"),
    ]:
        try:
            tmdb_ids = _find_hidden_gems_by_taste(
                db,
                taste_tag_ids=taste_tag_ids,
                media_type=media_type,
                exclude_ids=all_movie_ids | all_show_ids,
                limit=100,
            )
            if tmdb_ids:
                catalog_gen.save_user_catalog(
                    user_id=user.id,
                    slot_id=slot,
                    name="Hidden Gems For You",
                    media_type=media_type,
                    tmdb_ids=tmdb_ids,
                    generation_method="hidden_gems",
                )
                catalogs_created += 1
                logger.info(f"  Hidden Gems ({media_label}): {len(tmdb_ids)} items")
        except Exception as e:
            logger.warning(f"  Hidden Gems ({media_label}) failed: {e}")

    # ------------------------------------------------------------------
    # Steps 5-7: Trending/New Releases/Popular + backfill + commit
    # ------------------------------------------------------------------
    return await _generate_common_catalogs(
        user, db, catalog_gen, catalogs_created, display, common_data=common_data
    )


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

    # Fetch trending/new-releases/popular from TMDB once — shared across all users
    # so we make ~18 TMDB requests total instead of 18 × N per-user requests.
    logger.info(
        "Pre-fetching common TMDB catalog data (trending, new releases, popular)..."
    )
    common_data = await _prefetch_common_catalog_data(db)
    logger.info(
        f"Pre-fetch complete: "
        + ", ".join(f"{k}={len(v)}" for k, v in common_data.items())
    )

    stats = {"total": len(users), "synced": 0, "failed": 0, "catalogs": 0}

    for user in users:
        try:
            auth_source = getattr(user, "auth_source", "trakt")
            if auth_source == "local":
                count = await sync_local_user_catalogs(
                    user, db, common_data=common_data
                )
            else:
                access_token = await ensure_valid_trakt_token(user, db)
                count = await sync_user_catalogs(
                    user, db, access_token, common_data=common_data
                )
            stats["synced"] += 1
            stats["catalogs"] += count
        except Exception as e:
            display = user.trakt_username or user.display_name or user.user_key[:8]
            logger.error(f"Sync failed for user {display}: {e}")
            stats["failed"] += 1
            db.rollback()

        # Brief pause between users to avoid hammering APIs
        await asyncio.sleep(1)

    logger.info(
        f"Trakt sync complete: {stats['synced']}/{stats['total']} users synced, "
        f"{stats['catalogs']} total catalogs, {stats['failed']} failures"
    )
    return stats
