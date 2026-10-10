"""LRCLIB lookup client for song lyrics."""

import asyncio
import logging

import aiohttp


logger = logging.getLogger(__name__)
LRCLIB_API = "https://lrclib.net/api"
MAX_LYRICS_CHARS = 18000


def create_http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12, connect=4, sock_read=8),
        connector=aiohttp.TCPConnector(limit=8, keepalive_timeout=30),
    )


async def close_http_session(session: aiohttp.ClientSession | None) -> None:
    if session and not session.closed:
        await session.close()


async def _request_json(session: aiohttp.ClientSession, url: str, params: dict):
    try:
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": "EmyTelegramMusicBot/1.0 (lyrics lookup)"},
        ) as response:
            if response.status == 404:
                return None
            if response.status == 429:
                raise RuntimeError("LRCLIB is rate limiting requests.")
            if response.status != 200:
                raise RuntimeError(f"LRCLIB returned HTTP {response.status}.")
            return await response.json(content_type=None)
    except asyncio.TimeoutError as error:
        raise RuntimeError("LRCLIB lyrics lookup timed out.") from error
    except aiohttp.ClientError as error:
        raise RuntimeError("Could not connect to LRCLIB.") from error


async def find_lyrics(
    session: aiohttp.ClientSession,
    title: str,
    artist: str = "",
    album: str | None = None,
    duration: int | None = None,
) -> dict | None:
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(duration)

    result = None
    try:
        result = await _request_json(session, f"{LRCLIB_API}/get", params)
    except RuntimeError as error:
        logger.warning(
            "LRCLIB exact lookup failed (%s); falling back to text search.",
            error,
        )
    if not _usable(result):
        results = await _request_json(
            session,
            f"{LRCLIB_API}/search",
            {"q": title},
        )
        if isinstance(results, list):
            result = next(
                (
                    item for item in results
                    if _usable(item)
                    and _same_recording(title, artist, item)
                ),
                None,
            )
    if not _usable(result):
        return None
    lyrics = (result.get("plainLyrics") or result.get("syncedLyrics") or "").strip()
    if not lyrics:
        return None
    return {
        "title": result.get("trackName") or title,
        "artist": result.get("artistName") or artist,
        "lyrics": lyrics[:MAX_LYRICS_CHARS],
    }


def _usable(result) -> bool:
    return (
        isinstance(result, dict)
        and isinstance(result.get("plainLyrics") or result.get("syncedLyrics"), str)
        and bool((result.get("plainLyrics") or result.get("syncedLyrics")).strip())
    )


def _same_recording(title: str, artist: str, candidate: dict) -> bool:
    def words(value: str) -> set[str]:
        return set(value.casefold().split())

    expected_title = words(title)
    actual_title = words(str(candidate.get("trackName") or ""))
    expected_artist = words(artist)
    actual_artist = words(str(candidate.get("artistName") or ""))
    artist_matches = (
        not expected_artist
        or bool(expected_artist & actual_artist)
    )
    return (
        bool(expected_title)
        and expected_title.issubset(actual_title)
        and artist_matches
    )


def split_lyrics(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
