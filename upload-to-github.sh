#!/bin/bash
# GitHub Upload Script for Stremio AI Addon
# This script automates the entire GitHub upload process

set -e  # Exit on error

echo "=================================================="
echo "  Stremio AI Addon - GitHub Upload Script"
echo "=================================================="
echo ""

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo "❌ Git is not installed. Please install git first:"
    echo "   brew install git  # macOS"
    echo "   sudo apt install git  # Linux"
    exit 1
fi

# Check if GitHub CLI is installed (optional but recommended)
if command -v gh &> /dev/null; then
    echo "✅ GitHub CLI detected - will use for easier setup"
    USE_GH_CLI=true
else
    echo "ℹ️  GitHub CLI not found - will use manual git commands"
    echo "   Install with: brew install gh (macOS) or see https://cli.github.com"
    USE_GH_CLI=false
fi

# Get repository name
echo ""
read -p "Enter repository name (default: stremio-ai-addon): " REPO_NAME
REPO_NAME=${REPO_NAME:-stremio-ai-addon}

# Get GitHub username
echo ""
read -p "Enter your GitHub username: " GITHUB_USER

if [ -z "$GITHUB_USER" ]; then
    echo "❌ GitHub username is required"
    exit 1
fi

# Ask if repo should be private
echo ""
read -p "Make repository private? (Y/n): " MAKE_PRIVATE
MAKE_PRIVATE=${MAKE_PRIVATE:-Y}

# Navigate to project directory
cd "$(dirname "$0")"

echo ""
echo "=================================================="
echo "Step 1: Initializing Git Repository"
echo "=================================================="

# Initialize git if not already initialized
if [ ! -d .git ]; then
    git init
    echo "✅ Git repository initialized"
else
    echo "✅ Git repository already exists"
fi

# Configure git if needed
if [ -z "$(git config user.name)" ]; then
    read -p "Enter your name for git commits: " GIT_NAME
    git config user.name "$GIT_NAME"
fi

if [ -z "$(git config user.email)" ]; then
    read -p "Enter your email for git commits: " GIT_EMAIL
    git config user.email "$GIT_EMAIL"
fi

echo ""
echo "=================================================="
echo "Step 2: Preparing Files"
echo "=================================================="

# Add all files
git add -A

# Check if there are changes to commit
if git diff --staged --quiet; then
    echo "ℹ️  No new changes to commit"
else
    # Create initial commit
    git commit -m "Initial commit: Stremio AI Addon

- Complete FastAPI application
- Gemini AI tagging engine
- TMDB and Trakt integration
- PostgreSQL database
- Docker deployment
- Comprehensive tests
- Full documentation"
    
    echo "✅ Files committed to git"
fi

echo ""
echo "=================================================="
echo "Step 3: Creating GitHub Repository"
echo "=================================================="

if [ "$USE_GH_CLI" = true ]; then
    # Use GitHub CLI to create repo
    echo "Creating repository using GitHub CLI..."
    
    if [ "${MAKE_PRIVATE,,}" = "y" ]; then
        gh repo create "$GITHUB_USER/$REPO_NAME" --private --source=. --remote=origin --push
    else
        gh repo create "$GITHUB_USER/$REPO_NAME" --public --source=. --remote=origin --push
    fi
    
    echo "✅ Repository created and pushed!"
    
else
    # Manual method
    echo ""
    echo "📋 Manual Setup Required:"
    echo "=================================================="
    echo "1. Go to: https://github.com/new"
    echo "2. Repository name: $REPO_NAME"
    
    if [ "${MAKE_PRIVATE,,}" = "y" ]; then
        echo "3. Select: ⚫ Private"
    else
        echo "3. Select: 🌍 Public"
    fi
    
    echo "4. DO NOT initialize with README, .gitignore, or license"
    echo "5. Click 'Create repository'"
    echo ""
    read -p "Press ENTER after you've created the repository..."
    
    # Add remote and push
    REPO_URL="https://github.com/$GITHUB_USER/$REPO_NAME.git"
    
    # Remove existing origin if it exists
    git remote remove origin 2>/dev/null || true
    
    # Add new origin
    git remote add origin "$REPO_URL"
    
    # Rename branch to main if needed
    git branch -M main
    
    # Push to GitHub
    echo "Pushing to GitHub..."
    git push -u origin main
    
    echo "✅ Code pushed to GitHub!"
fi

echo ""
echo "=================================================="
echo "Step 4: Setting Up GitHub Actions"
echo "=================================================="

echo "✅ GitHub Actions workflow already configured in .github/workflows/build.yml"
echo "   It will automatically run on every push to main branch"

echo ""
echo "=================================================="
echo "Step 5: Configuring GitHub Container Registry"
echo "=================================================="

echo ""
echo "📋 To enable automatic Docker image builds:"
echo "1. Go to: https://github.com/$GITHUB_USER/$REPO_NAME/settings/actions"
echo "2. Under 'Workflow permissions', select:"
echo "   ✓ Read and write permissions"
echo "3. Click 'Save'"
echo ""
echo "This allows GitHub Actions to push Docker images to ghcr.io"

echo ""
echo "=================================================="
echo "✅ UPLOAD COMPLETE!"
echo "=================================================="
echo ""
echo "Your repository is now available at:"
echo "🔗 https://github.com/$GITHUB_USER/$REPO_NAME"
echo ""
echo "Next Steps:"
echo "1. Review your code on GitHub"
echo "2. Set up secrets for deployment (if needed)"
echo "3. Follow QUICKSTART.md to deploy"
echo ""
echo "GitHub Actions will automatically:"
echo "  • Run tests on every push"
echo "  • Build Docker images"
echo "  • Push to GitHub Container Registry"
echo ""
echo "To pull the Docker image on your Unraid server:"
echo "  docker pull ghcr.io/$GITHUB_USER/$REPO_NAME:main"
echo ""
echo "=================================================="
echo "🚀 Happy coding!"
echo "=================================================="
