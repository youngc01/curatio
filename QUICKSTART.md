# Quick Start Guide

Get your Stremio AI Addon running in 30 minutes.

## Prerequisites

- Docker & Docker Compose installed
- 4 API keys (all free except $5 one-time for Gemini build)

## Step 1: Get API Keys (15 minutes)

### TMDB API (5 min) - Free Forever
1. Go to https://www.themoviedb.org/signup
2. Create account
3. Go to Settings → API
4. Request API key (select "Developer")
5. Copy API key

### Gemini API (3 min) - Free + $5 One-Time
1. Go to https://aistudio.google.com/app/apikey
2. Sign in with Google
3. Click "Create API Key"
4. Copy API key
5. (Enable billing for initial $5 build, then disable)

### Trakt OAuth (10 min) - Free Forever
1. Go to https://trakt.tv/oauth/applications/new
2. Fill out:
   - Name: "Stremio AI Recommendations"
   - Redirect URI: `https://yourdomain.com/auth/trakt/callback`
3. Submit
4. Copy Client ID and Client Secret

### Master Password (1 min) - Free
Just create a strong password (share only with people you want to give access)

## Step 2: Configure (5 minutes)

```bash
# Clone repo
git clone https://github.com/yourusername/stremio-ai-addon.git
cd stremio-ai-addon

# Create environment file
cp .env.example .env

# Edit with your API keys
nano .env
```

**Minimum required settings:**
```env
TMDB_API_KEY=your_tmdb_key_here
GEMINI_API_KEY=your_gemini_key_here
TRAKT_CLIENT_ID=your_trakt_id_here
TRAKT_CLIENT_SECRET=your_trakt_secret_here
TRAKT_REDIRECT_URI=https://yourdomain.com/auth/trakt/callback
MASTER_PASSWORD=your_strong_password
BASE_URL=https://yourdomain.com
SECRET_KEY=generate_random_32_character_string_here
DATABASE_URL=postgresql://postgres:password@postgres:5432/stremio_ai
```

**Generate SECRET_KEY:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Step 3: Initial Build (3 hours, $5 one-time)

This tags 150,000 movies and TV shows with AI.

```bash
# Enable paid tier for initial build
echo "GEMINI_PAID_TIER=true" >> .env

# Start database
docker-compose up -d postgres

# Wait for database to be ready (30 seconds)
sleep 30

# Run initial build (takes ~3 hours, costs ~$5)
docker-compose run --rm worker python workers/initial_build.py

# After completion, disable paid tier
sed -i 's/GEMINI_PAID_TIER=true/GEMINI_PAID_TIER=false/' .env
```

**What this does:**
- Fetches 100,000 popular movies from TMDB
- Fetches 50,000 popular TV shows from TMDB
- Tags all items with Gemini AI
- Generates 40 universal catalogs
- Costs ~$5 total
- Takes ~3 hours

**Progress tracking:**
```bash
# In another terminal, watch progress
docker-compose logs -f worker
```

## Step 4: Start Addon (1 minute)

```bash
# Start all services
docker-compose up -d

# Check health
curl http://localhost:8000/health

# Should return: {"status":"healthy"}
```

## Step 5: Install in Stremio (2 minutes)

### Option A: Universal (No Trakt Required)

1. Open Stremio
2. Go to Addons
3. Click "Install from URL"
4. Enter: `https://yourdomain.com/manifest/universal.json`
5. Click Install

**You'll get 40 Netflix-style categories instantly!**

### Option B: Personalized (With Trakt)

1. Visit: `https://yourdomain.com/auth/start?password=your_master_password`
2. Sign in with Trakt
3. Copy your manifest URL
4. In Stremio:
   - Go to Addons
   - Click "Install from URL"
   - Paste your manifest URL
   - Click Install

**You'll get 40 universal + 10 personalized = 50 categories!**

## Step 6: Weekly Updates (Automated)

```bash
# Add to crontab
crontab -e

# Add this line (runs every Monday at 3 AM):
0 3 * * 1 cd /path/to/stremio-ai-addon && docker-compose run --rm worker python workers/weekly_update.py
```

**What this does:**
- Tags new releases from the past week (~500 items)
- Updates all catalogs
- Costs $0 (within free tier)
- Takes ~5 minutes

## Troubleshooting

### "Configuration errors"
```bash
# Check .env file
cat .env | grep -E "TMDB|GEMINI|TRAKT|MASTER|SECRET|BASE"

# All should have real values, not "your_key_here"
```

### "Database connection failed"
```bash
# Check PostgreSQL is running
docker-compose ps postgres

# Should show "Up" status
```

### "Gemini API error"
```bash
# Check if you have billing enabled (for initial build only)
# After initial build, disable it and use free tier
```

### "Empty catalogs"
```bash
# Check if initial build completed
docker-compose run --rm app python3 -c "
from app.database import get_db
from app.models import UniversalCatalogContent
with get_db() as db:
    count = db.query(UniversalCatalogContent).count()
    print(f'Catalog items: {count}')
"

# Should show 4,000+ items
# If 0, run initial build again
```

### "Trakt OAuth fails"
```bash
# Verify redirect URI matches EXACTLY
# In .env: TRAKT_REDIRECT_URI=https://yourdomain.com/auth/trakt/callback
# In Trakt app: Must be identical (including https://)
```

## Verification Checklist

After setup, verify:

- [ ] `curl http://localhost:8000/health` returns `{"status":"healthy"}`
- [ ] `curl http://localhost:8000/manifest/universal.json` returns JSON with 40 catalogs
- [ ] Database has data:
  ```bash
  docker-compose exec postgres psql -U postgres -d stremio_ai -c "SELECT COUNT(*) FROM movie_tags;"
  # Should show 7,500,000+ rows
  ```
- [ ] Stremio shows your addon in the addons list
- [ ] Catalogs appear in Stremio discover section
- [ ] Each catalog has ~100 movies

## What You Get

After setup:
- ✅ **40 universal categories** (e.g., "Dark & Gritty Crime Dramas")
- ✅ **10 personalized categories** (if using Trakt, e.g., "Because You Watched Blade Runner")
- ✅ **100 items per catalog** (ranked by AI relevance)
- ✅ **150,000 tagged titles** (100K movies + 50K shows)
- ✅ **<100ms response time** (PostgreSQL-powered)
- ✅ **$0/month cost** (free tier forever)
- ✅ **Weekly auto-updates** (new releases)

## Costs Summary

**One-Time**:
- Initial build: $5 (Gemini AI tagging)
- Time: ~3 hours

**Ongoing**:
- Monthly: $0 (free tier)
- Weekly updates: $0 (free tier)
- Maintenance: 5 min/week (automated)

**Total First Year**: $5  
**Total Following Years**: $0

## Next Steps

1. **Customize categories** - See README for how to add more
2. **Invite friends** - Share master password with trusted users
3. **Monitor usage** - Check logs occasionally
4. **Backup database** - Set up automated backups
5. **Update code** - `git pull && docker-compose up -d --build`

## Support

- **README.md** - Full documentation
- **TESTING_GUIDE.md** - Comprehensive testing
- **PROJECT_SUMMARY.md** - Architecture overview
- **GitHub Issues** - Report bugs
- **Code Comments** - Inline documentation

## Success!

If you see catalogs in Stremio, you're done! 🎉

Enjoy Netflix-style AI-powered content discovery in Stremio!

---

**Time to deploy**: ~4 hours (mostly initial build)  
**Cost**: $5 one-time  
**Complexity**: Low (just follow steps)  
**Result**: Production-ready addon with 50 AI-powered categories  

🚀 **Happy streaming!**
