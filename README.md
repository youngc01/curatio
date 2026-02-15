# Stremio AI Recommendations Addon

Netflix-style AI-powered content discovery for Stremio using Gemini AI.

## Features

✅ **40 Universal Categories** - Netflix-style semantic categories (no Trakt required)  
✅ **10 Personalized Categories** - AI-powered recommendations based on your Trakt history  
✅ **Gemini AI Tagging** - Semantic understanding of movies and TV shows  
✅ **One-Time $5 Build** - Tag 150,000 titles once, then free forever  
✅ **PostgreSQL Storage** - Fast catalog generation via SQL (no API calls)  
✅ **Multi-User Support** - Each user gets personalized recommendations  
✅ **Master Password** - Gate access to prevent abuse  

## Architecture

```
User → Stremio → FastAPI → PostgreSQL (tag database) → TMDB metadata
                     ↓
                 Gemini AI (tagging only)
                     ↓
                 Trakt (watch history)
```

**Key Innovation**: Gemini tags movies once → stored in PostgreSQL → catalogs generated via SQL → $0/month forever

## Quick Start

### Prerequisites

- Docker & Docker Compose
- TMDB API key (free): https://www.themoviedb.org/settings/api
- Gemini API key (free): https://aistudio.google.com/app/apikey
- Trakt OAuth app: https://trakt.tv/oauth/applications/new

### 1. Clone Repository

```bash
git clone https://github.com/yourusername/stremio-ai-addon.git
cd stremio-ai-addon
```

### 2. Configure Environment

```bash
cp .env.example .env
nano .env
```

Fill in your API keys:
```env
TMDB_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
TRAKT_CLIENT_ID=your_id_here
TRAKT_CLIENT_SECRET=your_secret_here
TRAKT_REDIRECT_URI=https://yourdomain.com/auth/trakt/callback
MASTER_PASSWORD=your_strong_password
BASE_URL=https://yourdomain.com
SECRET_KEY=generate_random_32_char_string
```

### 3. Initial Database Build ($5 One-Time)

**This tags 100,000 movies + 50,000 TV shows in ~3 hours for $5.**

```bash
# Enable Gemini paid tier in .env
GEMINI_PAID_TIER=true

# Run initial tagging job
docker-compose run --rm worker python workers/initial_build.py

# This will:
# - Fetch 150,000 titles from TMDB
# - Tag them with Gemini AI
# - Store tags in PostgreSQL
# - Generate all 40 universal catalogs
# - Cost: ~$5 total
# - Time: ~3 hours

# After completion, disable paid tier
GEMINI_PAID_TIER=false
```

### 4. Start the Addon

```bash
docker-compose up -d

# Check logs
docker-compose logs -f app

# Check health
curl http://localhost:8000/health
```

### 5. Install in Stremio

**Option A: Universal (No Trakt)**
```
https://yourdomain.com/manifest/universal.json
```

**Option B: Personalized (With Trakt)**
1. Visit: `https://yourdomain.com/auth/start?password=your_master_password`
2. Sign in with Trakt
3. Copy your personal manifest URL
4. Install in Stremio

## Ongoing Maintenance (Free Forever)

### Daily Updates (Built-In Scheduler)

Enable the in-app daily update scheduler — no cron needed:

```env
# In your .env file
DAILY_UPDATE_ENABLED=true
DAILY_UPDATE_TIME=03:00  # HH:MM in UTC
```

The scheduler runs inside the app process and automatically:
- Fetches new releases from TMDB daily
- Tags them with Gemini AI
- Regenerates all universal catalogs
- Cost: $0 (within free tier)
- Time: ~5 minutes per run

**Manual fallback:**
```bash
docker-compose run --rm worker python workers/daily_update.py
```

## Project Structure

```
stremio-ai-addon/
├── app/
│   ├── main.py              # FastAPI application
│   ├── models.py            # Database models
│   ├── config.py            # Configuration
│   ├── database.py          # Database connection
│   ├── tmdb_client.py       # TMDB API client
│   ├── gemini_client.py     # Gemini AI tagging
│   ├── trakt_client.py      # Trakt OAuth
│   └── catalog_generator.py # Catalog builder
├── workers/
│   ├── initial_build.py     # One-time $5 build
│   └── daily_update.py      # Free daily updates
├── tests/
│   ├── test_tagging.py      # Gemini tests
│   ├── test_catalogs.py     # Catalog tests
│   └── test_api.py          # API tests
├── docker-compose.yml       # Full stack
├── Dockerfile               # App container
├── requirements.txt         # Python deps
└── .env.example             # Config template
```

## Cost Breakdown

### One-Time Initial Build
- **Movies**: 100,000 × 250 tokens = 25M tokens = **$3.00**
- **TV Shows**: 50,000 × 250 tokens = 12.5M tokens = **$1.69**
- **Total**: **~$5**

### Monthly Ongoing (Free Tier)
- Weekly new releases: 400 movies + 120 shows/month
- Gemini requests: ~11/month
- Within free tier: 1,500 requests/day
- **Cost: $0/month**

## Category Examples

### Universal Categories (40 total)

**Genre + Mood**
- Dark & Gritty Crime Dramas
- Feel-Good Comedies
- Mind-Bending Sci-Fi Thrillers
- Slow-Burn Psychological Thrillers

**Era + Genre**
- Classic Film Noir (1940s-50s)
- 80s Action Blockbusters
- 90s Teen Comedies

**Region + Genre**
- Korean Crime Dramas
- British Cozy Mysteries
- Scandinavian Noir

**Plot Elements**
- Heist Films with a Twist
- Time Travel Stories
- Revenge Thrillers

**Special Collections**
- Critically Acclaimed Award Winners
- Hidden Gems
- Cult Classics

### Personalized Categories (10 per user)

- Top Picks for [Name]
- Because You Watched Blade Runner 2049
- More Like The Matrix
- Your Rewatch Favorites
- Continue Watching
- Hidden Gems We Think You'll Love
- Recently Added For You
- Trending in Your Taste

## API Endpoints

### Stremio Manifest
- `GET /manifest/universal.json` - Universal catalogs
- `GET /manifest/{user_key}.json` - Personalized catalogs

### Catalogs
- `GET /catalog/{type}/{id}.json` - Universal catalog
- `GET /catalog/{user_key}/{type}/{id}.json` - Personal catalog

### OAuth
- `GET /auth/start?password=xxx` - Start Trakt auth
- `GET /auth/trakt/callback` - OAuth callback

### Health
- `GET /health` - Health check
- `GET /` - Addon info

## Development

### Run Tests

```bash
# All tests
docker-compose run --rm app pytest

# Specific test
docker-compose run --rm app pytest tests/test_tagging.py

# With coverage
docker-compose run --rm app pytest --cov=app
```

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run database migrations
alembic upgrade head

# Start FastAPI dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Adding New Categories

1. Define category in code:
```python
{
    "id": "cyberpunk-noir",
    "name": "Neon-Soaked Cyberpunk Noir",
    "tag_formula": {
        "required": ["Cyberpunk", "Neo-Noir"],
        "optional": ["Neon Visuals"],
        "min_required": 2
    }
}
```

2. Add to database:
```python
category = UniversalCategory(
    id="cyberpunk-noir",
    name="Neon-Soaked Cyberpunk Noir",
    tier=1,
    sort_order=5,
    media_type="movie",
    tag_formula=tag_formula
)
db.add(category)
db.commit()
```

3. Generate catalog:
```bash
docker-compose run --rm app python -c "from app.catalog_generator import CatalogGenerator; from app.database import get_db; with get_db() as db: CatalogGenerator(db).regenerate_all_universal_catalogs()"
```

## Troubleshooting

### Gemini API Errors
```
Error: Rate limit exceeded
Solution: You're within free tier. Wait 1 minute and retry.
```

### Database Connection Failed
```
Error: could not connect to server
Solution: Ensure PostgreSQL is running: docker-compose up -d postgres
```

### Trakt OAuth Failed
```
Error: redirect_uri mismatch
Solution: Check TRAKT_REDIRECT_URI matches your Trakt app settings
```

### Empty Catalogs
```
Issue: Catalogs show no items
Solution: Run initial build first: docker-compose run --rm worker python workers/initial_build.py
```

## Performance

### Database Size
- 150,000 titles × 50 tags = 7.5M rows
- Total storage: ~500 MB
- Query time: <100ms per catalog

### API Response Times
- Manifest: <50ms
- Catalog (100 items): <100ms
- Health check: <10ms

### Scalability
- Supports 5,000+ concurrent users
- PostgreSQL handles all catalog queries
- No Gemini calls during user browsing (only during daily updates)

## Security

- Master password required for user signup
- Trakt OAuth for user authentication
- JWT tokens for API access
- Rate limiting: 60 requests/minute per user
- No sensitive data logged

## License

MIT License - see LICENSE file

## Support

- Issues: https://github.com/yourusername/stremio-ai-addon/issues
- Discussions: https://github.com/yourusername/stremio-ai-addon/discussions

## Credits

- TMDB for movie/TV metadata
- Google Gemini for AI tagging
- Trakt for watch history
- Stremio for the addon platform

## Changelog

### v1.0.0 (2025-01-01)
- Initial release
- 40 universal categories
- 10 personalized categories per user
- Gemini AI tagging
- PostgreSQL storage
- Docker deployment
