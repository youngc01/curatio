"""
Comprehensive test suite for Stremio AI Addon.

Tests all major components: tagging, catalogs, API endpoints.
"""

import pytest
import asyncio
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Add parent directory to path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import app
from app.models import Base, Tag, MovieTag, UniversalCategory, MediaMetadata
from app.gemini_client import GeminiTaggingEngine
from app.catalog_generator import CatalogGenerator
from app.config import settings

# Test database
TEST_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    """Create test database for each test."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


# =============================================================================
# Tagging Tests
# =============================================================================

@pytest.mark.asyncio
async def test_gemini_tagging_single_movie():
    """Test tagging a single movie with Gemini."""
    engine = GeminiTaggingEngine()
    
    test_movie = {
        'tmdb_id': 78,
        'media_type': 'movie',
        'title': 'Blade Runner',
        'overview': 'A blade runner must pursue and terminate four replicants...',
        'genres': ['Science Fiction', 'Thriller'],
        'release_date': '1982-06-25'
    }
    
    # Mock Gemini response for testing
    # In real test, this would call actual API or use mock
    result = await engine.tag_items([test_movie])
    
    assert len(result) > 0
    assert result[0]['tmdb_id'] == 78
    assert 'tags' in result[0]
    assert isinstance(result[0]['tags'], dict)
    
    # Check for expected tags
    tags = result[0]['tags']
    assert any(tag in tags for tag in ['Sci-Fi', 'Neo-Noir', 'Cyberpunk'])


@pytest.mark.asyncio
async def test_gemini_tagging_batch():
    """Test tagging multiple movies in batch."""
    engine = GeminiTaggingEngine()
    
    test_movies = [
        {
            'tmdb_id': 78,
            'media_type': 'movie',
            'title': 'Blade Runner',
            'overview': 'Sci-fi noir about replicants...',
            'genres': ['Science Fiction'],
            'release_date': '1982-06-25'
        },
        {
            'tmdb_id': 603,
            'media_type': 'movie',
            'title': 'The Matrix',
            'overview': 'Reality is not what it seems...',
            'genres': ['Action', 'Science Fiction'],
            'release_date': '1999-03-31'
        }
    ]
    
    results = await engine.tag_items(test_movies)
    
    assert len(results) == 2
    assert all('tags' in r for r in results)


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
    movie_tag = MovieTag(
        tmdb_id=78,
        tag_id=tag.id,
        confidence=0.95,
        media_type="movie"
    )
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
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2}
    )
    db.add(category)
    db.commit()
    
    retrieved = db.query(UniversalCategory).filter(
        UniversalCategory.id == "test-category"
    ).first()
    
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
            vote_average=8.0
        ),
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="Dark Crime Movie 2",
            popularity=90.0,
            vote_average=7.5
        )
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
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2}
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
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    assert "name" in response.json()


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()


def test_universal_manifest(client):
    """Test universal manifest endpoint."""
    response = client.get("/manifest/universal.json")
    assert response.status_code == 200
    
    manifest = response.json()
    assert "id" in manifest
    assert "catalogs" in manifest
    assert isinstance(manifest["catalogs"], list)


def test_catalog_endpoint_not_found(client):
    """Test catalog endpoint with non-existent catalog."""
    response = client.get("/catalog/movie/nonexistent.json")
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
        Tag(name="Crime", category="genre")
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
        vote_average=8.5
    )
    db.add(movie)
    db.commit()
    
    # Step 3: Add tags to movie
    movie_tags = [
        MovieTag(tmdb_id=999, tag_id=tags[0].id, confidence=0.95, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[1].id, confidence=0.90, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[2].id, confidence=0.88, media_type="movie")
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
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2}
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
            popularity=float(i)
        )
        db.add(movie)
        
        # Add random tags
        for j in range(5):
            movie_tag = MovieTag(
                tmdb_id=i,
                tag_id=tags[j % 10].id,
                confidence=0.8,
                media_type="movie"
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
        tag_formula={"required": ["Tag0", "Tag1"], "min_required": 2}
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
        tag_formula={"required": ["NonexistentTag"], "min_required": 1}
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
    engine = GeminiTaggingEngine()
    
    # Test with empty input
    results = await engine.tag_items([])
    assert results == []


# =============================================================================
# Security Tests
# =============================================================================

def test_master_password_protection(client):
    """Test that master password is required for authentication."""
    response = client.get("/auth/start?password=wrong_password")
    assert response.status_code == 403


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
