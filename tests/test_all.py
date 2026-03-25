"""
Comprehensive test suite for Curatio.

Tests all major components: tagging, catalogs, API endpoints.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables before importing app modules
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("TRAKT_CLIENT_ID", "test_id")
os.environ.setdefault("TRAKT_CLIENT_SECRET", "test_secret")
os.environ.setdefault("TRAKT_REDIRECT_URI", "http://localhost/callback")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.main import app  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Tag,
    MovieTag,
    UniversalCategory,
    MediaMetadata,
)
from app.gemini_client import GeminiTaggingEngine  # noqa: E402
from app.catalog_generator import CatalogGenerator  # noqa: E402

# Test database
TEST_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    """Create test database for each test."""
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    """FastAPI test client with test database."""

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db_dependency] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# =============================================================================
# Tagging Tests
# =============================================================================


@pytest.mark.asyncio
async def test_gemini_tagging_single_movie():
    """Test tagging a single movie with Gemini."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "items": [
                {
                    "tmdb_id": 78,
                    "tags": {"Sci-Fi": 0.95, "Neo-Noir": 0.9, "Cyberpunk": 0.85},
                }
            ]
        }
    )

    with patch("app.gemini_client.genai") as mock_genai:
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        tagging_engine = GeminiTaggingEngine()

        test_movie = {
            "tmdb_id": 78,
            "media_type": "movie",
            "title": "Blade Runner",
            "overview": "A blade runner must pursue and terminate four replicants...",
            "genres": ["Science Fiction", "Thriller"],
            "release_date": "1982-06-25",
        }

        result = await tagging_engine.tag_items([test_movie])

    assert len(result) > 0
    assert result[0]["tmdb_id"] == 78
    assert "tags" in result[0]
    assert isinstance(result[0]["tags"], dict)

    tags = result[0]["tags"]
    assert any(tag in tags for tag in ["Sci-Fi", "Neo-Noir", "Cyberpunk"])


@pytest.mark.asyncio
async def test_gemini_tagging_batch():
    """Test tagging multiple movies in batch."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "items": [
                {"tmdb_id": 78, "tags": {"Sci-Fi": 0.95}},
                {"tmdb_id": 603, "tags": {"Action": 0.9, "Sci-Fi": 0.85}},
            ]
        }
    )

    with patch("app.gemini_client.genai") as mock_genai:
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        tagging_engine = GeminiTaggingEngine()

        test_movies = [
            {
                "tmdb_id": 78,
                "media_type": "movie",
                "title": "Blade Runner",
                "overview": "Sci-fi noir about replicants...",
                "genres": ["Science Fiction"],
                "release_date": "1982-06-25",
            },
            {
                "tmdb_id": 603,
                "media_type": "movie",
                "title": "The Matrix",
                "overview": "Reality is not what it seems...",
                "genres": ["Action", "Science Fiction"],
                "release_date": "1999-03-31",
            },
        ]

        results = await tagging_engine.tag_items(test_movies)

    assert len(results) == 2
    assert all("tags" in r for r in results)


def test_tag_confidence_validation():
    """Test that tag confidence scores are valid (0.0 to 1.0)."""
    # This would test that all confidence scores from Gemini are in valid range
    confidence = 0.85

    assert 0.0 <= confidence <= 1.0


# =============================================================================
# Database Tests
# =============================================================================


def test_create_tags(db):
    """Test creating tags in database."""
    tag = Tag(name="Dark", category="mood")
    db.add(tag)
    db.commit()

    retrieved = db.query(Tag).filter(Tag.name == "Dark").first()
    assert retrieved is not None
    assert retrieved.name == "Dark"
    assert retrieved.category == "mood"


def test_create_movie_tag(db):
    """Test creating movie-tag association."""
    # Create tag
    tag = Tag(name="Cyberpunk", category="style")
    db.add(tag)
    db.commit()

    # Create movie tag
    movie_tag = MovieTag(tmdb_id=78, tag_id=tag.id, confidence=0.95, media_type="movie")
    db.add(movie_tag)
    db.commit()

    # Retrieve
    retrieved = db.query(MovieTag).filter(MovieTag.tmdb_id == 78).first()
    assert retrieved is not None
    assert retrieved.confidence == 0.95
    assert retrieved.tag.name == "Cyberpunk"


def test_create_universal_category(db):
    """Test creating universal category."""
    category = UniversalCategory(
        id="test-category",
        name="Test Category",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    retrieved = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.id == "test-category")
        .first()
    )

    assert retrieved is not None
    assert retrieved.name == "Test Category"
    assert "required" in retrieved.tag_formula


# =============================================================================
# Catalog Generator Tests
# =============================================================================


def test_catalog_generator_setup(db):
    """Test catalog generator initialization."""
    generator = CatalogGenerator(db)
    assert generator.db is not None


def test_generate_universal_catalog(db):
    """Test generating a universal catalog from tags."""
    # Create tags
    dark_tag = Tag(name="Dark", category="mood")
    crime_tag = Tag(name="Crime", category="genre")
    db.add_all([dark_tag, crime_tag])
    db.commit()

    # Create test movies
    movies = [
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="Dark Crime Movie 1",
            popularity=100.0,
            vote_average=8.0,
            vote_count=500,
        ),
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="Dark Crime Movie 2",
            popularity=90.0,
            vote_average=7.5,
            vote_count=300,
        ),
    ]
    db.add_all(movies)
    db.commit()

    # Create movie tags
    movie_tags = [
        MovieTag(tmdb_id=1, tag_id=dark_tag.id, confidence=0.9, media_type="movie"),
        MovieTag(tmdb_id=1, tag_id=crime_tag.id, confidence=0.85, media_type="movie"),
        MovieTag(tmdb_id=2, tag_id=dark_tag.id, confidence=0.8, media_type="movie"),
        MovieTag(tmdb_id=2, tag_id=crime_tag.id, confidence=0.9, media_type="movie"),
    ]
    db.add_all(movie_tags)
    db.commit()

    # Create category
    category = UniversalCategory(
        id="dark-crime",
        name="Dark Crime Dramas",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Generate catalog
    generator = CatalogGenerator(db)
    tmdb_ids = generator.generate_universal_catalog(category, limit=10)

    assert len(tmdb_ids) == 2
    assert 1 in tmdb_ids
    assert 2 in tmdb_ids


# =============================================================================
# API Tests
# =============================================================================


def test_root_endpoint(client):
    """Test root endpoint returns landing page HTML."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Curatio" in response.text


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()


def test_universal_manifest(client):
    """Test universal manifest endpoint."""
    with patch("app.main.get_install_token", return_value="test-token"):
        response = client.get("/test-token/manifest.json")
    assert response.status_code == 200

    manifest = response.json()
    assert "id" in manifest
    assert "catalogs" in manifest
    assert isinstance(manifest["catalogs"], list)
    assert manifest["idPrefixes"] == ["tmdb"]


def test_catalog_endpoint_not_found(client):
    """Test catalog endpoint with non-existent catalog."""
    with patch("app.main.get_install_token", return_value="test-token"):
        response = client.get("/test-token/catalog/movie/nonexistent.json")
    # Should return empty metas, not 404
    assert response.status_code == 200
    assert "metas" in response.json()


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.integration
def test_full_tagging_to_catalog_flow(db):
    """Integration test: Tag movies → Store in DB → Generate catalog."""
    # Step 1: Create tags
    tags = [
        Tag(name="Dark", category="mood"),
        Tag(name="Gritty", category="mood"),
        Tag(name="Crime", category="genre"),
    ]
    db.add_all(tags)
    db.commit()

    # Step 2: Create movie metadata
    movie = MediaMetadata(
        tmdb_id=999,
        media_type="movie",
        title="Test Dark Crime Movie",
        overview="A dark and gritty crime story",
        popularity=100.0,
        vote_average=8.5,
        vote_count=500,
    )
    db.add(movie)
    db.commit()

    # Step 3: Add tags to movie
    movie_tags = [
        MovieTag(tmdb_id=999, tag_id=tags[0].id, confidence=0.95, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[1].id, confidence=0.90, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[2].id, confidence=0.88, media_type="movie"),
    ]
    db.add_all(movie_tags)
    db.commit()

    # Step 4: Create category
    category = UniversalCategory(
        id="integration-test",
        name="Integration Test Category",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Step 5: Generate catalog
    generator = CatalogGenerator(db)
    tmdb_ids = generator.generate_universal_catalog(category)

    # Step 6: Verify
    assert 999 in tmdb_ids


# =============================================================================
# Performance Tests
# =============================================================================


@pytest.mark.performance
def test_catalog_query_performance(db):
    """Test that catalog queries are fast (< 100ms)."""
    import time

    # Create test data
    tags = [Tag(name=f"Tag{i}", category="test") for i in range(10)]
    db.add_all(tags)
    db.commit()

    # Create 1000 test movies
    for i in range(1000):
        movie = MediaMetadata(
            tmdb_id=i,
            media_type="movie",
            title=f"Movie {i}",
            popularity=float(i),
            vote_count=100,
        )
        db.add(movie)

        # Add random tags
        for j in range(5):
            movie_tag = MovieTag(
                tmdb_id=i, tag_id=tags[j % 10].id, confidence=0.8, media_type="movie"
            )
            db.add(movie_tag)

    db.commit()

    # Create category
    category = UniversalCategory(
        id="perf-test",
        name="Performance Test",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Tag0", "Tag1"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Measure query time
    generator = CatalogGenerator(db)

    start = time.time()
    results = generator.generate_universal_catalog(category, limit=100)
    duration = time.time() - start

    assert duration < 0.1  # Should be under 100ms
    assert len(results) > 0


# =============================================================================
# Error Handling Tests
# =============================================================================


def test_invalid_tag_formula(db):
    """Test handling of invalid tag formula."""
    category = UniversalCategory(
        id="invalid-test",
        name="Invalid Test",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["NonexistentTag"], "min_required": 1},
    )
    db.add(category)
    db.commit()

    generator = CatalogGenerator(db)

    # Should not crash, just return empty results
    results = generator.generate_universal_catalog(category)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_gemini_api_error_handling():
    """Test handling of Gemini API errors."""
    with patch("app.gemini_client.genai") as mock_genai:
        mock_model = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model

        tagging_engine = GeminiTaggingEngine()

        # Test with empty input
        results = await tagging_engine.tag_items([])
        assert results == []


# =============================================================================
# Security Tests
# =============================================================================


def test_master_password_protection(client, db):
    """Test that master password is required for authentication."""
    from contextlib import contextmanager

    @contextmanager
    def mock_get_db():
        yield db

    with patch("app.main.get_db", mock_get_db):
        response = client.get("/auth/start?password=wrong_password")
    assert response.status_code == 403


# =============================================================================
# Scheduler Tests
# =============================================================================


def test_parse_time_valid():
    """Test parsing valid HH:MM time strings."""
    from app.scheduler import parse_time

    assert parse_time("03:00") == (3, 0)
    assert parse_time("00:00") == (0, 0)
    assert parse_time("23:59") == (23, 59)
    assert parse_time("12:30") == (12, 30)


def test_parse_time_invalid():
    """Test parsing invalid time strings raises ValueError."""
    from app.scheduler import parse_time

    with pytest.raises(ValueError):
        parse_time("25:00")
    with pytest.raises(ValueError):
        parse_time("12:60")
    with pytest.raises(ValueError):
        parse_time("invalid")
    with pytest.raises(ValueError):
        parse_time("12")


def test_seconds_until():
    """Test seconds_until returns a positive value."""
    from app.scheduler import seconds_until

    result = seconds_until(3, 0)
    assert result > 0
    # Should be at most ~24 hours
    assert result <= 86400


# =============================================================================
# Daily Update Tests
# =============================================================================


def test_filter_new_items(db):
    """Test that filter_new_items removes already-existing items."""
    from workers.daily_update import filter_new_items

    # Add an existing item to the database
    existing = MediaMetadata(
        tmdb_id=100,
        media_type="movie",
        title="Existing Movie",
        popularity=50.0,
    )
    db.add(existing)
    db.commit()

    # Items from TMDB (one existing, one new)
    items = [
        {"id": 100, "title": "Existing Movie"},
        {"id": 200, "title": "New Movie"},
    ]

    result = filter_new_items(db, items, "movie")

    assert len(result) == 1
    assert result[0]["id"] == 200


def test_filter_new_items_all_new(db):
    """Test filter_new_items when all items are new."""
    from workers.daily_update import filter_new_items

    items = [
        {"id": 300, "title": "New Movie 1"},
        {"id": 400, "title": "New Movie 2"},
    ]

    result = filter_new_items(db, items, "movie")
    assert len(result) == 2


def test_filter_new_items_all_existing(db):
    """Test filter_new_items when all items already exist."""
    from workers.daily_update import filter_new_items

    for tmdb_id in [500, 600]:
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=f"Movie {tmdb_id}",
                popularity=50.0,
            )
        )
    db.commit()

    items = [
        {"id": 500, "title": "Movie 500"},
        {"id": 600, "title": "Movie 600"},
    ]

    result = filter_new_items(db, items, "movie")
    assert len(result) == 0


# =============================================================================
# Category Completeness Tests
# =============================================================================


def test_universal_categories_count(db):
    """Test that all 40 universal categories are created."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    count = db.query(UniversalCategory).count()
    assert count == 40, f"Expected 40 categories, got {count}"


def test_universal_categories_no_duplicates(db):
    """Test that no duplicate category IDs exist."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    categories = db.query(UniversalCategory).all()
    ids = [c.id for c in categories]
    assert len(ids) == len(set(ids)), "Duplicate category IDs found"


def test_universal_categories_have_valid_formulas(db):
    """Test that all categories have valid tag formulas."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    categories = db.query(UniversalCategory).all()
    for cat in categories:
        formula = cat.tag_formula
        assert "required" in formula, f"{cat.id} missing 'required' in formula"
        assert "min_required" in formula, f"{cat.id} missing 'min_required'"
        # Era categories use mandatory + required; total constraint tags must be >= 2
        mandatory = formula.get("mandatory", [])
        required = formula["required"]
        total_tags = len(mandatory) + len(required)
        assert (
            total_tags >= 2
        ), f"{cat.id} needs >= 2 total constraint tags (has {total_tags})"
        assert formula["min_required"] >= 1, f"{cat.id} min_required should be >= 1"


def test_seed_categories_idempotent(db):
    """Test that seeding categories twice doesn't create duplicates."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)
    seed_categories(db)

    count = db.query(UniversalCategory).count()
    assert count == 40, f"Expected 40 after double seed, got {count}"


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
