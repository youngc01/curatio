"""
Gemini AI client for semantic tagging of movies and TV shows.

This is the core AI component that analyzes media and generates Netflix-style tags.
"""

from typing import List, Dict, Optional
import asyncio
import json
import google.generativeai as genai
from loguru import logger
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

from app.config import settings


def _is_retryable_error(exception: BaseException) -> bool:
    """Check if an error is retryable (rate limit or server error)."""
    error_str = str(exception)
    return "429" in error_str or "Resource exhausted" in error_str or "503" in error_str


class GeminiTaggingEngine:
    """AI engine for tagging movies and TV shows with semantic attributes."""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model_name = model_name or settings.gemini_model

        # Configure Gemini
        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(self.model_name)

        # Tag categories
        self.tag_categories = {
            "genre": [
                "Action",
                "Crime",
                "Sci-Fi",
                "Horror",
                "Drama",
                "Comedy",
                "Thriller",
                "Romance",
                "Fantasy",
                "Mystery",
                "Documentary",
                "Animation",
            ],
            "mood": [
                "Dark",
                "Gritty",
                "Feel-Good",
                "Quirky",
                "Suspenseful",
                "Witty",
                "Heartwarming",
                "Intense",
                "Cerebral",
                "Lighthearted",
            ],
            "era": [
                "1920s",
                "1930s",
                "1940s",
                "1950s",
                "1960s",
                "1970s",
                "1980s",
                "1990s",
                "2000s",
                "2010s",
                "2020s",
            ],
            "region": [
                "British",
                "Korean",
                "French",
                "Scandinavian",
                "Japanese",
                "Italian",
                "Spanish",
                "Latin American",
                "Indian",
                "Chinese",
            ],
            "plot": [
                "Heist",
                "Time Travel",
                "Revenge",
                "Courtroom",
                "Found Footage",
                "Coming-of-Age",
                "Survival",
                "Conspiracy",
                "Detective",
            ],
            "style": [
                "Neo-Noir",
                "Arthouse",
                "Independent",
                "Cyberpunk",
                "Visually Stunning",
                "Minimalist",
                "Period Costume",
                "Neon Visuals",
            ],
            "character": [
                "Strong Female Lead",
                "Anti-Hero",
                "Ensemble Cast",
                "Character Study",
                "Underdog",
                "Morally Ambiguous",
            ],
        }

    def _create_tagging_prompt(self, items: List[Dict]) -> str:
        """
        Create prompt for Gemini to tag multiple items in one request.

        Args:
            items: List of media items with metadata

        Returns:
            Formatted prompt string
        """
        prompt = """You are an expert film critic and Netflix-style content curator. Your job is to analyze movies and TV shows and tag them with semantic attributes that help users discover content.

Tag each item with attributes from these categories:

GENRES: Action, Crime, Sci-Fi, Horror, Drama, Comedy, Thriller, Romance, Fantasy, Mystery, Documentary, Animation
MOODS: Dark, Gritty, Feel-Good, Quirky, Suspenseful, Witty, Heartwarming, Intense, Cerebral, Lighthearted, Slow-Burn
ERAS: 1920s, 1930s, 1940s, 1950s, 1960s, 1970s, 1980s, 1990s, 2000s, 2010s, 2020s
REGIONS: British, Korean, French, Scandinavian, Japanese, Italian, Spanish, Latin American, Indian, Chinese, American
PLOT ELEMENTS: Heist, Time Travel, Revenge, Courtroom, Found Footage, Coming-of-Age, Survival, Conspiracy, Detective, Mystery
STYLES: Neo-Noir, Arthouse, Independent, Cyberpunk, Visually Stunning, Minimalist, Period Costume, Neon Visuals, Satirical
CHARACTER TYPES: Strong Female Lead, Anti-Hero, Ensemble Cast, Character Study, Underdog, Morally Ambiguous

For each item, assign:
- A confidence score (0.0 to 1.0) for how strongly each tag applies
- Only include tags with confidence >= 0.5
- Each item should have 5-15 tags total
- Be specific and nuanced (e.g., Blade Runner 2049 is "Neo-Noir", "Cyberpunk", "Visually Stunning", "Slow-Burn", "Sci-Fi")

Respond ONLY with valid JSON in this exact format (no markdown, no preamble):
```json
{
  "items": [
    {
      "tmdb_id": 123,
      "tags": {
        "Dark": 0.95,
        "Cyberpunk": 0.90,
        "Sci-Fi": 1.0,
        "Neo-Noir": 0.85,
        "Visually Stunning": 0.95,
        "Slow-Burn": 0.80
      }
    }
  ]
}
```

Here are the items to tag:

"""

        for item in items:
            prompt += f"""
ITEM {item['tmdb_id']}:
Title: {item['title']}
Type: {item['media_type']}
Release: {item.get('release_date', 'Unknown')}
Genres: {', '.join(item.get('genres', []))}
Overview: {item.get('overview', 'No description available')[:500]}

"""

        prompt += "\nReturn ONLY the JSON response with tags for all items. No explanation needed."

        return prompt

    @retry(
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=lambda retry_state: logger.warning(
            f"Rate limited, retrying in {retry_state.next_action.sleep:.0f}s "
            f"(attempt {retry_state.attempt_number}/6)..."
        ),
    )
    async def tag_items(self, items: List[Dict]) -> List[Dict]:
        """
        Tag multiple items with semantic attributes using Gemini AI.

        Args:
            items: List of media items with metadata (from TMDB)

        Returns:
            List of items with tags: [{"tmdb_id": int, "tags": {"tag": confidence}}]
        """
        if not items:
            return []

        prompt = self._create_tagging_prompt(items)

        try:
            logger.info(f"Tagging {len(items)} items with Gemini...")

            response = self.model.generate_content(prompt)

            # Extract JSON from response
            response_text = response.text.strip()

            # Remove markdown code blocks if present
            if response_text.startswith("```json"):
                response_text = response_text[7:]  # Remove ```json
            if response_text.startswith("```"):
                response_text = response_text[3:]  # Remove ```
            if response_text.endswith("```"):
                response_text = response_text[:-3]  # Remove closing ```

            response_text = response_text.strip()

            # Parse JSON
            result = json.loads(response_text)

            tagged_items = result.get("items", [])

            logger.info(f"Successfully tagged {len(tagged_items)} items")

            return tagged_items

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            logger.error(f"Response text: {response_text[:500]}")
            raise
        except Exception as e:
            logger.error(f"Gemini tagging failed: {e}")
            raise

    async def analyze_user_taste(self, watched_items: List[Dict]) -> Dict:
        """
        Analyze user's viewing history to understand their taste.

        Args:
            watched_items: List of movies/shows the user has watched

        Returns:
            Analysis with top patterns, recommended categories, etc.
        """
        if not watched_items:
            return {"patterns": [], "categories": []}

        prompt = """You are analyzing a user's viewing history to understand their taste in movies and TV shows.

Based on what they've watched, identify:
1. Top 5 strongest patterns in their taste (e.g., "loves dark cyberpunk with philosophical themes")
2. 10 Netflix-style personalized category names that would appeal to them
3. Tags that appear frequently in their watched content

Watched content:
"""

        for item in watched_items[:50]:  # Limit to 50 for context window
            prompt += f"- {item.get('title', 'Unknown')} ({item.get('media_type', 'movie')})\n"

        prompt += """
Respond ONLY with valid JSON (no markdown):
{
  "patterns": ["pattern 1", "pattern 2", ...],
  "personalized_categories": [
    {"name": "Because You Watched Blade Runner 2049", "description": "More cyberpunk neo-noir"},
    ...
  ],
  "top_tags": {"tag": frequency_score}
}
"""

        try:
            response = self.model.generate_content(prompt)
            response_text = response.text.strip()

            # Clean markdown
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            result = json.loads(response_text.strip())

            logger.info(
                f"Analyzed user taste: {len(result.get('patterns', []))} patterns found"
            )

            return result

        except Exception as e:
            logger.error(f"Failed to analyze user taste: {e}")
            return {"patterns": [], "personalized_categories": [], "top_tags": {}}

    async def find_similar_items(
        self, reference_item: Dict, candidate_items: List[Dict], limit: int = 100
    ) -> List[int]:
        """
        Find items similar to a reference item.

        Args:
            reference_item: The reference movie/show
            candidate_items: Pool of items to search
            limit: Number of similar items to return

        Returns:
            List of TMDB IDs ranked by similarity
        """
        # This would use embeddings or more sophisticated matching
        # For now, simplified implementation
        logger.info(f"Finding items similar to '{reference_item.get('title')}'")

        # Return top candidates by popularity (placeholder)
        sorted_items = sorted(
            candidate_items, key=lambda x: x.get("popularity", 0), reverse=True
        )

        return [item["tmdb_id"] for item in sorted_items[:limit]]


# Global instance
gemini_engine = GeminiTaggingEngine()
