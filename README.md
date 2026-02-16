<p align="center">
  <img src="assets/logo.svg" alt="Curatio" width="200">
</p>

<h1 align="center">Curatio</h1>
<p align="center"><strong>AI-curated cinema for Stremio</strong></p>

<p align="center">
  Netflix-style content discovery powered by Gemini AI.<br>
  40 curated catalogs. 150,000 tagged titles. $5 one-time, then free forever.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &middot;
  <a href="#features">Features</a> &middot;
  <a href="#how-it-works">How It Works</a> &middot;
  <a href="#categories">Categories</a> &middot;
  <a href="#admin-dashboard">Admin Dashboard</a> &middot;
  <a href="#api-reference">API</a>
</p>

---

## Features

- **40 Universal Catalogs** -- Netflix-style semantic categories (Dark Crime Dramas, Cyberpunk Futures, Feel-Good Comedies, etc.)
- **10 Personalized Catalogs** -- AI recommendations based on your Trakt watch history
- **Gemini AI Tagging** -- Every title semantically tagged across mood, genre, era, and style
- **One-Time $5 Build** -- Tag 150,000 titles once, then serve catalogs at $0/month
- **Admin Dashboard** -- Web UI to manage builds, monitor status, and configure settings
- **Build Pause/Resume** -- Pause builds at any time and resume later, even after a container restart
- **Daily Auto-Updates** -- Built-in scheduler fetches and tags new releases automatically
- **Multi-User Support** -- Each Trakt user gets personalized recommendations
- **Master Password** -- Gate access to prevent unauthorized usage

## How It Works

```
User -> Stremio -> Curatio (FastAPI) -> PostgreSQL (tag database) -> TMDB metadata
                         |
                     Gemini AI (tagging only, one-time + daily updates)
                         |
                     Trakt (watch history for personalization)
```

**The key insight**: Gemini tags every movie/show once and stores the results in PostgreSQL. After that, all catalog generation is pure SQL -- no AI calls during browsing. This keeps ongoing costs at $0.

## Quick Start

### Prerequisites

| Service | Cost | Link |
|---------|------|------|
| Docker & Docker Compose | Free | [docs.docker.com](https://docs.docker.com/get-docker/) |
| TMDB API key | Free | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| Gemini API key | Free + $5 one-time | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| Trakt OAuth app | Free | [trakt.tv/oauth/applications/new](https://trakt.tv/oauth/applications/new) |

### 1. Clone and Configure

```bash
git clone https://github.com/youngc01/stremio-ai-addon.git
cd stremio-ai-addon
cp .env.example .env
```

Edit `.env` with your API keys:

```env
TMDB_API_KEY=your_key
GEMINI_API_KEY=your_key
TRAKT_CLIENT_ID=your_id
TRAKT_CLIENT_SECRET=your_secret
TRAKT_REDIRECT_URI=https://yourdomain.com/auth/trakt/callback
MASTER_PASSWORD=your_strong_password
BASE_URL=https://yourdomain.com
SECRET_KEY=generate_random_32_char_string
```

Generate a secret key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Initial Build (~$5, ~3 hours)

This tags 150,000 titles with Gemini AI. You only do this once.

```bash
# Start database
docker-compose up -d postgres

# Run the tagging build (enable paid tier in .env: GEMINI_PAID_TIER=true)
docker-compose --profile workers run --rm worker python workers/initial_build.py

# After completion, set GEMINI_PAID_TIER=false in .env
```

**Or use the Admin Dashboard** (recommended): start the app and trigger the build from the Build tab at `/admin`. You can pause/resume and monitor progress with live logs.

### 3. Start Curatio

```bash
docker-compose up -d
curl http://localhost:8000/health
# {"status":"healthy","database":"connected","version":"1.0.0"}
```

### 4. Install in Stremio

**Universal (no account needed):**
```
https://yourdomain.com/manifest.json
```

**Personalized (with Trakt):**
1. Visit `https://yourdomain.com` and enter the master password
2. Connect your Trakt account
3. Copy your personal manifest URL
4. Install in Stremio

### 5. Enable Daily Updates

Add to `.env` and restart, or toggle from the Admin Dashboard (Schedule tab):

```env
DAILY_UPDATE_ENABLED=true
DAILY_UPDATE_TIME=03:00
```

New releases are fetched, tagged, and added to catalogs automatically. Stays within Gemini's free tier.

## Categories

### Universal Catalogs (40 total)

Curatio ships with 40 hand-designed categories spanning five tiers:

| Tier | Focus | Examples |
|------|-------|---------|
| **Genre + Mood** (15) | Emotional tone | Dark & Gritty Crime Dramas, Feel-Good Comedies, Mind-Bending Sci-Fi |
| **Era + Genre** (5) | Time period | Totally '80s Action, '90s Comedies, Golden Age Film Noir |
| **Plot Elements** (6) | Story type | Heist & Caper Films, Time Travel Mind-Benders, Coming-of-Age |
| **Style + Character** (9) | Cinematic style | Neo-Noir Cinema, Cyberpunk Futures, Anti-Hero Sagas |
| **Special Collections** (5) | Curated sets | Conspiracy & Paranoia, Lavish Period Dramas, Whodunit Mysteries |

Each catalog contains ~100 titles ranked by AI relevance.

### Personalized Catalogs (10 per user)

When connected to Trakt, users get catalogs like:
- Top Picks for You
- Because You Watched [Movie]
- Hidden Gems We Think You'll Love
- Trending in Your Taste

## Admin Dashboard

Curatio includes a web-based admin dashboard at `/admin`, protected by master password:

- **Overview** -- Database stats (tagged titles, categories, users) and recent job history
- **Build Management** -- Start, stop, pause, and resume builds with real-time log streaming
- **Container Restart Recovery** -- Builds automatically resume where they left off after a restart. If the build was paused before a restart, it stays paused until you click Resume
- **Catalog Diagnostics** -- Full pipeline diagnostic to identify issues, plus one-click catalog regeneration
- **Settings** -- Configure API keys, catalog sizes, shuffle intervals, Gemini model, and feature flags -- all hot-reloaded without restarting
- **Schedule** -- Enable/disable and configure the daily update scheduler

## Architecture

```
curatio/
├── app/
│   ├── main.py              # FastAPI app, Stremio manifest & catalog endpoints
│   ├── admin.py             # Admin dashboard, build management & pause/resume
│   ├── models.py            # SQLAlchemy models (12 tables)
│   ├── config.py            # Pydantic settings from environment
│   ├── database.py          # PostgreSQL connection & pooling
│   ├── tmdb_client.py       # TMDB API client with multi-strategy discovery
│   ├── gemini_client.py     # Gemini AI tagging engine
│   ├── trakt_client.py      # Trakt OAuth & watch history
│   ├── catalog_generator.py # SQL-based catalog builder
│   ├── categories.py        # 40 universal category definitions
│   ├── landing.py           # HTML landing & auth pages
│   ├── scheduler.py         # Daily update scheduler
│   └── crypto.py            # Token encryption for Trakt credentials
├── workers/
│   ├── initial_build.py     # One-time full tagging build (pause/resume aware)
│   └── daily_update.py      # Daily new-release tagger
├── tests/
│   └── test_all.py          # Unit, integration & performance tests
├── assets/
│   └── logo.svg             # Curatio logo
├── .github/workflows/
│   └── build.yml            # CI/CD: test, lint, build, deploy
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Configuration

All settings can be configured via `.env` or overridden at runtime through the Admin Dashboard. See [`.env.example`](.env.example) for the full list with descriptions.

| Setting | Default | Description |
|---------|---------|-------------|
| `TMDB_API_KEY` | -- | TMDB API key (required) |
| `GEMINI_API_KEY` | -- | Gemini API key (required) |
| `GEMINI_PAID_TIER` | `false` | Set `true` for initial build, then `false` |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model for tagging |
| `TRAKT_CLIENT_ID` | -- | Trakt OAuth client ID (required) |
| `TRAKT_CLIENT_SECRET` | -- | Trakt OAuth client secret (required) |
| `MASTER_PASSWORD` | -- | Password to gate addon access (required) |
| `SECRET_KEY` | -- | JWT signing key, min 32 chars (required) |
| `BASE_URL` | -- | Public URL where the addon is hosted (required) |
| `CATALOG_SIZE` | `200` | Total items stored per catalog |
| `CATALOG_PAGE_SIZE` | `100` | Items returned per Stremio request |
| `CATALOG_SHUFFLE_HOURS` | `3` | Randomize catalog order every N hours (0 = off) |
| `DAILY_UPDATE_ENABLED` | `false` | Enable automatic daily updates |
| `DAILY_UPDATE_TIME` | `03:00` | Time to run daily update (HH:MM UTC) |
| `WORKERS` | `4` | Uvicorn worker count |
| `DB_POOL_SIZE` | `20` | Database connection pool size |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Cost Breakdown

| Phase | What | Cost | Time |
|-------|------|------|------|
| Initial build | Tag 150K titles with Gemini | ~$5 | ~3 hours |
| Ongoing | Daily new releases (~50-100/day) | $0 (free tier) | ~5 min/day |
| Hosting | Self-hosted via Docker | $0 | -- |

**Total first year: $5. Every year after: $0.**

## Performance

| Metric | Value |
|--------|-------|
| Catalog query | <100ms |
| API response | <200ms |
| Concurrent users | 5,000+ |
| Database size | ~500MB |
| Memory usage | ~300MB |

## API Reference

### Stremio Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/manifest.json` | Universal manifest (40 catalogs) |
| `GET` | `/{user_key}/manifest.json` | Personalized manifest (50 catalogs) |
| `GET` | `/catalog/{type}/{id}.json` | Universal catalog content |
| `GET` | `/{user_key}/catalog/{type}/{id}.json` | Personalized catalog content |

### Auth & Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Landing page |
| `GET` | `/auth/start?password=xxx` | Start Trakt OAuth |
| `GET` | `/auth/trakt/callback` | OAuth callback |
| `GET` | `/admin` | Admin dashboard (master password) |
| `GET` | `/health` | Health check |

### Admin Build API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/api/build/start` | Start a database build |
| `POST` | `/admin/api/build/stop` | Stop the running build |
| `POST` | `/admin/api/build/pause` | Pause build between batches |
| `POST` | `/admin/api/build/resume` | Resume a paused build |
| `POST` | `/admin/api/build/daily` | Trigger a manual daily update |
| `GET` | `/admin/api/build/status` | Current build status |
| `GET` | `/admin/api/build/logs` | Build log lines (poll) |
| `WS` | `/admin/ws/build-logs` | Live build log stream |
| `POST` | `/admin/api/catalogs/regenerate` | Regenerate catalogs from tags |
| `GET` | `/admin/api/debug` | Full pipeline diagnostic |

## Development

### Run Tests

```bash
pytest tests/ -v --cov=app --cov-report=term
```

### Local Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Linting

```bash
black app/ workers/ tests/
ruff check app/ workers/ tests/
mypy app/ --ignore-missing-imports
```

### Docker Build

```bash
docker build -t curatio .
docker run -p 8000:8000 --env-file .env curatio
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Empty catalogs | Run the initial build first, or use "Regenerate Catalogs" in the admin dashboard |
| Gemini rate limit | Wait 1 minute (free tier: 1,500 req/day). The build auto-backs off on 429 errors |
| Build interrupted | Just restart the container -- it auto-resumes where it left off |
| Database connection failed | Check PostgreSQL: `docker-compose up -d postgres` |
| Trakt OAuth redirect mismatch | Ensure `TRAKT_REDIRECT_URI` matches your Trakt app settings exactly |
| Configuration errors | Verify all keys in `.env` have real values, not placeholders |
| Catalogs not updating | Check that `DAILY_UPDATE_ENABLED=true` or trigger manually from the admin Build tab |

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** -- Async Python web framework
- **[PostgreSQL](https://www.postgresql.org/)** -- Tag database & catalog storage
- **[Google Gemini](https://ai.google.dev/)** -- AI-powered semantic tagging
- **[TMDB](https://www.themoviedb.org/)** -- Movie & TV metadata
- **[Trakt](https://trakt.tv/)** -- Watch history & personalization
- **[Docker](https://www.docker.com/)** -- Containerized deployment
- **[GitHub Actions](https://github.com/features/actions)** -- CI/CD pipeline

## License

MIT
