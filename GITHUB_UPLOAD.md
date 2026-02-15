# How to Upload to GitHub (3 Easy Ways)

## Option 1: Automated Script (Easiest - 2 minutes)

```bash
cd stremio-ai-addon
./upload-to-github.sh
```

**That's it!** The script will:
1. Initialize git repository
2. Commit all files
3. Create GitHub repository (if you have GitHub CLI)
4. Push everything to GitHub

---

## Option 2: GitHub Desktop (Easiest if you prefer GUI)

### Download GitHub Desktop
- macOS/Windows: https://desktop.github.com
- Linux: https://github.com/shiftkey/desktop

### Steps:
1. Open GitHub Desktop
2. Click "Add" → "Add Existing Repository"
3. Select `stremio-ai-addon` folder
4. Click "Publish repository"
5. Choose name: `stremio-ai-addon`
6. Choose: Private (recommended)
7. Click "Publish Repository"

**Done!** Your code is on GitHub.

---

## Option 3: Manual Git Commands (5 minutes)

### Step 1: Install Git (if needed)
```bash
# macOS
brew install git

# Linux
sudo apt install git

# Windows
# Download from https://git-scm.com
```

### Step 2: Configure Git (first time only)
```bash
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

### Step 3: Create GitHub Repository
1. Go to https://github.com/new
2. Repository name: `stremio-ai-addon`
3. Select: **Private** ⚫ (recommended)
4. **DO NOT** check any boxes (no README, no .gitignore, no license)
5. Click **"Create repository"**

### Step 4: Upload Code
```bash
# Navigate to project
cd stremio-ai-addon

# Initialize git
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit: Stremio AI Addon"

# Add your GitHub repository (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/stremio-ai-addon.git

# Push to GitHub
git branch -M main
git push -u origin main
```

### Step 5: Enter Credentials
When prompted:
- **Username**: Your GitHub username
- **Password**: Use a **Personal Access Token** (not your password)

**To create a token**:
1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Select scopes: `repo` (all sub-items)
4. Click "Generate token"
5. **Copy the token** (you won't see it again!)
6. Use this token as your password

---

## Verify Upload

After uploading, verify:
1. Go to https://github.com/YOUR_USERNAME/stremio-ai-addon
2. You should see all files
3. Check that GitHub Actions workflow appears in "Actions" tab

---

## Enable GitHub Actions (Important!)

For automatic Docker builds:

1. Go to repository Settings → Actions → General
2. Under "Workflow permissions", select:
   - ✓ **Read and write permissions**
3. Click **Save**

This allows GitHub Actions to push Docker images.

---

## What Happens After Upload?

**Automatically (via GitHub Actions)**:
- ✅ Tests run on every push
- ✅ Docker image builds
- ✅ Image pushes to GitHub Container Registry
- ✅ You get notified of build status

**Your Docker image will be at**:
```
ghcr.io/YOUR_USERNAME/stremio-ai-addon:main
```

---

## Troubleshooting

### "Permission denied (publickey)"
**Solution**: Use HTTPS URL, not SSH:
```bash
git remote set-url origin https://github.com/YOUR_USERNAME/stremio-ai-addon.git
```

### "Authentication failed"
**Solution**: Use a Personal Access Token, not your password
- Create token: https://github.com/settings/tokens
- Scopes needed: `repo`

### "Repository already exists"
**Solution**: 
```bash
git remote remove origin
git remote add origin https://github.com/YOUR_USERNAME/stremio-ai-addon.git
git push -u origin main
```

### "Nothing to commit"
**Solution**: 
```bash
git add -A
git commit -m "Initial commit"
git push
```

---

## After Upload

### Update Code Later
```bash
# Make changes to code
git add .
git commit -m "Description of changes"
git push
```

### Pull on Unraid
```bash
# SSH into Unraid
ssh root@unraid-server

# Pull latest image
docker pull ghcr.io/YOUR_USERNAME/stremio-ai-addon:main

# Restart
cd /path/to/stremio-ai-addon
docker-compose pull
docker-compose up -d
```

---

## Recommended: Install GitHub CLI

Makes everything easier:

```bash
# macOS
brew install gh

# Linux
sudo apt install gh

# Windows
winget install GitHub.cli
```

**Then just run**:
```bash
cd stremio-ai-addon
gh auth login
gh repo create stremio-ai-addon --private --source=. --push
```

**Done in 30 seconds!**

---

## Summary

**Easiest**: Run `./upload-to-github.sh`  
**GUI**: Use GitHub Desktop  
**Manual**: Follow Option 3 steps  

All options take **< 5 minutes** and get your code safely on GitHub.

Choose whichever method you're most comfortable with!
