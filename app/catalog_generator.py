"""
Catalog generator - builds Netflix-style categories from tag database.

This module generates both universal and personalized catalogs using SQL queries.
No Gemini calls needed - everything is pre-computed from tags!
"""

from typing import List, Dict, Optional
from datetime import datetime
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from loguru import logger

from app.models import (
    Tag,
    MovieTag,
    UniversalCategory,
    UniversalCatalogContent,
    UserCatalog,
    UserCatalogContent,
    MediaMetadata,
)
from app.config import settings


class CatalogGenerator:
    """Generates catalogs from tag database using SQL queries."""

    def __init__(self, db: Session):
        self.db = db

    def generate_universal_catalog(
        self, category: UniversalCategory, limit: Optional[int] = None
    ) -> List[int]:
        """
        Generate content for a universal catalog based on tag formula.

        Args:
            category: UniversalCategory with tag_formula
            limit: Max items (defaults to settings.catalog_size)

        Returns:
            List of TMDB IDs ranked by relevance
        """
        limit = limit or settings.catalog_size
        tag_formula = category.tag_formula

        mandatory_tags = tag_formula.get("mandatory", [])
        required_tags = tag_formula.get("required", [])
        optional_tags = tag_formula.get("optional", [])
        min_required = tag_formula.get("min_required", len(required_tags))
        min_vote_count = tag_formula.get("min_vote_count", 10)

        # Get tag IDs
        all_tag_names = mandatory_tags + required_tags + optional_tags
        tag_map = {
            tag.name: tag.id
            for tag in self.db.query(Tag).filter(Tag.name.in_(all_tag_names)).all()
        }

        mandatory_tag_ids = [
            tag_map[name] for name in mandatory_tags if name in tag_map
        ]
        required_tag_ids = [tag_map[name] for name in required_tags if name in tag_map]
        optional_tag_ids = [tag_map[name] for name in optional_tags if name in tag_map]

        all_match_ids = mandatory_tag_ids + required_tag_ids + optional_tag_ids

        # Query movies with matching tags
        query = (
            self.db.query(
                MovieTag.tmdb_id,
                func.avg(MovieTag.confidence).label("match_score"),
                func.count(func.distinct(MovieTag.tag_id)).label("tag_count"),
            )
            .filter(
                MovieTag.media_type == category.media_type,
                MovieTag.tag_id.in_(all_match_ids),
            )
            .group_by(MovieTag.tmdb_id)
        )

        # Mandatory tags: every single one must be present
        if mandatory_tag_ids:
            mandatory_sub = (
                self.db.query(MovieTag.tmdb_id)
                .filter(
                    MovieTag.tag_id.in_(mandatory_tag_ids),
                    MovieTag.media_type == category.media_type,
                )
                .group_by(MovieTag.tmdb_id)
                .having(
                    func.count(func.distinct(MovieTag.tag_id)) == len(mandatory_tag_ids)
                )
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(mandatory_sub.select()))

        # Required tags: must have at least min_required of these
        if required_tag_ids:
            required_sub = (
                self.db.query(MovieTag.tmdb_id)
                .filter(
                    MovieTag.tag_id.in_(required_tag_ids),
                    MovieTag.media_type == category.media_type,
                )
                .group_by(MovieTag.tmdb_id)
                .having(func.count(func.distinct(MovieTag.tag_id)) >= min_required)
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(required_sub.select()))

        # Quality floor: filter out junk with near-zero votes
        if min_vote_count > 0:
            quality_sub = (
                self.db.query(MediaMetadata.tmdb_id)
                .filter(
                    MediaMetadata.media_type == category.media_type,
                    MediaMetadata.vote_count >= min_vote_count,
                )
                .subquery()
            )
            query = query.filter(MovieTag.tmdb_id.in_(quality_sub.select()))

        # Order by match score and limit
        results = (
            query.order_by(func.avg(MovieTag.confidence).desc()).limit(limit).all()
        )

        logger.info(
            f"Generated {len(results)} items for catalog '{category.name}' "
            f"(required: {required_tags}, optional: {optional_tags})"
        )

        return [r.tmdb_id for r in results]

    def save_universal_catalog_content(
        self,
        category_id: str,
        tmdb_ids: List[int],
        match_scores: Optional[List[float]] = None,
    ):
        """
        Save universal catalog content to database.

        Args:
            category_id: Category ID
            tmdb_ids: List of TMDB IDs in rank order
            match_scores: Optional match scores for each item
        """
        # Delete existing content
        self.db.query(UniversalCatalogContent).filter(
            UniversalCatalogContent.category_id == category_id
        ).delete()

        # Get category for media_type
        category = (
            self.db.query(UniversalCategory)
            .filter(UniversalCategory.id == category_id)
            .first()
        )

        if not category:
            logger.error(f"Category {category_id} not found")
            return

        # Insert new content
        for rank, tmdb_id in enumerate(tmdb_ids, start=1):
            match_score = match_scores[rank - 1] if match_scores else 0.0

            content = UniversalCatalogContent(
                category_id=category_id,
                tmdb_id=tmdb_id,
                rank=rank,
                match_score=match_score,
                media_type=category.media_type,
                last_updated=datetime.utcnow(),
            )
            self.db.add(content)

        self.db.flush()
        logger.info(f"Saved {len(tmdb_ids)} items to catalog '{category_id}'")

    def regenerate_all_universal_catalogs(self):
        """Regenerate all universal catalogs from scratch."""
        categories = (
            self.db.query(UniversalCategory)
            .filter(UniversalCategory.is_active.is_(True))
            .all()
        )

        logger.info(f"Regenerating {len(categories)} universal catalogs...")

        for category in categories:
            try:
                tmdb_ids = self.generate_universal_catalog(category)
                self.save_universal_catalog_content(category.id, tmdb_ids)
            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to generate catalog '{category.id}': {e}")

        self.db.commit()
        logger.info("Universal catalog regeneration complete")

    def generate_personalized_catalog(
        self,
        user_id: int,
        catalog_method: str,
        params: Optional[Dict] = None,
        limit: Optional[int] = None,
    ) -> List[int]:
        """
        Generate personalized catalog for a user.

        Args:
            user_id: User ID
            catalog_method: Method (e.g., 'top_picks', 'because_you_watched')
            params: Parameters for generation (e.g., {'reference_movie_id': 123})
            limit: Max items

        Returns:
            List of TMDB IDs
        """
        limit = limit or settings.catalog_size
        params = params or {}

        if catalog_method == "top_picks":
            return self._generate_top_picks(user_id, limit)
        elif catalog_method == "because_you_watched":
            return self._generate_because_you_watched(user_id, params, limit)
        elif catalog_method == "hidden_gems":
            return self._generate_hidden_gems(user_id, limit)
        elif catalog_method in (
            "tag_recommendations",
            "trakt_recommendations",
            "trakt_trending",
            "trakt_popular_weekly",
            "trakt_popular",
            "trakt_anticipated",
        ):
            return self._generate_from_tmdb_ids(params, limit)
        else:
            logger.warning(f"Unknown catalog method: {catalog_method}")
            return []

    def _generate_top_picks(self, user_id: int, limit: int) -> List[int]:
        """
        Generate 'Top Picks for You' based on user's viewing history tags.

        Finds the user's most-watched tag profile, then recommends unwatched
        items that match those tags most strongly.
        """
        from app.models import User

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user or not user.last_sync:
            # No watch history yet -- fall back to popular + well-rated
            return self._fallback_top_picks(limit)

        # Get user's watched TMDB IDs from their personal catalogs
        watched_ids = {
            row.tmdb_id
            for row in self.db.query(UserCatalogContent.tmdb_id)
            .join(UserCatalog)
            .filter(UserCatalog.user_id == user_id)
            .all()
        }

        if not watched_ids:
            return self._fallback_top_picks(limit)

        # Find the user's top tags (most frequent + highest confidence)
        user_tags = (
            self.db.query(
                MovieTag.tag_id,
                func.avg(MovieTag.confidence).label("avg_conf"),
                func.count().label("cnt"),
            )
            .filter(MovieTag.tmdb_id.in_(watched_ids))
            .group_by(MovieTag.tag_id)
            .order_by(func.count().desc(), func.avg(MovieTag.confidence).desc())
            .limit(15)
            .all()
        )

        if not user_tags:
            return self._fallback_top_picks(limit)

        top_tag_ids = [t.tag_id for t in user_tags]

        # Find items matching user's taste profile, excluding watched
        results = (
            self.db.query(
                MovieTag.tmdb_id,
                func.count(MovieTag.tag_id).label("matching_tags"),
                func.avg(MovieTag.confidence).label("avg_confidence"),
            )
            .filter(
                MovieTag.tag_id.in_(top_tag_ids),
                ~MovieTag.tmdb_id.in_(watched_ids),
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

    def _fallback_top_picks(self, limit: int) -> List[int]:
        """Fallback top picks when no watch history is available."""
        results = (
            self.db.query(MediaMetadata.tmdb_id)
            .filter(MediaMetadata.vote_average >= 7.0, MediaMetadata.vote_count >= 1000)
            .order_by(MediaMetadata.popularity.desc())
            .limit(limit)
            .all()
        )
        return [r.tmdb_id for r in results]

    def _generate_because_you_watched(
        self, user_id: int, params: Dict, limit: int
    ) -> List[int]:
        """
        Generate 'Because You Watched X' catalog.

        Finds items with similar tags to the reference item.
        """
        reference_id = params.get("reference_movie_id")
        if not reference_id:
            logger.error("No reference_movie_id provided")
            return []

        # Get tags for reference item
        reference_tags = (
            self.db.query(MovieTag.tag_id, MovieTag.confidence)
            .filter(MovieTag.tmdb_id == reference_id)
            .all()
        )

        if not reference_tags:
            logger.warning(f"No tags found for reference item {reference_id}")
            return []

        tag_ids = [t.tag_id for t in reference_tags]

        # Find items with similar tags
        results = (
            self.db.query(
                MovieTag.tmdb_id,
                func.count(MovieTag.tag_id).label("matching_tags"),
                func.avg(MovieTag.confidence).label("avg_confidence"),
            )
            .filter(
                MovieTag.tag_id.in_(tag_ids),
                MovieTag.tmdb_id != reference_id,  # Exclude the reference item itself
            )
            .group_by(MovieTag.tmdb_id)
            .order_by(
                func.count(MovieTag.tag_id).desc(), func.avg(MovieTag.confidence).desc()
            )
            .limit(limit)
            .all()
        )

        return [r.tmdb_id for r in results]

    def _generate_from_tmdb_ids(self, params: Dict, limit: int) -> List[int]:
        """
        Generate a catalog from a pre-fetched list of TMDB IDs.

        Used for Trakt recommendations/watchlist where the IDs come
        directly from the Trakt API rather than from tag matching.
        """
        tmdb_ids = params.get("tmdb_ids", [])
        if not tmdb_ids:
            return []
        return tmdb_ids[:limit]

    def _generate_hidden_gems(self, user_id: int, limit: int) -> List[int]:
        """
        Generate 'Hidden Gems' - high-quality but less popular items.
        """
        results = (
            self.db.query(MediaMetadata.tmdb_id)
            .filter(
                MediaMetadata.vote_average >= 7.5,
                MediaMetadata.vote_count >= 500,
                MediaMetadata.popularity < 50,  # Less popular
            )
            .order_by(MediaMetadata.vote_average.desc())
            .limit(limit)
            .all()
        )

        return [r.tmdb_id for r in results]

    def save_user_catalog(
        self,
        user_id: int,
        slot_id: str,
        name: str,
        media_type: str,
        tmdb_ids: List[int],
        generation_method: str,
        generation_params: Optional[Dict] = None,
    ) -> UserCatalog:
        """
        Save personalized catalog for a user.

        Args:
            user_id: User ID
            slot_id: Slot identifier (e.g., 'personalized-1')
            name: Display name
            media_type: 'movie' or 'tv'
            tmdb_ids: List of TMDB IDs
            generation_method: How it was generated
            generation_params: Parameters used

        Returns:
            Created UserCatalog
        """
        # Check if catalog exists
        catalog = (
            self.db.query(UserCatalog)
            .filter(UserCatalog.user_id == user_id, UserCatalog.slot_id == slot_id)
            .first()
        )

        if catalog:
            # Update existing
            catalog.name = name  # type: ignore[assignment]
            catalog.generation_method = generation_method  # type: ignore[assignment]
            catalog.generation_params = generation_params  # type: ignore[assignment]
            catalog.last_generated = datetime.utcnow()  # type: ignore[assignment]

            # Delete old content
            self.db.query(UserCatalogContent).filter(
                UserCatalogContent.catalog_id == catalog.id
            ).delete()
        else:
            # Create new
            catalog = UserCatalog(
                user_id=user_id,
                slot_id=slot_id,
                name=name,
                media_type=media_type,
                generation_method=generation_method,
                generation_params=generation_params,
                last_generated=datetime.utcnow(),
            )
            self.db.add(catalog)
            self.db.flush()  # Get catalog.id

        # Add content
        for rank, tmdb_id in enumerate(tmdb_ids, start=1):
            content = UserCatalogContent(
                catalog_id=catalog.id,
                tmdb_id=tmdb_id,
                rank=rank,
                match_score=0.0,  # Can be calculated if needed
                media_type=media_type,
            )
            self.db.add(content)

        self.db.flush()
        logger.info(f"Saved user catalog '{name}' with {len(tmdb_ids)} items")

        return catalog

    def get_catalog_content(
        self,
        category_id: str,
        user_id: Optional[int] = None,
        hide_foreign: bool = False,
        hide_adult: bool = False,
    ) -> List[Dict]:
        """
        Get catalog content with metadata.

        Args:
            category_id: Category ID
            user_id: User ID (for personalized catalogs)
            hide_foreign: Exclude non-English content
            hide_adult: Exclude explicit/18+ content

        Returns:
            List of catalog items with metadata
        """
        limit = settings.catalog_size

        if user_id:
            # Personalized catalog
            query = (
                self.db.query(UserCatalogContent, MediaMetadata)
                .join(
                    MediaMetadata,
                    and_(
                        UserCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UserCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .join(UserCatalog)
                .filter(
                    UserCatalog.user_id == user_id, UserCatalog.slot_id == category_id
                )
            )
        else:
            # Universal catalog
            query = (
                self.db.query(UniversalCatalogContent, MediaMetadata)
                .join(
                    MediaMetadata,
                    and_(
                        UniversalCatalogContent.tmdb_id == MediaMetadata.tmdb_id,
                        UniversalCatalogContent.media_type == MediaMetadata.media_type,
                    ),
                )
                .filter(UniversalCatalogContent.category_id == category_id)
            )

        # Apply content filters
        if hide_foreign:
            query = query.filter(MediaMetadata.original_language == "en")
        if hide_adult:
            query = query.filter(MediaMetadata.adult.isnot(True))

        if user_id:
            results = query.order_by(UserCatalogContent.rank).limit(limit).all()
        else:
            results = query.order_by(UniversalCatalogContent.rank).limit(limit).all()

        items = []
        for content, metadata in results:
            year = ""
            if metadata.release_date:
                year = metadata.release_date[:4]

            items.append(
                {
                    "tmdb_id": metadata.tmdb_id,
                    "media_type": metadata.media_type,
                    "title": metadata.title,
                    "poster": metadata.poster_path,
                    "year": year,
                    "genres": metadata.genres or [],
                    "rating": metadata.vote_average,
                    "description": metadata.overview or "",
                    "rank": content.rank,
                }
            )

        return items
