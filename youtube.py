"""Resolve and download direct YouTube player streams."""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
PLAYER_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20261009.01.00",
        "hl": "en",
        "gl": "IN",
    }
}
MUSIC_PLAYER_CONTEXT = {
    "client": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20261009.01.00",
        "hl": "en",
        "gl": "IN",
    }
}
ANDROID_PLAYER_CONTEXT = {
    "client": {
        "clientName": "ANDROID",
        "clientVersion": "20.10.38",
        "androidSdkVersion": 30,
        "hl": "en",
        "gl": "IN",
    }
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DOWNLOAD_WORKERS = 16
DOWNLOAD_CHUNK_SIZE = 1 * 1024 * 1024
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_AUDIO_CODEC_RE = re.compile(r"(?:mp4a|opus|vorbis|aac|mp3|alac|flac)", re.I)
SEARCH_TIMEOUT = 12
MAX_SEARCH_RESPONSE_BYTES = 12 * 1024 * 1024


class YouTubeError(RuntimeError):
    """An expected YouTube URL, player, or stream failure."""


class _RangeUnsupported(YouTubeError):
    """The stream server ignored an HTTP Range request."""


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_initial_data(page: str) -> dict:
    markers = ("var ytInitialData = ", "ytInitialData = ")
    marker_position = max(page.find(marker) for marker in markers)
    if marker_position < 0:
        raise YouTubeError(
            "YouTube search data is unavailable; YouTube may require consent."
        )
    marker = next(
        marker for marker in markers
        if page.find(marker) == marker_position
    )
    start = marker_position + len(marker)
    json_start = page.find("{", start)
    if json_start < 0:
        raise YouTubeError("YouTube returned malformed search data.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(json_start, len(page)):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(page[json_start:index + 1])
                except json.JSONDecodeError as error:
                    raise YouTubeError("YouTube returned malformed search data.") from error
                if not isinstance(data, dict):
                    raise YouTubeError("YouTube returned invalid search data.")
                return data
    raise YouTubeError("YouTube returned incomplete search data.")


def _text_from_runs(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(
            str(run.get("text") or "")
            for run in runs if isinstance(run, dict)
        ).strip()
    return str(value.get("simpleText") or "").strip()


def _walk_video_renderers(value: object):
    if isinstance(value, dict):
        renderer = value.get("videoRenderer")
        if isinstance(renderer, dict):
            yield renderer
        for child in value.values():
            yield from _walk_video_renderers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_video_renderers(child)


def _parse_duration(value: str) -> int:
    if not value:
        return 0
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return 0
    duration = 0
    for part in parts:
        duration = duration * 60 + part
    return duration


def search(query: str, limit: int = 10) -> list[dict]:
    """Search YouTube's results page and normalize its video renderers."""
    query = str(query).strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query}
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT) as response:
            if response.status != 200:
                raise YouTubeError(f"YouTube returned HTTP {response.status}.")
            page = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise YouTubeError(f"YouTube returned HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise YouTubeError(f"YouTube search request failed: {error}") from error
    if len(page) > MAX_SEARCH_RESPONSE_BYTES:
        raise YouTubeError("YouTube search response exceeded the size limit.")

    data = _extract_initial_data(page.decode("utf-8", errors="replace"))
    results = []
    seen = set()
    for video in _walk_video_renderers(data):
        video_id = str(video.get("videoId") or "")
        title = _text_from_runs(video.get("title"))
        if (
            not _VIDEO_ID_RE.fullmatch(video_id)
            or not title
            or video_id in seen
        ):
            continue
        seen.add(video_id)
        owner = video.get("ownerText") or video.get("shortBylineText")
        thumbnails = (video.get("thumbnail") or {}).get("thumbnails") or []
        thumbnail = (
            thumbnails[-1].get("url")
            if thumbnails and isinstance(thumbnails[-1], dict) else None
        )
        duration_text = _text_from_runs(video.get("lengthText"))
        channel = _text_from_runs(owner)
        results.append({
            "id": video_id,
            "videoId": video_id,
            "title": html.unescape(title),
            "track": html.unescape(title),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "duration": _parse_duration(duration_text),
            "channel": html.unescape(channel) if channel else None,
            "uploader": html.unescape(channel) if channel else None,
            "artist": html.unescape(channel) if channel else "",
            "views": _text_from_runs(video.get("viewCountText")) or None,
            "thumbnail": thumbnail,
        })
        if len(results) >= limit:
            break
    return results


def get_video_id(value: str) -> str:
    text = str(value).strip()
    if _VIDEO_ID_RE.fullmatch(text):
        return text

    try:
        url = urlparse(text if "://" in text else f"https://{text}")
    except ValueError as error:
        raise YouTubeError("Invalid YouTube URL.") from error
    host = (url.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if url.username is not None or url.password is not None:
        raise YouTubeError("Invalid YouTube URL.")

    video_id = None
    if host == "youtu.be":
        video_id = next((part for part in url.path.split("/") if part), None)
    elif host in {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
    }:
        video_id = parse_qs(url.query).get("v", [None])[0]
        if not video_id:
            match = re.match(r"^/(?:shorts|embed|live)/([^/]+)", url.path)
            video_id = match.group(1) if match else None

    if not video_id or not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeError("Could not extract a YouTube video ID.")
    return video_id


def _has_audio(mime_type: str) -> bool:
    return mime_type.lower().startswith("audio/") or bool(
        _AUDIO_CODEC_RE.search(mime_type)
    )


def _format_entry(raw: dict) -> dict:
    mime_type = str(raw.get("mimeType") or "")
    return {
        "mimeType": mime_type,
        "quality": (
            raw.get("qualityLabel") or raw.get("audioQuality") or "unknown"
        ),
        "bitrate": _integer(raw.get("bitrate")),
        "contentLength": _integer(raw.get("contentLength")),
        "url": raw.get("url"),
        "width": _integer(raw.get("width")),
        "height": _integer(raw.get("height")),
        "hasAudio": _has_audio(mime_type),
        "hasVideo": mime_type.lower().startswith("video/"),
        "audioQuality": raw.get("audioQuality"),
    }


def resolve(value: str) -> dict:
    """Resolve a YouTube video through the public player endpoint."""
    video_id = get_video_id(value)
    host = (urlparse(str(value)).hostname or "").lower().rstrip(".")
    clients = []
    if host == "music.youtube.com":
        clients.append((
            MUSIC_PLAYER_CONTEXT,
            "https://music.youtube.com/youtubei/v1/player",
            "https://music.youtube.com",
            USER_AGENT,
        ))
    clients.extend((
        (PLAYER_CONTEXT, PLAYER_URL, "https://www.youtube.com", USER_AGENT),
        (
            ANDROID_PLAYER_CONTEXT,
            PLAYER_URL,
            "https://www.youtube.com",
            "com.google.android.youtube/20.10.38 (Linux; U; Android 11)",
        ),
    ))

    player = None
    failures = []
    for client_context, endpoint, origin, user_agent in clients:
        body = json.dumps({
            "videoId": video_id,
            "context": client_context,
        }).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "User-Agent": user_agent,
                "Origin": origin,
                "Referer": f"{origin}/",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                candidate = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as error:
            raise YouTubeError(f"YouTube player request failed: {error}") from error

        if not isinstance(candidate, dict):
            failures.append("YouTube returned an invalid player response.")
            continue
        status = candidate.get("playabilityStatus") or {}
        if status.get("status") != "OK":
            reason = status.get("reason")
            messages = status.get("messages")
            if not reason and isinstance(messages, list) and messages:
                reason = messages[0]
            failures.append(str(reason or status.get("status") or "Video unavailable."))
            continue
        player = candidate
        break

    if player is None:
        reason = next(
            (failure for failure in failures if failure != "Video unavailable."),
            failures[-1] if failures else "Video unavailable.",
        )
        raise YouTubeError(reason)

    details = player.get("videoDetails") or {}
    if not isinstance(details, dict):
        raise YouTubeError("YouTube returned invalid video details.")
    streaming = player.get("streamingData") or {}
    formats = [
        _format_entry(item)
        for item in (
            list(streaming.get("formats") or [])
            + list(streaming.get("adaptiveFormats") or [])
        )
        if isinstance(item, dict) and item.get("url")
    ]
    formats.sort(key=lambda item: item["bitrate"], reverse=True)
    if not formats:
        raise YouTubeError(
            "YouTube did not return direct stream URLs for this video."
        )

    thumbnail_data = details.get("thumbnail") or {}
    thumbnails = (
        thumbnail_data.get("thumbnails") or []
        if isinstance(thumbnail_data, dict) else []
    )
    thumbnail = max(
        (item for item in thumbnails if isinstance(item, dict)),
        key=lambda item: _integer(item.get("width")) * _integer(item.get("height")),
        default={},
    )
    return {
        "id": video_id,
        "title": details.get("title") or "Unknown",
        "author": details.get("author") or "",
        "duration": _integer(details.get("lengthSeconds")),
        "thumbnail": thumbnail.get("url"),
        "formats": formats,
    }


def select_audio_format(formats: list[dict]) -> dict:
    candidates = [
        item for item in formats
        if item.get("url") and item.get("hasAudio")
    ]
    candidates.sort(
        key=lambda item: (
            not item.get("hasVideo"),
            item.get("bitrate") or 0,
        ),
        reverse=True,
    )
    if not candidates:
        raise YouTubeError("YouTube did not return a direct audio stream.")
    return candidates[0]


def select_inline_audio_format(formats: list[dict]) -> dict:
    candidates = [
        item for item in formats
        if item.get("url")
        and item.get("hasAudio")
        and not item.get("hasVideo")
        and "audio/mp4" in str(item.get("mimeType") or "").lower()
    ]
    candidates.sort(
        key=lambda item: item.get("bitrate") or 0,
        reverse=True,
    )
    if not candidates:
        raise YouTubeError(
            "YouTube did not return a direct MP4 audio stream for inline playback."
        )
    return candidates[0]


def select_inline_video_format(
    formats: list[dict], max_bytes: int = 50 * 1024 * 1024
) -> dict:
    candidates = [
        item for item in formats
        if item.get("url")
        and item.get("hasAudio")
        and item.get("hasVideo")
        and "video/mp4" in str(item.get("mimeType") or "").lower()
        and 0 < _integer(item.get("height")) <= 720
        and (
            not item.get("contentLength")
            or _integer(item["contentLength"]) <= max_bytes
        )
    ]
    candidates.sort(
        key=lambda item: (
            _integer(item.get("height")),
            _integer(item.get("bitrate")),
        ),
        reverse=True,
    )
    if not candidates:
        raise YouTubeError(
            "YouTube did not return a direct MP4 video-and-audio stream "
            "at 720p or lower within the inline size limit."
        )
    return candidates[0]


def format_extension(stream: dict) -> str:
    mime_type = str(stream.get("mimeType") or "").lower()
    if "mp4" in mime_type or "m4a" in mime_type:
        return ".m4a"
    if "webm" in mime_type:
        return ".webm"
    if "ogg" in mime_type:
        return ".ogg"
    if "mpeg" in mime_type or "mp3" in mime_type:
        return ".mp3"
    return ".audio"


def select_preview_format(formats: list[dict], max_bytes: int) -> dict:
    candidates = [
        item for item in formats
        if item.get("url")
        and item.get("hasAudio")
        and item.get("hasVideo")
        and "mp4" in str(item.get("mimeType") or "").lower()
        and 0 < _integer(item.get("height")) <= 720
        and (
            not item.get("contentLength")
            or _integer(item["contentLength"]) <= max_bytes
        )
    ]
    candidates.sort(
        key=lambda item: (
            _integer(item.get("height")),
            _integer(item.get("bitrate")),
        ),
        reverse=True,
    )
    if not candidates:
        raise YouTubeError(
            "YouTube did not return a direct MP4 video-and-audio stream "
            "at 720p or lower within the preview size limit."
        )
    return candidates[0]


def _request_headers(start: int | None = None, end: int | None = None) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if start is not None and end is not None:
        headers["Range"] = f"bytes={start}-{end}"
    return headers


def _read_single_stream(url: str, destination: Path, max_bytes: int | None) -> int:
    request = urllib.request.Request(url, headers=_request_headers())
    bytes_written = 0
    with urllib.request.urlopen(request, timeout=30) as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if max_bytes and bytes_written > max_bytes:
                    raise YouTubeError(
                        f"Stream exceeds the {max_bytes // (1024 * 1024)} MB limit."
                    )
                output.write(chunk)
    return bytes_written


def _download_range(url: str, start: int, end: int, total: int) -> bytes:
    request = urllib.request.Request(
        url, headers=_request_headers(start, end)
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 206:
            raise _RangeUnsupported("The stream server does not support byte ranges.")
        expected_range = f"bytes {start}-{end}/{total}"
        if response.headers.get("Content-Range") != expected_range:
            raise YouTubeError("The stream server returned an invalid byte range.")
        data = response.read(end - start + 1)
    if len(data) != end - start + 1:
        raise YouTubeError("The stream server returned an incomplete byte range.")
    return data


def _probe_content_length(url: str) -> int:
    """Get a stream length with a one-byte range request when available."""
    request = urllib.request.Request(
        url, headers=_request_headers(0, 0)
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                return _integer(content_range.rsplit("/", 1)[1])
            # A server may ignore Range but still report the full object size.
            if response.status == 200:
                return _integer(response.headers.get("Content-Length"))
    except Exception as error:
        logger.debug("Could not probe stream length: %s", type(error).__name__)
    return 0


def download_format(
    stream: dict,
    destination: Path,
    max_bytes: int | None = None,
) -> int:
    """Download a direct format URL, using bounded parallel HTTP ranges."""
    url = str(stream.get("url") or "")
    if not url:
        raise YouTubeError("The selected stream has no direct URL.")
    try:
        total = int(stream.get("contentLength") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        total = _probe_content_length(url)
    if max_bytes and total > max_bytes:
        raise YouTubeError(
            f"Stream exceeds the {max_bytes // (1024 * 1024)} MB limit."
        )

    started = time.perf_counter()
    if total < DOWNLOAD_CHUNK_SIZE * 2:
        size = _read_single_stream(url, destination, max_bytes)
    else:
        ranges = [
            (start, min(start + DOWNLOAD_CHUNK_SIZE - 1, total - 1))
            for start in range(0, total, DOWNLOAD_CHUNK_SIZE)
        ]
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(DOWNLOAD_WORKERS, len(ranges))
            ) as executor:
                futures = {
                    executor.submit(_download_range, url, start, end, total):
                        (start, end)
                    for start, end in ranges
                }
                chunks = {}
                for future in concurrent.futures.as_completed(futures):
                    start, _ = futures[future]
                    chunks[start] = future.result()
            with destination.open("wb") as output:
                output.truncate(total)
                for start in sorted(chunks):
                    output.seek(start)
                    output.write(chunks[start])
            size = total
        except _RangeUnsupported:
            logger.info("YouTube stream has no range support; using one GET.")
            size = _read_single_stream(url, destination, max_bytes)

    elapsed = max(time.perf_counter() - started, 0.001)
    logger.info(
        "STREAM_DOWNLOAD bytes=%d workers=%d elapsed=%.2fs rate=%.2fMiB/s",
        size,
        min(DOWNLOAD_WORKERS, max(1, (total + DOWNLOAD_CHUNK_SIZE - 1) // DOWNLOAD_CHUNK_SIZE)),
        elapsed,
        size / (1024 * 1024) / elapsed,
    )
    return size
