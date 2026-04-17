"""
TMDB (The Movie Database) API client.

Handles all interactions with TMDB API for movie and TV show metadata.
"""

from typing import List, Dict, Optional, Literal
from datetime import datetime, timedelta
import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings


class TMDBServerError(Exception):
    """TMDB returned a 5xx error (not worth retrying, especially at deep pages)."""

    pass


def _is_retryable(exc: BaseException) -> bool:
    """Retry on transient/connection errors, not TMDB 5xx server errors."""
    if isinstance(exc, TMDBServerError):
        return False
    # Always retry connection-level errors (VPN/Gluetun reconnects)
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, OSError)):
        return True
    return True  # default: retry


MediaType = Literal["movie", "tv"]

# Standard TMDB genre IDs — avoids an extra API call to /genre/movie/list
# Combined movie + TV genres (TMDB uses the same IDs across both)
_GENRE_MAP: dict[int, str] = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
    10759: "Action & Adventure",
    10762: "Kids",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
}


class TMDBClient:
    """Client for TMDB API."""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.tmdb_api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
    )
    async def _request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """
        Make request to TMDB API with retry logic.

        Args:
            endpoint: API endpoint (e.g., '/movie/550')
            params: Query parameters

        Returns:
            JSON response as dictionary
        """
        if params is None:
            params = {}

        params["api_key"] = self.api_key

        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status >= 500:
                raise TMDBServerError(
                    f"TMDB server error {status} on {endpoint}"
                ) from e
            logger.error(f"TMDB API error: {status} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"TMDB request failed: {e}")
            raise

    async def get_movie(self, tmdb_id: int) -> Dict:
        """Get detailed movie information (includes external_ids for IMDB)."""
        return await self._request(
            f"/movie/{tmdb_id}",
            params={
                "append_to_response": "credits,keywords,external_ids,images,videos",
                "include_image_language": "en,null",
            },
        )

    async def get_tv_show(self, tmdb_id: int) -> Dict:
        """Get detailed TV show information (includes external_ids for IMDB)."""
        return await self._request(
            f"/tv/{tmdb_id}",
            params={
                "append_to_response": "credits,keywords,external_ids,images,videos",
                "include_image_language": "en,null",
            },
        )

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> Dict:
        """Get detailed information for a specific TV season."""
        return await self._request(f"/tv/{tmdb_id}/season/{season_number}")

    async def get_movie_release_dates(self, tmdb_id: int) -> Dict:
        """Get release dates for a movie by country and type."""
        return await self._request(f"/movie/{tmdb_id}/release_dates")

    async def has_us_digital_release(self, tmdb_id: int) -> bool:
        """Check if a movie has a US digital or physical release date in the past.

        TMDB release types: 1=Premiere, 2=Theatrical(limited),
        3=Theatrical, 4=Digital, 5=Physical, 6=TV
        """
        type_names = {
            1: "Premiere",
            2: "Theatrical(limited)",
            3: "Theatrical",
            4: "Digital",
            5: "Physical",
            6: "TV",
        }
        try:
            data = await self.get_movie_release_dates(tmdb_id)
            today = datetime.now().strftime("%Y-%m-%d")
            us_found = False
            for entry in data.get("results", []):
                if entry.get("iso_3166_1") != "US":
                    continue
                us_found = True
                for rd in entry.get("release_dates", []):
                    rtype = rd.get("type")
                    rdate = (rd.get("release_date") or "")[:10]
                    logger.debug(
                        f"TMDB release check {tmdb_id}: "
                        f"type={rtype}({type_names.get(rtype, '?')}) "
                        f"date={rdate} today={today}"
                    )
                    if rtype in (4, 5) and rdate and rdate <= today:
                        logger.info(
                            f"Movie {tmdb_id} has US digital/physical release: "
                            f"type={type_names.get(rtype)} date={rdate}"
                        )
                        return True
            if not us_found:
                logger.debug(f"Movie {tmdb_id}: no US release data found")
            else:
                logger.debug(f"Movie {tmdb_id}: no US digital/physical release yet")
            return False
        except Exception as e:
            logger.warning(f"Release date check failed for {tmdb_id}: {e}")
            return False

    async def get_trending_movies(
        self, time_window: str = "day", page: int = 1
    ) -> Dict:
        """Get trending movies (day or week)."""
        return await self._request(
            f"/trending/movie/{time_window}", params={"page": page}
        )

    async def get_trending_tv_shows(
        self, time_window: str = "day", page: int = 1
    ) -> Dict:
        """Get trending TV shows (day or week)."""
        return await self._request(f"/trending/tv/{time_window}", params={"page": page})

    async def get_similar_movies(self, tmdb_id: int, page: int = 1) -> Dict:
        """Get movies similar to the given movie."""
        return await self._request(f"/movie/{tmdb_id}/similar", params={"page": page})

    async def get_similar_tv_shows(self, tmdb_id: int, page: int = 1) -> Dict:
        """Get TV shows similar to the given show."""
        return await self._request(f"/tv/{tmdb_id}/similar", params={"page": page})

    async def get_popular_movies(self, page: int = 1) -> Dict:
        """Get popular movies."""
        return await self._request("/movie/popular", params={"page": page})

    async def get_popular_tv_shows(self, page: int = 1) -> Dict:
        """Get popular TV shows."""
        return await self._request("/tv/popular", params={"page": page})

    async def get_top_rated_movies(self, page: int = 1) -> Dict:
        """Get top rated movies."""
        return await self._request("/movie/top_rated", params={"page": page})

    async def get_top_rated_tv_shows(self, page: int = 1) -> Dict:
        """Get top rated TV shows."""
        return await self._request("/tv/top_rated", params={"page": page})

    async def discover_movies(
        self,
        page: int = 1,
        sort_by: str = "popularity.desc",
        year: Optional[int] = None,
        with_genres: Optional[str] = None,
        vote_average_gte: Optional[float] = None,
    ) -> Dict:
        """
        Discover movies with filters.

        Args:
            page: Page number
            sort_by: Sort order (popularity.desc, vote_average.desc, etc.)
            year: Filter by release year
            with_genres: Comma-separated genre IDs
            vote_average_gte: Minimum vote average
        """
        params = {
            "page": page,
            "sort_by": sort_by,
            "include_adult": False,
            "include_video": False,
        }

        if year:
            params["primary_release_year"] = year
        if with_genres:
            params["with_genres"] = with_genres
        if vote_average_gte:
            params["vote_average.gte"] = vote_average_gte

        return await self._request("/discover/movie", params=params)

    async def discover_tv_shows(
        self,
        page: int = 1,
        sort_by: str = "popularity.desc",
        first_air_date_year: Optional[int] = None,
        with_genres: Optional[str] = None,
        vote_average_gte: Optional[float] = None,
    ) -> Dict:
        """
        Discover TV shows with filters.

        Args:
            page: Page number
            sort_by: Sort order
            first_air_date_year: Filter by first air date year
            with_genres: Comma-separated genre IDs
            vote_average_gte: Minimum vote average
        """
        params = {
            "page": page,
            "sort_by": sort_by,
            "include_adult": False,
        }

        if first_air_date_year:
            params["first_air_date_year"] = first_air_date_year
        if with_genres:
            params["with_genres"] = with_genres
        if vote_average_gte:
            params["vote_average.gte"] = vote_average_gte

        return await self._request("/discover/tv", params=params)

    async def get_movies_released_in_date_range(
        self, start_date: datetime, end_date: datetime, page: int = 1
    ) -> Dict:
        """
        Get movies released between two dates.

        Args:
            start_date: Start date
            end_date: End date
            page: Page number
        """
        params = {
            "page": page,
            "sort_by": "popularity.desc",
            "primary_release_date.gte": start_date.strftime("%Y-%m-%d"),
            "primary_release_date.lte": end_date.strftime("%Y-%m-%d"),
        }

        return await self._request("/discover/movie", params=params)

    async def get_tv_shows_aired_in_date_range(
        self, start_date: datetime, end_date: datetime, page: int = 1
    ) -> Dict:
        """
        Get TV shows that first aired between two dates.

        Args:
            start_date: Start date
            end_date: End date
            page: Page number
        """
        params = {
            "page": page,
            "sort_by": "popularity.desc",
            "first_air_date.gte": start_date.strftime("%Y-%m-%d"),
            "first_air_date.lte": end_date.strftime("%Y-%m-%d"),
        }

        return await self._request("/discover/tv", params=params)

    async def get_new_releases_this_week(self, media_type: MediaType) -> List[Dict]:
        """
        Get movies or TV shows released in the past week.

        Args:
            media_type: 'movie' or 'tv'

        Returns:
            List of media items
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        all_results = []
        page = 1

        while page <= 5:  # Limit to 5 pages (100 items)
            if media_type == "movie":
                response = await self.get_movies_released_in_date_range(
                    start_date, end_date, page
                )
            else:
                response = await self.get_tv_shows_aired_in_date_range(
                    start_date, end_date, page
                )

            results = response.get("results", [])
            if not results:
                break

            all_results.extend(results)

            if page >= response.get("total_pages", 1):
                break

            page += 1

        logger.info(f"Found {len(all_results)} new {media_type} releases this week")
        return all_results

    async def get_popular_items(
        self, media_type: MediaType, limit: int = 10000
    ) -> List[Dict]:
        """
        Get popular movies or TV shows.

        Args:
            media_type: 'movie' or 'tv'
            limit: Maximum number of items to fetch

        Returns:
            List of media items
        """
        all_results = []
        page = 1
        max_pages = min(500, (limit // 20) + 1)  # TMDB limits to 500 pages

        while page <= max_pages:
            try:
                if media_type == "movie":
                    response = await self.get_popular_movies(page)
                else:
                    response = await self.get_popular_tv_shows(page)
            except TMDBServerError:
                logger.debug(f"Popular {media_type} reached TMDB limit at page {page}")
                break

            results = response.get("results", [])
            if not results:
                break

            all_results.extend(results)

            if len(all_results) >= limit:
                break

            if page >= response.get("total_pages", 1):
                break

            page += 1

        logger.info(f"Fetched {len(all_results)} popular {media_type} items")
        return all_results[:limit]

    async def _paginate_discover(
        self,
        media_type: MediaType,
        params: Dict,
        seen_ids: set,
        max_pages: int = 500,
    ) -> List[Dict]:
        """Paginate through a discover query, skipping already-seen IDs."""
        results = []
        endpoint = "/discover/movie" if media_type == "movie" else "/discover/tv"

        for page in range(1, max_pages + 1):
            params["page"] = page
            try:
                response = await self._request(endpoint, params=params.copy())
            except TMDBServerError:
                logger.debug(f"Discover {media_type} reached TMDB limit at page {page}")
                break
            except Exception as e:
                logger.warning(f"Discover page {page} failed: {e}")
                break

            items = response.get("results", [])
            if not items:
                break

            for item in items:
                tmdb_id = item.get("id")
                if tmdb_id and tmdb_id not in seen_ids:
                    seen_ids.add(tmdb_id)
                    results.append(item)

            if page >= response.get("total_pages", 1):
                break

        return results

    async def fetch_all_items(
        self, media_type: MediaType, limit: int = 100000
    ) -> List[Dict]:
        """
        Fetch items using multiple strategies to maximize catalog coverage.

        Strategies applied in order:
        1. Popular endpoint (baseline)
        2. Top rated
        3. Discover by year (each year gets its own 500-page window)
        4. Discover by vote average (hidden gems with good ratings)
        5. Discover by language (international cinema)

        Args:
            media_type: 'movie' or 'tv'
            limit: Maximum number of unique items to fetch

        Returns:
            Deduplicated list of media items
        """
        seen_ids: set = set()
        all_items: List[Dict] = []

        def _add_items(items: List[Dict], label: str):
            new = 0
            for item in items:
                tmdb_id = item.get("id")
                if tmdb_id and tmdb_id not in seen_ids:
                    seen_ids.add(tmdb_id)
                    all_items.append(item)
                    new += 1
            logger.info(
                f"[{media_type}] {label}: +{new} new items "
                f"(total: {len(all_items)})"
            )

        # --- Strategy 1: Popular endpoint ---
        popular = await self.get_popular_items(media_type, limit=10000)
        _add_items(popular, "Popular")

        if len(all_items) >= limit:
            return all_items[:limit]

        # --- Strategy 2: Top rated ---
        logger.info(f"[{media_type}] Fetching top rated...")
        top_rated = []
        for page in range(1, 501):
            try:
                if media_type == "movie":
                    resp = await self.get_top_rated_movies(page)
                else:
                    resp = await self.get_top_rated_tv_shows(page)
            except TMDBServerError:
                logger.debug(
                    f"[{media_type}] Top rated reached TMDB limit at page {page}"
                )
                break
            except Exception:
                break
            items = resp.get("results", [])
            if not items:
                break
            top_rated.extend(items)
            if page >= resp.get("total_pages", 1):
                break
        _add_items(top_rated, "Top Rated")

        if len(all_items) >= limit:
            return all_items[:limit]

        # --- Strategy 3: Discover by year (most popular per year) ---
        current_year = datetime.now().year
        year_key = (
            "primary_release_year" if media_type == "movie" else "first_air_date_year"
        )

        for year in range(current_year, 1969, -1):
            if len(all_items) >= limit:
                break
            logger.info(f"[{media_type}] Discovering year {year}...")
            params = {
                "sort_by": "popularity.desc",
                "include_adult": False,
                year_key: year,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Year {year}: +{len(items)} new "
                f"(total: {len(all_items)})"
            )

        if len(all_items) >= limit:
            return all_items[:limit]

        # --- Strategy 4: Hidden gems (high vote avg, lower popularity) ---
        logger.info(f"[{media_type}] Fetching hidden gems...")
        for min_votes in [100, 50, 20]:
            if len(all_items) >= limit:
                break
            params = {
                "sort_by": "vote_average.desc",
                "vote_count.gte": min_votes,
                "include_adult": False,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Hidden gems (votes>={min_votes}): "
                f"+{len(items)} new (total: {len(all_items)})"
            )

        if len(all_items) >= limit:
            return all_items[:limit]

        # --- Strategy 5: International cinema by language ---
        languages = [
            "ko",
            "ja",
            "fr",
            "es",
            "de",
            "it",
            "hi",
            "zh",
            "pt",
            "sv",
            "da",
            "no",
            "th",
            "tr",
            "pl",
        ]
        logger.info(f"[{media_type}] Fetching international content...")
        for lang in languages:
            if len(all_items) >= limit:
                break
            params = {
                "sort_by": "popularity.desc",
                "with_original_language": lang,
                "include_adult": False,
            }
            if media_type == "movie":
                params["include_video"] = False
            items = await self._paginate_discover(
                media_type, params, seen_ids, max_pages=500
            )
            all_items.extend(items)
            logger.info(
                f"[{media_type}] Language '{lang}': +{len(items)} new "
                f"(total: {len(all_items)})"
            )

        logger.info(f"[{media_type}] Fetch complete: {len(all_items)} unique items")
        return all_items[:limit]

    def extract_metadata(self, item: Dict, media_type: MediaType) -> Dict:
        """
        Extract relevant metadata from TMDB response.

        Args:
            item: TMDB API response
            media_type: 'movie' or 'tv'

        Returns:
            Cleaned metadata dictionary
        """
        if media_type == "movie":
            title = item.get("title", "Unknown")
            original_title = item.get("original_title")
            release_date = item.get("release_date", "")
        else:
            title = item.get("name", "Unknown")
            original_title = item.get("original_name")
            release_date = item.get("first_air_date", "")

        # Extract imdb_id from external_ids (only present in detail responses)
        imdb_id = (
            item.get("external_ids", {}).get("imdb_id")
            if isinstance(item.get("external_ids"), dict)
            else None
        )

        # Extract logo path (prefer English logos)
        logo_path = None
        logos = (
            item.get("images", {}).get("logos", [])
            if isinstance(item.get("images"), dict)
            else []
        )
        if logos:
            en_logos = [lg for lg in logos if lg.get("iso_639_1") in ("en", None)]
            chosen = en_logos[0] if en_logos else logos[0]
            logo_path = chosen.get("file_path")

        return {
            "tmdb_id": item["id"],
            "media_type": media_type,
            "title": title,
            "original_title": original_title,
            "original_language": item.get("original_language"),
            "adult": item.get("adult", False),
            "overview": item.get("overview", ""),
            "release_date": release_date,
            "genres": (
                [g["name"] for g in item["genres"]]
                if "genres" in item
                and item["genres"]
                and isinstance(item["genres"][0], dict)
                else [
                    _GENRE_MAP[gid]
                    for gid in item.get("genre_ids", [])
                    if gid in _GENRE_MAP
                ]
            ),
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
            "imdb_id": imdb_id,
            "logo_path": logo_path,
            "vote_average": item.get("vote_average"),
            "vote_count": item.get("vote_count"),
            "popularity": item.get("popularity"),
            "number_of_seasons": (
                item.get("number_of_seasons") if media_type == "tv" else None
            ),
            "number_of_episodes": (
                item.get("number_of_episodes") if media_type == "tv" else None
            ),
            "raw_data": item,
        }


# Global client instance
tmdb_client = TMDBClient()
