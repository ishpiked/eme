"""Fast YouTube Music catalog search.

Replaces the old slow ``yt-dlp ytsearch`` + hand built ranking path with the
``ytmusicapi`` library (unauthenticated YouTube Music search): typically
~0.5 to 1s for a full result list with clean music metadata (title, artists,
album, duration, thumbnails, videoId).

Results are returned in YouTube Music's own relevance order with no custom
scoring, no query fallbacks, no per-result metadata lookups. Entry dicts are
shaped to stay compatible with the bot's existing display/download code
(``id``/``title``/``track``/``artist``/``album``/``duration``/``thumbnail``/
``channel``/``uploader``).
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Cache key: casefolded, whitespace-collapsed query text."""
    return _WS_RE.sub(" ", query.strip().casefold())


def parse_views(value: object) -> int:
    """Parse YouTube Music view counts like '1.2B', '345M', '13K', '1,234'."""
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip().replace(",", "").upper()
    multiplier = 1
    if text.endswith("B"):
        multiplier, text = 1_000_000_000, text[:-1]
    elif text.endswith("M"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.endswith("K"):
        multiplier, text = 1_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def parse_duration(value: object, fallback: object = None) -> int:
    """Duration in whole seconds from int seconds or 'M:SS' / 'H:MM:SS' text."""
    for candidate in (value, fallback):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
        if isinstance(candidate, str) and candidate.strip():
            parts = candidate.strip().split(":")
            try:
                nums = [int(p) for p in parts]
            except ValueError:
                continue
            total = 0
            for num in nums:
                total = total * 60 + num
            if total > 0:
                return total
    return 0


def best_thumbnail(thumbnails: object) -> str | None:
    """Pick the largest thumbnail URL from a YouTube Music thumbnail list."""
    if not isinstance(thumbnails, (list, tuple)):
        return None
    best_url: str | None = None
    best_area = -1
    for thumb in thumbnails:
        if not isinstance(thumb, dict):
            continue
        url = thumb.get("url")
        if not url:
            continue
        try:
            area = int(thumb.get("width") or 0) * int(thumb.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        if area >= best_area:
            best_area = area
            best_url = str(url)
    return best_url


def normalize_item(raw: object) -> dict | None:
    """Convert one YouTube Music search hit into a bot-compatible entry.

    Returns None for hits without a playable videoId.
    """
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("videoId") or raw.get("id")
    if not video_id:
        return None
    video_id = str(video_id)

    title = str(raw.get("title") or "").strip()
    artist_names = [
        str(a.get("name", "")).strip()
        for a in (raw.get("artists") or [])
        if isinstance(a, dict) and str(a.get("name", "")).strip()
    ]
    artist = ", ".join(artist_names)
    album = raw.get("album")
    album_name = str(album.get("name", "")).strip() if isinstance(album, dict) else ""
    album_id = str(album.get("id", "")) if isinstance(album, dict) and album.get("id") else None
    duration = parse_duration(raw.get("duration_seconds"), raw.get("duration"))
    thumbnail = best_thumbnail(raw.get("thumbnails"))
    primary_artist = artist_names[0] if artist_names else ""

    return {
        "id": video_id,
        "videoId": video_id,
        "title": title or "Unknown title",
        "track": title or "Unknown title",
        "artist": artist,
        "artists": artist_names,
        "album": album_name or None,
        "album_id": album_id,
        "duration": duration,
        "thumbnail": thumbnail,
        "thumbnails": raw.get("thumbnails"),
        "channel": primary_artist,
        "uploader": primary_artist,
        "view_count": parse_views(raw.get("views")),
        "views": raw.get("views"),
        "year": raw.get("year"),
        "videoType": raw.get("videoType"),
        "resultType": raw.get("resultType"),
        "category": raw.get("category"),
    }


def _filter_search(client, query: str, kind: str, limit: int) -> list:
    """One YouTube Music filtered search that never raises."""
    try:
        return client.search(query, filter=kind, limit=limit) or []
    except Exception as error:
        logger.warning(
            "YouTube Music %s search failed: %s", kind, type(error).__name__
        )
        return []


def search(query: str, limit: int = 20) -> list[dict]:
    """Search YouTube Music and return up to ``limit`` normalized entries.

    Official ``songs`` are preferred; ``videos`` fill the remainder so
    version queries (slowed/remix/live) still find playable uploads.
    Both filters run concurrently so wall time is a single call, not two.
    Order is YouTube Music relevance order.
    """
    if not query or not query.strip():
        return []
    limit = max(1, int(limit))
    started = time.perf_counter()
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        logger.error("ytmusicapi is not installed; cannot search music catalog.")
        return []

    client = YTMusic()
    text = query.strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        songs_future = pool.submit(_filter_search, client, text, "songs", limit)
        videos_future = pool.submit(
            _filter_search, client, text, "videos", max(10, limit // 2)
        )
        songs = songs_future.result()
        videos = videos_future.result()

    entries: list[dict] = []
    seen: set[str] = set()
    for raw in list(songs) + list(videos):
        entry = normalize_item(raw)
        if not entry or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        entries.append(entry)
        if len(entries) >= limit:
            break
    logger.info(
        "CATALOG_SEARCH query=%r results=%d elapsed=%.2fs",
        query[:80], len(entries), time.perf_counter() - started,
    )
    return entries


def search_one(query: str) -> dict | None:
    """Return the top YouTube Music hit for ``query``, if any."""
    results = search(query, limit=10)
    return results[0] if results else None
