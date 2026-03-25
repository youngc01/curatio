"""
AIOStreams stream proxy for Curatio.

Proxies stream requests to a self-hosted AIOStreams addon instance,
resolving TMDB IDs to IMDb IDs as needed.
"""

import time
from typing import Optional

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.models import MediaMetadata, AdminSetting
from app.config import settings

# In-memory cache: (video_id, tier) -> (timestamp, streams)
_stream_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_STREAM_CACHE_TTL = 300  # 5 minutes
_STREAM_CACHE_MAX = 500

# In-memory cache for AIOStreams URLs
_aiostreams_url_cache: dict[str, tuple[float, str]] = {}
_URL_CACHE_TTL = 60  # 1 minute


def _get_aiostreams_url(tier: str, db: Session) -> Optional[str]:
    """Get the AIOStreams URL for the given bandwidth tier from AdminSetting."""
    cache_key = f"aiostreams_{tier}"
    now = time.time()

    cached = _aiostreams_url_cache.get(cache_key)
    if cached and (now - cached[0]) < _URL_CACHE_TTL:
        return cached[1] or None

    key = f"aiostreams_{tier}_bw_url"
    setting = db.query(AdminSetting).filter(AdminSetting.key == key).first()
    url = setting.value.rstrip("/") if setting and setting.value else ""
    # Strip /manifest.json suffix if admin pasted the full manifest URL
    if url.endswith("/manifest.json"):
        url = url[: -len("/manifest.json")]
    _aiostreams_url_cache[cache_key] = (now, url)
    return url or None


def _resolve_imdb_id(tmdb_id: int, media_type: str, db: Session) -> Optional[str]:
    """Resolve a TMDB ID to an IMDb ID using the media_metadata table."""
    meta = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id,
            MediaMetadata.media_type == media_type,
        )
        .first()
    )
    if meta and meta.imdb_id:
        return meta.imdb_id
    return None


async def _fetch_imdb_from_tmdb(tmdb_id: int, media_type: str) -> Optional[str]:
    """Fetch IMDb ID from TMDB API (fallback when not in DB)."""
    tmdb_type = "movie" if media_type == "movie" else "tv"
    url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/external_ids"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"api_key": settings.tmdb_api_key})
            if resp.status_code == 200:
                data = resp.json()
                return data.get("imdb_id")
    except Exception as e:
        logger.warning(f"Failed to fetch IMDb ID from TMDB for {tmdb_id}: {e}")
    return None


def _parse_video_id(video_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parse a Stremio video_id into (imdb_id_with_suffix, tmdb_info).

    Possible formats:
    - "tt1234567" -> IMDb movie
    - "tt1234567:1:3" -> IMDb series S01E03
    - "tmdb:12345" -> TMDB movie
    - "tmdb:12345:1:3" -> TMDB series S01E03

    Returns (imdb_video_id, tmdb_key) where:
    - imdb_video_id is ready to send to AIOStreams (e.g. "tt1234567:1:3")
    - tmdb_key is "tmdb_id:media_type" if we need to resolve, else None
    """
    if video_id.startswith("tt"):
        return video_id, None

    if video_id.startswith("tmdb:"):
        parts = video_id.split(":")
        tmdb_id = parts[1]
        suffix = ":".join(parts[2:]) if len(parts) > 2 else ""
        media_type = "tv" if suffix else "movie"
        return None, f"{tmdb_id}:{media_type}:{suffix}"

    # Unknown format — try passing through as-is
    return video_id, None


async def get_streams(
    video_id: str, stremio_type: str, bandwidth_tier: str, db: Session
) -> list[dict]:
    """
    Fetch streams from AIOStreams for the given video.

    Args:
        video_id: Stremio video ID (IMDb or TMDB prefixed)
        stremio_type: "movie" or "series"
        bandwidth_tier: "low" or "high"
        db: Database session

    Returns:
        List of Stremio stream objects.
    """
    # Check cache
    cache_key = (video_id, bandwidth_tier)
    now = time.time()
    cached = _stream_cache.get(cache_key)
    if cached and (now - cached[0]) < _STREAM_CACHE_TTL:
        return cached[1]

    # Get AIOStreams URL
    aiostreams_url = _get_aiostreams_url(bandwidth_tier, db)
    if not aiostreams_url:
        logger.warning(f"AIOStreams URL not configured for tier '{bandwidth_tier}'")
        return []

    # Resolve video ID to IMDb format
    imdb_video_id, tmdb_key = _parse_video_id(video_id)

    if not imdb_video_id and tmdb_key:
        parts = tmdb_key.split(":")
        tmdb_id = int(parts[0])
        media_type = parts[1]
        suffix = parts[2] if len(parts) > 2 and parts[2] else ""

        # Try DB first
        imdb_id = _resolve_imdb_id(tmdb_id, media_type, db)

        # Fallback to TMDB API
        if not imdb_id:
            imdb_id = await _fetch_imdb_from_tmdb(tmdb_id, media_type)

        if not imdb_id:
            logger.warning(f"Could not resolve TMDB ID {tmdb_id} to IMDb ID")
            return []

        imdb_video_id = f"{imdb_id}:{suffix}" if suffix else imdb_id

    if not imdb_video_id:
        return []

    # Call AIOStreams
    stream_url = f"{aiostreams_url}/stream/{stremio_type}/{imdb_video_id}.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(stream_url)
            if resp.status_code == 200:
                data = resp.json()
                streams = data.get("streams", [])

                # Cache the result
                if len(_stream_cache) >= _STREAM_CACHE_MAX:
                    # Evict oldest entries
                    oldest_keys = sorted(
                        _stream_cache, key=lambda k: _stream_cache[k][0]
                    )[:100]
                    for k in oldest_keys:
                        del _stream_cache[k]
                _stream_cache[cache_key] = (now, streams)

                return streams
            else:
                logger.warning(
                    f"AIOStreams returned {resp.status_code} for {stream_url}"
                )
                return []
    except Exception as e:
        logger.error(f"AIOStreams proxy error for {stream_url}: {e}")
        return []
