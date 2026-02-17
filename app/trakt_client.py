"""
Trakt API client with OAuth 2.0 authentication.

Handles user authentication and fetching watch history.
"""

from typing import List, Dict, Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


class TraktClient:
    """Client for Trakt API with OAuth authentication."""

    BASE_URL = "https://api.trakt.tv"
    AUTH_URL = "https://trakt.tv/oauth/authorize"
    TOKEN_URL = "https://api.trakt.tv/oauth/token"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.client_id = client_id or settings.trakt_client_id
        self.client_secret = client_secret or settings.trakt_client_secret
        self.redirect_uri = redirect_uri or settings.trakt_redirect_uri

        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    def get_authorization_url(self, state: str) -> str:
        """
        Get OAuth authorization URL for user to visit.

        Args:
            state: Random state string for CSRF protection

        Returns:
            Authorization URL
        """
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }

        query = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.AUTH_URL}?{query}"

    async def exchange_code_for_token(self, code: str) -> Dict:
        """
        Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Token response with access_token, refresh_token, etc.
        """
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            response = await self.client.post(self.TOKEN_URL, json=data)
            response.raise_for_status()

            token_data = response.json()
            logger.info("Successfully exchanged code for access token")

            return token_data
        except Exception as e:
            logger.error(f"Failed to exchange code for token: {e}")
            raise

    async def refresh_access_token(self, refresh_token: str) -> Dict:
        """
        Refresh expired access token.

        Args:
            refresh_token: Refresh token

        Returns:
            New token response
        """
        data = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "refresh_token",
        }

        try:
            response = await self.client.post(self.TOKEN_URL, json=data)
            response.raise_for_status()

            token_data = response.json()
            logger.info("Successfully refreshed access token")

            return token_data
        except Exception as e:
            logger.error(f"Failed to refresh access token: {e}")
            raise

    def _get_headers(self, access_token: str) -> Dict:
        """Get headers for authenticated requests."""
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": f"Bearer {access_token}",
        }

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        access_token: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Dict:
        """
        Make authenticated request to Trakt API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint
            access_token: User's access token
            params: Query parameters
            json_data: JSON body

        Returns:
            JSON response
        """
        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_headers(access_token)

        try:
            response = await self.client.request(
                method=method, url=url, headers=headers, params=params, json=json_data
            )
            response.raise_for_status()

            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Trakt API error: {e.response.status_code} - {e.response.text}"
            )
            raise
        except Exception as e:
            logger.error(f"Trakt request failed: {e}")
            raise

    async def get_user_profile(self, access_token: str) -> Dict:
        """Get user's Trakt profile."""
        return await self._request("GET", "/users/me", access_token)

    async def get_user_watched_movies(
        self, access_token: str, limit: int = 100
    ) -> List[Dict]:
        """
        Get user's watched movies.

        Args:
            access_token: User's access token
            limit: Maximum number of items to fetch

        Returns:
            List of watched movies with metadata
        """
        try:
            response = await self._request(
                "GET",
                "/users/me/watched/movies",
                access_token,
                params={"extended": "full"},
            )

            logger.info(f"Fetched {len(response)} watched movies")
            return response[:limit]
        except Exception as e:
            logger.error(f"Failed to fetch watched movies: {e}")
            return []

    async def get_user_watched_shows(
        self, access_token: str, limit: int = 100
    ) -> List[Dict]:
        """
        Get user's watched TV shows.

        Args:
            access_token: User's access token
            limit: Maximum number of items to fetch

        Returns:
            List of watched shows with metadata
        """
        try:
            response = await self._request(
                "GET",
                "/users/me/watched/shows",
                access_token,
                params={"extended": "full"},
            )

            logger.info(f"Fetched {len(response)} watched shows")
            return response[:limit]
        except Exception as e:
            logger.error(f"Failed to fetch watched shows: {e}")
            return []

    async def get_user_history(
        self, access_token: str, media_type: Optional[str] = None, limit: int = 100
    ) -> List[Dict]:
        """
        Get user's watch history (recently watched).

        Args:
            access_token: User's access token
            media_type: Filter by 'movies' or 'shows'
            limit: Maximum number of items

        Returns:
            List of history items
        """
        endpoint = "/users/me/history"
        if media_type:
            endpoint += f"/{media_type}"

        try:
            response = await self._request(
                "GET",
                endpoint,
                access_token,
                params={"page": 1, "limit": limit, "extended": "full"},
            )

            logger.info(f"Fetched {len(response)} history items")
            return response  # type: ignore[return-value]
        except Exception as e:
            logger.error(f"Failed to fetch history: {e}")
            return []

    async def get_trending_movies(
        self, access_token: str, limit: int = 40
    ) -> List[Dict]:
        """Get movies trending on Trakt right now."""
        try:
            response = await self._request(
                "GET",
                "/movies/trending",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} trending movies")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch trending movies: {e}")
            return []

    async def get_trending_shows(
        self, access_token: str, limit: int = 40
    ) -> List[Dict]:
        """Get shows trending on Trakt right now."""
        try:
            response = await self._request(
                "GET",
                "/shows/trending",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} trending shows")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch trending shows: {e}")
            return []

    async def get_watched_daily_movies(
        self, access_token: str, limit: int = 10
    ) -> List[Dict]:
        """Get most watched movies today (by unique viewers)."""
        try:
            response = await self._request(
                "GET",
                "/movies/watched/daily",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} daily watched movies")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch daily watched movies: {e}")
            return []

    async def get_watched_daily_shows(
        self, access_token: str, limit: int = 10
    ) -> List[Dict]:
        """Get most watched shows today (by unique viewers)."""
        try:
            response = await self._request(
                "GET",
                "/shows/watched/daily",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} daily watched shows")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch daily watched shows: {e}")
            return []

    async def get_popular_movies(
        self, access_token: str, limit: int = 40
    ) -> List[Dict]:
        """Get most popular movies on Trakt (all-time, by rating + watch count)."""
        try:
            response = await self._request(
                "GET",
                "/movies/popular",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} popular movies")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch popular movies: {e}")
            return []

    async def get_popular_shows(self, access_token: str, limit: int = 40) -> List[Dict]:
        """Get most popular shows on Trakt (all-time, by rating + watch count)."""
        try:
            response = await self._request(
                "GET",
                "/shows/popular",
                access_token,
                params={"limit": limit, "extended": "full"},
            )
            logger.info(f"Fetched {len(response)} popular shows")
            return response
        except Exception as e:
            logger.error(f"Failed to fetch popular shows: {e}")
            return []

    def extract_tmdb_ids(self, trakt_items: List[Dict], media_type: str) -> List[int]:
        """
        Extract TMDB IDs from Trakt response.

        Args:
            trakt_items: List of items from Trakt API
            media_type: 'movie' or 'show'

        Returns:
            List of TMDB IDs
        """
        tmdb_ids = []

        for item in trakt_items:
            # Trakt response structure varies by endpoint
            if media_type in item:
                media = item[media_type]
            elif "movie" in item:
                media = item["movie"]
            elif "show" in item:
                media = item["show"]
            else:
                media = item

            # Get TMDB ID from ids object
            ids = media.get("ids", {})
            tmdb_id = ids.get("tmdb")

            if tmdb_id:
                tmdb_ids.append(tmdb_id)

        logger.info(f"Extracted {len(tmdb_ids)} TMDB IDs from Trakt data")
        return tmdb_ids


# Global client instance
trakt_client = TraktClient()
