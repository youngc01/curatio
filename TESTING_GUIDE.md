# Testing & Debugging Guide

Complete guide for testing and debugging the Stremio AI Addon.

## Quick Test Checklist

✅ Python syntax validation  
✅ Database model validation  
✅ API endpoint testing  
✅ Gemini AI integration testing  
✅ TMDB API integration testing  
✅ Trakt OAuth flow testing  
✅ Catalog generation testing  
✅ Docker build testing  
✅ End-to-end integration testing  

## Pre-Deployment Testing

### 1. Validate Configuration

```bash
# Check all required environment variables are set
cd stremio-ai-addon
cp .env.example .env

# Edit .env with your actual API keys
nano .env

# Validate configuration
python3 -c "from app.config import settings, validate_api_keys; validate_api_keys(); print('✓ Configuration valid')"
```

### 2. Test Database Models

```bash
# Test database initialization
python3 -c "
from app.database import init_database, check_database_connection
init_database()
assert check_database_connection()
print('✓ Database initialized successfully')
"
```

### 3. Test TMDB API Integration

```bash
# Test TMDB client
python3 -c "
import asyncio
from app.tmdb_client import tmdb_client

async def test():
    # Test fetching popular movies
    movies = await tmdb_client.get_popular_movies(page=1)
    assert len(movies['results']) > 0
    print(f'✓ TMDB API working - fetched {len(movies[\"results\"])} movies')

asyncio.run(test())
"
```

### 4. Test Gemini AI Integration

**Note**: This will use 1 API request from your free tier.

```bash
# Test Gemini tagging
python3 -c "
import asyncio
from app.gemini_client import gemini_engine

async def test():
    test_item = {
        'tmdb_id': 78,
        'media_type': 'movie',
        'title': 'Blade Runner',
        'overview': 'A blade runner must pursue and terminate replicants...',
        'genres': ['Science Fiction', 'Thriller'],
        'release_date': '1982-06-25'
    }
    
    result = await gemini_engine.tag_items([test_item])
    assert len(result) > 0
    assert 'tags' in result[0]
    print(f'✓ Gemini AI working - tagged movie with {len(result[0][\"tags\"])} tags')
    print(f'Tags: {list(result[0][\"tags\"].keys())[:5]}...')

asyncio.run(test())
"
```

### 5. Run Unit Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov pytest-mock

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=app --cov-report=term --cov-report=html

# Open coverage report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### 6. Test FastAPI Application

```bash
# Start test server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# In another terminal, test endpoints:

# Health check
curl http://localhost:8000/health

# Root endpoint
curl http://localhost:8000/

# Universal manifest
curl http://localhost:8000/manifest/universal.json
```

### 7. Test Docker Build

```bash
# Build Docker image
docker build -t stremio-ai-addon:test .

# Check image size
docker images stremio-ai-addon:test

# Run container
docker run -p 8000:8000 --env-file .env stremio-ai-addon:test

# Test health endpoint
curl http://localhost:8000/health
```

### 8. Test Docker Compose Stack

```bash
# Start full stack
docker-compose up -d

# Check all containers are running
docker-compose ps

# Check logs
docker-compose logs -f app

# Test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/manifest/universal.json

# Stop stack
docker-compose down
```

## Testing the Initial Build Job

### Dry Run (Free - No API Calls)

Test the initial build script without making actual API calls:

```bash
# Create test database
export DATABASE_URL="sqlite:///./test_build.db"
export SKIP_API_VALIDATION="true"

# Run with small dataset
python workers/initial_build.py --movies 10 --shows 5
```

### Small Test Run ($0.01)

Test with a small batch to verify everything works:

```bash
# Set to paid tier temporarily
export GEMINI_PAID_TIER="true"

# Run with 100 movies (costs ~$0.01)
docker-compose run --rm worker python workers/initial_build.py --movies 100 --shows 50

# Check results
docker-compose run --rm app python -c "
from app.database import get_db
from app.models import MovieTag
with get_db() as db:
    count = db.query(MovieTag).count()
    print(f'Tagged items: {count}')
"
```

### Full Build ($5)

Once everything is tested, run the full build:

```bash
# Enable paid tier
export GEMINI_PAID_TIER="true"

# Run full build
docker-compose run --rm worker python workers/initial_build.py --movies 100000 --shows 50000

# This will take ~3 hours and cost ~$5

# After completion, disable paid tier
export GEMINI_PAID_TIER="false"
```

## Testing the Daily Update Scheduler

### Run Unit Tests

The scheduler and daily update worker have dedicated tests:

```bash
# Run scheduler and daily update tests
pytest tests/test_all.py -v -k "daily or parse_time or seconds_until or filter_new"
```

Tests cover:
- `test_parse_time_valid` - Parses "HH:MM" time strings correctly
- `test_parse_time_invalid` - Rejects malformed time strings
- `test_seconds_until` - Calculates wait time until next target
- `test_filter_new_items` - Filters out already-tagged items
- `test_filter_new_items_all_new` - Passes through all new items
- `test_filter_new_items_all_existing` - Filters out all existing items

### Manual Test Run

Run the daily update worker manually to verify it works:

```bash
# Run daily update once (without the scheduler loop)
docker-compose run --rm worker python workers/daily_update.py
```

### Enable the Scheduler

```bash
# In .env
DAILY_UPDATE_ENABLED=true
DAILY_UPDATE_TIME=03:00

# Restart the app
docker-compose up -d

# Check logs for scheduler startup
docker-compose logs -f app | grep -i "scheduler"
```

## Testing Trakt OAuth Flow

### 1. Setup Ngrok (for local testing)

```bash
# Install ngrok
brew install ngrok  # macOS
# or download from https://ngrok.com

# Start ngrok tunnel
ngrok http 8000

# Copy the https URL (e.g., https://abc123.ngrok.io)
# Update TRAKT_REDIRECT_URI in .env:
TRAKT_REDIRECT_URI=https://abc123.ngrok.io/auth/trakt/callback
BASE_URL=https://abc123.ngrok.io

# Also update in Trakt app settings
```

### 2. Test OAuth Flow

```bash
# Start addon
docker-compose up -d

# Visit in browser:
https://abc123.ngrok.io/auth/start?password=your_master_password

# You should be redirected to Trakt
# After authorizing, you'll get a manifest URL

# Test the manifest
curl https://abc123.ngrok.io/manifest/YOUR_USER_KEY.json
```

## Debugging Common Issues

### Issue: "Configuration errors: TMDB_API_KEY is not configured"

**Solution**:
```bash
# Check .env file exists
ls -la .env

# Verify API key is set
grep TMDB_API_KEY .env

# If empty, get key from https://www.themoviedb.org/settings/api
```

### Issue: "Database connection failed"

**Solution**:
```bash
# Check PostgreSQL is running
docker-compose ps postgres

# Check database URL
echo $DATABASE_URL

# Test connection manually
docker-compose exec postgres psql -U postgres -d stremio_ai -c "SELECT 1"
```

### Issue: "Gemini API rate limit exceeded"

**Solution**:
```bash
# You're hitting free tier limits (1,500 requests/day)
# Wait 1 minute and retry
# Or enable paid tier temporarily
```

### Issue: "Empty catalogs in Stremio"

**Solution**:
```bash
# Check if initial build completed
docker-compose run --rm app python -c "
from app.database import get_db
from app.models import UniversalCatalogContent
with get_db() as db:
    count = db.query(UniversalCatalogContent).count()
    print(f'Catalog items: {count}')
    if count == 0:
        print('Run initial build: docker-compose run --rm worker python workers/initial_build.py')
"
```

### Issue: "Trakt OAuth redirect mismatch"

**Solution**:
```bash
# Verify redirect URI matches exactly
# In .env:
TRAKT_REDIRECT_URI=https://yourdomain.com/auth/trakt/callback

# In Trakt app settings (https://trakt.tv/oauth/applications):
# Redirect URI must be EXACTLY the same (including https://)
```

## Performance Testing

### Test Catalog Query Speed

```bash
# Should be < 100ms
docker-compose run --rm app python -c "
import time
from app.database import get_db
from app.catalog_generator import CatalogGenerator
from app.models import UniversalCategory

with get_db() as db:
    category = db.query(UniversalCategory).first()
    if category:
        generator = CatalogGenerator(db)
        start = time.time()
        items = generator.get_catalog_content(category.id)
        duration = time.time() - start
        print(f'Query time: {duration*1000:.1f}ms')
        print(f'Items returned: {len(items)}')
        assert duration < 0.1, 'Query too slow!'
        print('✓ Performance OK')
"
```

### Test API Response Time

```bash
# Start server
docker-compose up -d

# Test with Apache Bench
ab -n 1000 -c 10 http://localhost:8000/health

# Should handle 100+ requests/second
```

### Load Test

```bash
# Install k6
brew install k6

# Create load test script
cat > load_test.js << 'EOF'
import http from 'k6/http';
import { check } from 'k6';

export let options = {
  vus: 50,  // 50 virtual users
  duration: '30s',
};

export default function() {
  let res = http.get('http://localhost:8000/manifest/universal.json');
  check(res, {
    'status is 200': (r) => r.status === 200,
    'response time < 200ms': (r) => r.timings.duration < 200,
  });
}
EOF

# Run load test
k6 run load_test.js
```

## Security Testing

### Test Master Password Protection

```bash
# Should fail with wrong password
curl -w "%{http_code}" "http://localhost:8000/auth/start?password=wrong"
# Expected: 403

# Should succeed with correct password
curl -w "%{http_code}" "http://localhost:8000/auth/start?password=your_master_password"
# Expected: 302 (redirect)
```

### Test Rate Limiting

```bash
# Make 100 requests rapidly
for i in {1..100}; do
  curl -s http://localhost:8000/health > /dev/null
done

# Should not crash or slow down significantly
```

## Monitoring & Logs

### View Logs

```bash
# All logs
docker-compose logs -f

# Just app logs
docker-compose logs -f app

# Just PostgreSQL logs
docker-compose logs -f postgres

# Search logs
docker-compose logs app | grep ERROR
```

### Monitor Database

```bash
# Connect to database
docker-compose exec postgres psql -U postgres -d stremio_ai

# Check table sizes
SELECT 
    schemaname, 
    tablename, 
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

# Check tag counts
SELECT COUNT(*) FROM tags;
SELECT COUNT(*) FROM movie_tags;
SELECT COUNT(*) FROM universal_catalog_content;
```

## Pre-Production Checklist

Before deploying to production:

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Docker build succeeds
- [ ] Docker Compose stack runs without errors
- [ ] Health endpoint returns 200
- [ ] Universal manifest loads
- [ ] At least one catalog has content
- [ ] Trakt OAuth flow works
- [ ] Database backups configured
- [ ] Logs are being collected
- [ ] Monitoring alerts configured
- [ ] SSL/TLS certificate installed
- [ ] Firewall rules configured
- [ ] Rate limiting tested
- [ ] Load testing completed
- [ ] Documentation updated

## Continuous Testing

Add to crontab for daily health checks:

```bash
# Daily health check
0 2 * * * curl -f http://localhost:8000/health || echo "Addon health check failed" | mail -s "Stremio Addon Alert" you@email.com

# Daily catalog count check (or use the built-in scheduler instead)
0 3 * * 0 docker-compose run --rm app python -c "from app.database import get_db; from app.models import UniversalCatalogContent; with get_db() as db: print(f'Catalog items: {db.query(UniversalCatalogContent).count()}')"
```

## Getting Help

If tests fail:

1. Check logs: `docker-compose logs -f app`
2. Verify configuration: `cat .env`
3. Test database: `docker-compose exec postgres psql -U postgres`
4. Check disk space: `df -h`
5. Review recent changes: `git log -5`
6. Open GitHub issue with:
   - Error message
   - Logs
   - Steps to reproduce
   - Environment details

## Success Criteria

Tests are successful when:

✅ All pytest tests pass  
✅ Docker containers start without errors  
✅ Health endpoint returns `{"status": "healthy"}`  
✅ Universal manifest has 40 catalogs  
✅ Each catalog has 100 items  
✅ Trakt OAuth completes successfully  
✅ Personalized manifest has 50 catalogs (40 universal + 10 personal)  
✅ API response time < 200ms  
✅ Database queries < 100ms  
✅ No memory leaks after 24 hours  
✅ Handles 100+ concurrent users  

Once all checks pass, you're ready to deploy! 🚀
