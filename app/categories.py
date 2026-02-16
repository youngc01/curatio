"""
Universal category definitions and seeding.

These 40 categories are seeded automatically on app startup so the
Stremio manifest always returns a full catalog list, even before
the initial tagging build is run.
"""

from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.models import UniversalCategory

UNIVERSAL_CATEGORIES: list[dict[str, Any]] = [
    # Tier 1: Genre + Mood (15)
    {
        "id": "dark-crime-dramas",
        "name": "Dark & Gritty Crime Dramas",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Dark", "Gritty", "Crime"], "min_required": 2},
    },
    {
        "id": "feel-good-comedies",
        "name": "Feel-Good Comedies",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Feel-Good", "Comedy"], "min_required": 2},
    },
    {
        "id": "mind-bending-scifi",
        "name": "Mind-Bending Sci-Fi Thrillers",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "required": ["Sci-Fi", "Cerebral", "Suspenseful"],
            "min_required": 2,
        },
    },
    {
        "id": "slow-burn-thrillers",
        "name": "Slow-Burn Psychological Thrillers",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "required": ["Slow-Burn", "Thriller", "Cerebral"],
            "min_required": 2,
        },
    },
    {
        "id": "quirky-indie",
        "name": "Quirky Independent Films",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Quirky", "Independent"], "min_required": 2},
    },
    {
        "id": "intense-action",
        "name": "High-Octane Action",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Action", "Intense"], "min_required": 2},
    },
    {
        "id": "heartwarming-dramas",
        "name": "Heartwarming Dramas",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Heartwarming", "Drama"], "min_required": 2},
    },
    {
        "id": "witty-comedies",
        "name": "Sharp & Witty Comedies",
        "tier": 1,
        "media_type": "tv",
        "formula": {"required": ["Witty", "Comedy"], "min_required": 2},
    },
    {
        "id": "dark-horror",
        "name": "Deeply Disturbing Horror",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "required": ["Dark", "Horror", "Intense"],
            "min_required": 2,
        },
    },
    {
        "id": "lighthearted-romance",
        "name": "Lighthearted Romance",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Lighthearted", "Romance"], "min_required": 2},
    },
    {
        "id": "gritty-thrillers",
        "name": "Gritty Crime Thrillers",
        "tier": 1,
        "media_type": "tv",
        "formula": {
            "required": ["Gritty", "Thriller", "Crime"],
            "min_required": 2,
        },
    },
    {
        "id": "suspenseful-mysteries",
        "name": "Edge-of-Your-Seat Mysteries",
        "tier": 1,
        "media_type": "tv",
        "formula": {"required": ["Suspenseful", "Mystery"], "min_required": 2},
    },
    {
        "id": "cerebral-dramas",
        "name": "Cerebral Dramas That Make You Think",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Cerebral", "Drama"], "min_required": 2},
    },
    {
        "id": "feel-good-fantasy",
        "name": "Feel-Good Fantasy Adventures",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Feel-Good", "Fantasy"], "min_required": 2},
    },
    {
        "id": "dark-sci-fi",
        "name": "Dark & Thought-Provoking Sci-Fi",
        "tier": 1,
        "media_type": "tv",
        "formula": {
            "required": ["Dark", "Sci-Fi", "Cerebral"],
            "min_required": 2,
        },
    },
    # Tier 2: Era + Genre (5)
    {
        "id": "80s-action",
        "name": "Totally '80s Action",
        "tier": 2,
        "media_type": "movie",
        "formula": {"required": ["1980s", "Action"], "min_required": 2},
    },
    {
        "id": "90s-comedies",
        "name": "'90s Comedies",
        "tier": 2,
        "media_type": "movie",
        "formula": {"required": ["1990s", "Comedy"], "min_required": 2},
    },
    {
        "id": "70s-thrillers",
        "name": "'70s Paranoia Thrillers",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "required": ["1970s", "Thriller", "Suspenseful"],
            "min_required": 2,
        },
    },
    {
        "id": "golden-age-noir",
        "name": "Golden Age Film Noir",
        "tier": 2,
        "media_type": "movie",
        "formula": {"required": ["1940s", "Crime", "Dark"], "min_required": 2},
    },
    {
        "id": "modern-horror",
        "name": "Modern Horror (2010s & Beyond)",
        "tier": 2,
        "media_type": "movie",
        "formula": {"required": ["2010s", "Horror"], "min_required": 2},
    },
    # Tier 3: Plot Elements (6)
    {
        "id": "heist-capers",
        "name": "Heist & Caper Films",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "required": ["Heist", "Crime", "Suspenseful"],
            "min_required": 2,
        },
    },
    {
        "id": "time-travel-sci-fi",
        "name": "Time Travel Mind-Benders",
        "tier": 3,
        "media_type": "movie",
        "formula": {"required": ["Time Travel", "Sci-Fi"], "min_required": 2},
    },
    {
        "id": "revenge-tales",
        "name": "Cold-Blooded Revenge Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "required": ["Revenge", "Intense", "Dark"],
            "min_required": 2,
        },
    },
    {
        "id": "courtroom-dramas",
        "name": "Courtroom Dramas",
        "tier": 3,
        "media_type": "movie",
        "formula": {"required": ["Courtroom", "Drama"], "min_required": 2},
    },
    {
        "id": "coming-of-age",
        "name": "Coming-of-Age Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "required": ["Coming-of-Age", "Drama", "Heartwarming"],
            "min_required": 2,
        },
    },
    {
        "id": "survival-thrillers",
        "name": "Against All Odds: Survival Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "required": ["Survival", "Intense", "Thriller"],
            "min_required": 2,
        },
    },
    # Tier 4: Style + Character (9)
    {
        "id": "neo-noir-cinema",
        "name": "Neo-Noir Cinema",
        "tier": 4,
        "media_type": "movie",
        "formula": {"required": ["Neo-Noir", "Dark", "Crime"], "min_required": 2},
    },
    {
        "id": "arthouse-gems",
        "name": "Arthouse Cinema",
        "tier": 4,
        "media_type": "movie",
        "formula": {"required": ["Arthouse", "Cerebral"], "min_required": 2},
    },
    {
        "id": "cyberpunk-futures",
        "name": "Cyberpunk Futures",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Cyberpunk", "Sci-Fi", "Neon Visuals"],
            "min_required": 2,
        },
    },
    {
        "id": "visually-stunning",
        "name": "Visually Stunning Masterpieces",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Visually Stunning", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "satirical-comedies",
        "name": "Biting Satire & Dark Comedy",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Satirical", "Comedy", "Witty"],
            "min_required": 2,
        },
    },
    {
        "id": "anti-hero-saga",
        "name": "Anti-Hero Sagas",
        "tier": 4,
        "media_type": "tv",
        "formula": {
            "required": ["Anti-Hero", "Dark", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "strong-female-leads",
        "name": "Powerful Women on Screen",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Strong Female Lead", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "character-studies",
        "name": "Intimate Character Studies",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Character Study", "Slow-Burn", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "underdog-stories",
        "name": "Underdog Triumphs",
        "tier": 4,
        "media_type": "movie",
        "formula": {"required": ["Underdog", "Heartwarming"], "min_required": 2},
    },
    # Tier 5: Special Collections (5)
    {
        "id": "conspiracy-thrillers",
        "name": "Conspiracy & Paranoia",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "required": ["Conspiracy", "Suspenseful", "Thriller"],
            "min_required": 2,
        },
    },
    {
        "id": "found-footage-horror",
        "name": "Found Footage Frights",
        "tier": 5,
        "media_type": "movie",
        "formula": {"required": ["Found Footage", "Horror"], "min_required": 2},
    },
    {
        "id": "ensemble-dramedies",
        "name": "Ensemble Cast Dramedies",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "required": ["Ensemble Cast", "Comedy", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "period-costume-dramas",
        "name": "Lavish Period Dramas",
        "tier": 5,
        "media_type": "tv",
        "formula": {"required": ["Period Costume", "Drama"], "min_required": 2},
    },
    {
        "id": "detective-mysteries",
        "name": "Whodunit Detective Mysteries",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "required": ["Detective", "Mystery", "Suspenseful"],
            "min_required": 2,
        },
    },
]


def seed_categories(db: Session) -> int:
    """
    Seed universal categories into the database.

    Idempotent — only inserts categories that don't already exist.
    Returns the number of new categories created.
    """
    created = 0
    for i, cat_data in enumerate(UNIVERSAL_CATEGORIES):
        existing = (
            db.query(UniversalCategory)
            .filter(UniversalCategory.id == cat_data["id"])
            .first()
        )

        if not existing:
            category = UniversalCategory(
                id=cat_data["id"],
                name=cat_data["name"],
                tier=cat_data["tier"],
                sort_order=i + 1,
                media_type=cat_data["media_type"],
                tag_formula=cat_data["formula"],
            )
            db.add(category)
            created += 1

    db.commit()

    if created > 0:
        logger.info(f"Seeded {created} universal categories")
    else:
        logger.info("All 40 universal categories already exist")

    return created
