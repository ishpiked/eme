import sys
import warnings

sys.dont_write_bytecode = True  # never emit __pycache__ / *.pyc
warnings.filterwarnings("ignore", message=r".*match a supported version.*")

import asyncio
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InputFile,
    InputMediaAudio,
    InlineQueryResultAudio,
    InlineQueryResultVideo,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import emyfx
import recognition
import youtube
import lrclib
from transport import configure_local_api, local_api_urls
from features import (
    CUSTOM_EMOJI_IDS,
    CUSTOM_EMOJI_PACK_NAMES,
    call_api,
    custom_button,
    ephemeral_message_target,
    inline_keyboard,
    resolve_custom_emoji_packs,
    send_ephemeral_message,
)

load_dotenv()
import owner

OWNER_CONSOLE = owner.OwnerConsole()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_DESCRIPTION = os.getenv(
    "BOT_DESCRIPTION",
    "Emy is your personal music companion on Telegram. Search for your favorite songs, "
    "discover tracks, and enjoy music with a simple, seamless experience, all from your chat.",
)
BOT_ABOUT = os.getenv(
    "BOT_ABOUT",
    "Emy is your go to music bot on Telegram. Search your favorite songs and play them instantly.",
)
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))  # 10 min default
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "49"))  # Telegram bots cap ~50MB
MP3_QUALITY = os.getenv("MP3_QUALITY", "320")  # user demands max quality: 320k every song
TELEGRAM_AUDIO_CACHE_VERSION = "v2"
TELEGRAM_LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip()
BASE_DIR = Path(__file__).resolve().parent
BOT_PROFILE_PHOTO = os.getenv("BOT_PROFILE_PHOTO", "").strip()
BOT_PROFILE_PHOTO_URL = os.getenv(
    "BOT_PROFILE_PHOTO_URL",
    "https://imagehost-pearl.vercel.app/i/0e2ddf9f30ba.png",
).strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MAX_INLINE_QUERY_LENGTH = 256
INLINE_QUERY_TIMEOUT = 3.0


def derive_artist(entry: dict) -> str:
    """Proper artist without hardcoding: Topic channel -> artist name,
    else official label/uploader channel."""
    if entry.get("artist"):
        return str(entry["artist"])
    channel = entry.get("channel") or entry.get("uploader") or "Unknown artist"
    if channel.lower().endswith(" - topic"):
        channel = channel[: -len(" - topic")].strip()
    return channel


def derive_title(entry: dict) -> str:
    if entry.get("track"):
        return str(entry["track"])
    return entry.get("title") or "Unknown title"


# --- Professional title/artist curation (generic formatting, no hardcoding) ---
# Turns dull raw metadata like "Lambo Drive - Pyar Ki Si [Lyrics]" or
# "Nanku - Nanku" into clean player display: "Lambo Drive (Pyar Ki Si)".
JUNK_BRACKET_WORDS = (
    "lyric", "official video", "official audio", "official music video",
    "music video", "audio", "video", "4k", "8k", "hd", "hq", "mv", "m/v",
    "full song", "full video", "video song", "visualizer", "visualiser",
    "song", "track",
)
_BRACKET_RE = re.compile(r"[\(\[]([^()\[\]]{1,40})[\)\]]")


def _strip_junk_brackets(text: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip().lower()
        if any(w in inner for w in JUNK_BRACKET_WORDS):
            return ""
        return m.group(0)  # keep meaningful brackets like (Film Version)

    prev = None
    while prev != text:
        prev = text
        text = _BRACKET_RE.sub(repl, text)
    return text


def _split_camel(text: str) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _smart_case(text: str) -> str:
    # Only touch ALL-CAPS shouting; preserve original stylization otherwise.
    if text.isupper():
        return " ".join(w.capitalize() for w in text.split())
    return text


def clean_channel_name(channel: str) -> str:
    channel = channel.strip()
    if channel.lower().endswith("vevo"):
        channel = channel[: -len("vevo")].strip(" -_")
    return _smart_case(_split_camel(channel))


def curate_track(raw_title: str, raw_artist: str) -> tuple:
    """Returns (display_title, display_performer_or_None)."""
    title = _strip_junk_brackets(raw_title.replace("–", "-").replace("—", "-"))
    title = re.sub(r"\s+", " ", title).strip(" -|•·")

    # Split "Song - Note | Movie | ..." promo structure.
    head, *tail = [p.strip(" -") for p in title.split("|")]
    head = head.strip()
    parts = [p.strip() for p in head.split(" - ") if p.strip()]
    song = parts[0] if parts else head
    note = " · ".join(parts[1:]) if len(parts) > 1 else ""
    if note.lower() == song.lower():
        note = ""  # "Nanku - Nanku" -> just "Nanku"

    display = _smart_case(song)
    if note:
        display += f" ({_smart_case(note)})"
    movie = tail[0].strip() if tail and tail[0].strip() else ""
    if movie and movie.lower() not in display.lower() and len(movie) <= 40:
        display += f" \u2022 {_smart_case(movie)}"

    performer = clean_channel_name(raw_artist) if raw_artist else ""
    if not performer or performer.lower() in ("unknown artist",):
        performer = ""
    if performer and (performer.lower() == display.lower() or performer.lower() == song.lower()):
        performer = ""  # "Nanku / Nanku" -> show title only once
    return display, (performer or None)


def resolve_official(query: str) -> dict | None:
    """Return the top YouTube search hit for ``query``."""
    results = youtube.search(query, 1)
    best = results[0] if results else None
    if best:
        logger.info(
            "PICK '%s' -> %s | %s",
            query, best.get("id"), (best.get("title") or "")[:60],
        )
    return best


SEARCH_RESULT_CACHE: dict[str, tuple[float, int, list[dict]]] = {}
SEARCH_SELECTIONS: dict[str, dict] = {}
SEARCH_RESULT_TTL = 300
SEARCH_SELECTION_TTL = 600


def _normalize_search_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().casefold())


async def resolve_candidates_async(
    query: str, application, limit: int = 6
) -> list[dict]:
    """Search YouTube's results page with an in-memory five-minute cache.

    ``application`` is kept for call compatibility; results are cached in
    memory for five minutes.
    """
    cache_key = _normalize_search_query(query)
    now = time.time()
    fetch = min(25, max(limit, 10))
    cached = SEARCH_RESULT_CACHE.get(cache_key)
    if cached and cached[0] > now and cached[1] >= fetch:
        logger.info(
            "CATALOG_CACHE_HIT query=%r results=%d",
            query[:80], min(len(cached[2]), limit),
        )
        return [dict(entry) for entry in cached[2][:limit]]
    SEARCH_RESULT_CACHE.pop(cache_key, None)

    search_started = time.perf_counter()
    try:
        entries = await asyncio.to_thread(
            youtube.search, query, fetch
        )
    except Exception as error:
        logger.error(
            "YouTube catalog search failed for %r: %s",
            query[:100], error,
        )
        raise
    logger.info(
        "CATALOG_SEARCH provider=YouTube results=%d elapsed=%.2fs",
        len(entries), time.perf_counter() - search_started,
    )
    SEARCH_RESULT_CACHE[cache_key] = (
        time.time() + SEARCH_RESULT_TTL,
        fetch,
        [dict(entry) for entry in entries],
    )
    for old_key, (expires, _, _) in list(SEARCH_RESULT_CACHE.items()):
        if expires <= now:
            SEARCH_RESULT_CACHE.pop(old_key, None)
    while len(SEARCH_RESULT_CACHE) > 256:
        SEARCH_RESULT_CACHE.pop(next(iter(SEARCH_RESULT_CACHE)))
    return entries[:limit]


async def resolve_official_async(query: str, application) -> dict | None:
    ranked = await resolve_candidates_async(query, application, limit=1)
    return ranked[0] if ranked else None


async def resolve_recognition_candidate_async(
    recognized_song: dict, application
) -> dict | None:
    title = str(recognized_song.get("title") or "").strip()
    artist = str(recognized_song.get("artist") or "").strip()
    if not title:
        return None

    queries = [f"{title} {artist}".strip()]
    candidates = await resolve_candidates_async(
        queries[0], application, limit=10
    )

    def rank(entries: list[dict]) -> list[tuple[float, float, float, dict]]:
        ranked = []
        for candidate in entries:
            candidate_title, _ = curate_track(
                derive_title(candidate), derive_artist(candidate)
            )
            title_score = recognition.title_match_ratio(title, candidate_title)
            artist_score = (
                recognition.title_match_ratio(artist, derive_artist(candidate))
                if artist else 0.0
            )
            combined = title_score * 0.85 + artist_score * 0.15
            ranked.append((combined, title_score, artist_score, candidate))
        return sorted(ranked, key=lambda item: item[:3], reverse=True)

    ranked = rank(candidates)
    if ranked and (
        ranked[0][1] >= 0.5
        or (ranked[0][1] >= 0.35 and ranked[0][2] >= 0.7)
    ):
        return ranked[0][3]

    title_candidates = await resolve_candidates_async(
        title, application, limit=10
    )
    known_ids = {
        str(candidate.get("id"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("id")
    }
    ranked.extend(rank([
        candidate for candidate in title_candidates
        if not candidate.get("id") or str(candidate["id"]) not in known_ids
    ]))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    if not ranked:
        return None
    best = ranked[0]
    if best[1] < 0.5 and not (best[1] >= 0.35 and best[2] >= 0.7):
        return None
    logger.info(
        "RECOGNITION_CATALOG_PICK title=%r video_id=%s title_score=%.2f "
        "artist_score=%.2f",
        title[:80], best[3].get("id"), best[1], best[2],
    )
    return best[3]


def _inline_thumbnail(value: object) -> str | None:
    if (
        isinstance(value, str)
        and value.startswith("https://")
        and len(value) <= 2048
    ):
        return value
    return None


def _resolve_inline_audio(candidate: dict) -> InlineQueryResultAudio:
    video_id = youtube.get_video_id(str(candidate.get("id") or ""))
    player = youtube.resolve(str(candidate.get("url") or video_id))
    audio_format = youtube.select_inline_audio_format(player["formats"])
    title, artist = curate_track(
        str(player.get("title") or candidate.get("title") or "Song"),
        str(player.get("author") or candidate.get("artist") or candidate.get("channel") or ""),
    )
    return InlineQueryResultAudio(
        id=video_id,
        audio_url=audio_format["url"],
        title=title[:64] or "Song",
        performer=artist[:64] if artist else None,
        audio_duration=player.get("duration") or None,
        thumb_url=_inline_thumbnail(
            player.get("thumbnail") or candidate.get("thumbnail")
        ),
    )


def _resolve_inline_video(candidate: dict) -> InlineQueryResultVideo:
    video_id = youtube.get_video_id(str(candidate.get("id") or ""))
    player = youtube.resolve(str(candidate.get("url") or video_id))
    video_format = youtube.select_inline_video_format(player["formats"])
    title, artist = curate_track(
        str(player.get("title") or candidate.get("title") or "Video"),
        str(player.get("author") or candidate.get("artist") or candidate.get("channel") or ""),
    )
    thumbnail = _inline_thumbnail(
        player.get("thumbnail") or candidate.get("thumbnail")
    ) or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return InlineQueryResultVideo(
        id=video_id,
        video_url=video_format["url"],
        mime_type="video/mp4",
        thumb_url=thumbnail,
        title=title[:128] or "Video",
        description=artist[:255] if artist else None,
        video_width=video_format.get("width") or None,
        video_height=video_format.get("height") or None,
        video_duration=player.get("duration") or None,
    )


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if not inline_query:
        return
    query = (inline_query.query or "").strip()
    if len(query) > MAX_INLINE_QUERY_LENGTH:
        await inline_query.answer([], cache_time=0, is_personal=True)
        return

    deadline = time.monotonic() + INLINE_QUERY_TIMEOUT
    media_kind = "audio"
    search_query = query
    video_search = re.match(r"^(?:/video|video:)\s+(.+)$", query, re.IGNORECASE)
    if video_search:
        media_kind = "video"
        search_query = video_search.group(1).strip()

    try:
        if not query:
            candidates = []
        elif URL_RE.match(search_query):
            if not recognition.validate_media_url(search_query):
                candidates = []
            else:
                video_id = youtube.get_video_id(search_query)
                media_kind = (
                    "audio"
                    if recognition.is_music_url(search_query)
                    else "video"
                )
                candidates = [{
                    "id": video_id,
                    "title": "YouTube video",
                    "url": search_query,
                }]
        elif re.fullmatch(r"[A-Za-z0-9_-]{11}", search_query):
            media_kind = "video"
            candidates = [{
                "id": search_query,
                "url": f"https://www.youtube.com/watch?v={search_query}",
            }]
        else:
            try:
                candidates = await asyncio.wait_for(
                    resolve_candidates_async(
                        search_query, context.application, limit=10
                    ),
                    timeout=max(0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                logger.warning("Inline catalog search timed out for %r", query[:80])
                candidates = []

        valid_candidates = []
        seen_ids = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                video_id = youtube.get_video_id(str(candidate.get("id") or ""))
            except youtube.YouTubeError:
                continue
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            valid_candidates.append({
                **candidate,
                "id": video_id,
                "url": (
                    f"https://music.youtube.com/watch?v={video_id}"
                    if recognition.is_music_url(str(candidate.get("url") or ""))
                    else f"https://www.youtube.com/watch?v={video_id}"
                ),
            })

        resolver = (
            _resolve_inline_video
            if media_kind == "video"
            else _resolve_inline_audio
        )

        async def resolve_candidate(candidate: dict):
            try:
                return await asyncio.to_thread(resolver, candidate)
            except Exception as error:
                logger.info(
                    "Skipping unresolved inline %s result %s: %s",
                    media_kind,
                    candidate.get("id"),
                    error,
                )
                return None

        tasks = [
            asyncio.create_task(resolve_candidate(candidate))
            for candidate in valid_candidates[:10]
        ]
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0, deadline - time.monotonic()),
        )
        results = [
            task.result()
            for task in tasks
            if task in done and not task.cancelled()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning(
                "Inline stream resolution timed out for %d result(s)",
                len(pending),
            )
        results = [result for result in results if result is not None]
    except (recognition.RecognitionError, youtube.YouTubeError, ValueError) as error:
        logger.info("Inline query could not be resolved: %s", error)
        results = []
    except Exception:
        logger.exception("Unexpected inline query failure for %r", query[:80])
        results = []

    await inline_query.answer(
        results,
        cache_time=0,
        is_personal=True,
    )


class TypeTubeFreeQuotaExhausted(RuntimeError):
    """Raised when public TypeTube quota is spent and no owner key is saved."""


class TypeTubeOwnerKeyUnavailable(RuntimeError):
    """Raised when the owner's private TypeTube key cannot serve requests."""


class TypeTubeSDKVersionError(RuntimeError):
    """Raised when an old TypeTube package cannot speak to TAP clusters."""


def friendly_error(e: BaseException) -> str:
    msg = ANSI_RE.sub("", str(e)).strip()
    low = msg.lower()
    if isinstance(e, TypeTubeSDKVersionError):
        return msg
    if isinstance(e, (TypeTubeFreeQuotaExhausted, TypeTubeOwnerKeyUnavailable)):
        return "Song downloads are temporarily unavailable. Please try again later."
    if isinstance(e, youtube.YouTubeError):
        return msg[:400]
    if isinstance(e, urllib.error.HTTPError) and e.code == 403:
        return "The audio source temporarily refused the download. Please try again."
    if type(e).__module__.startswith("typetube") or any(
        marker in low
        for marker in (
            "api key", "unauthorized", "quota", "daily limit", "rate limit", "typetube"
        )
    ):
        return "Song downloads are temporarily unavailable. Please try again later."
    if "timed out" in low or "writeTimeout" in str(type(e).__name__):
        return (
            "Upload to Telegram timed out (slow connection). "
            "Retried 3x with 180s timeout. Please try /song again, "
            "or set MP3_QUALITY=96 in .env for smaller files."
        )
    if (
        "sign in to confirm" in low
        or "confirm you’re not a bot" in low
        or "confirm you're not a bot" in low
    ):
        return (
            "YouTube requires sign-in for this upload, so Emy can't access it. "
            "Choose another result; this does not mean the song itself is unavailable."
        )
    return msg[:400]


# --- Album art: YouTube mqdefault is guaranteed JPEG 320x180 (~15KB),
# which already satisfies Telegram's thumbnail spec (<200KB, <=320px).
# Fetched straight into RAM; never stored.
def fetch_cover_jpeg(video_id: str, timeout: int = 2) -> bytes | None:
    url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read(200 * 1024)
        if len(data) < 1024 or not data.startswith(b"\xff\xd8"):
            return None  # not a real JPEG (e.g. 404 placeholder)
        return data
    except Exception as e:
        logger.warning("Cover fetch failed for %s: %s", video_id, str(e)[:120])
        return None


def embed_cover_mp3(mp3_path: Path, cover_bytes: bytes) -> bool:
    """Attach cover as ID3 APIC via stream-copy (fast, no re-encode).
    Non-fatal: returns False on any failure, original MP3 untouched."""
    try:
        cover_path = mp3_path.with_name(mp3_path.stem + "_cover.jpg")
        out_path = mp3_path.with_name(mp3_path.stem + "_art.mp3")
        cover_path.write_bytes(cover_bytes)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-i", str(mp3_path), "-i", str(cover_path),
             "-map", "0:a", "-map", "1:v",
             "-c", "copy", "-id3v2_version", "3",
             "-metadata:s:v", 'title=Album cover',
             "-metadata:s:v", "comment=Cover (front)",
             str(out_path)],
            capture_output=True, timeout=30,
        )
        cover_path.unlink(missing_ok=True)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1024:
            out_path.unlink(missing_ok=True)
            return False
        out_path.replace(mp3_path)
        return True
    except Exception as e:
        logger.warning("Cover embed failed: %s", str(e)[:150])
        return False


def _is_typetube_public_limit(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    message = str(error).lower()
    if "region" in message:
        return False
    return status_code in (401, 403, 429) or any(
        marker in message
        for marker in (
            "quota",
            "daily limit",
            "rate limit",
            "limit exceeded",
            "public api key",
            "no public api key",
            "error 401",
            "error 403",
            "error 429",
        )
    )


def _close_typetube_client(client) -> None:
    close = getattr(client, "close", None)
    if close:
        try:
            close()
        except Exception as error:
            logger.debug("TypeTube client close failed: %s", type(error).__name__)


def _create_typetube_client(create_client, *, host: str, api_key: str | None = None):
    kwargs = {"host": host, "port": 8443, "timeout_ms": 15000}
    if api_key:
        kwargs["api_key"] = api_key
    try:
        return create_client(**kwargs)
    except TypeError as error:
        if "unexpected keyword argument" in str(error):
            raise TypeTubeSDKVersionError(
                "This bot requires the TypeTube 1.1.3 TAP client. Run "
                "`python -m pip install -r requirements.txt` with the same "
                "Python environment used to start the bot, then restart it."
            ) from error
        raise


class _InvalidRangeResponse(Exception):
    """Raised when a media server ignores or misreports an HTTP byte range."""


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", (value or "").strip(), re.I)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _download_audio_url(url: str, destination: Path) -> int:
    """Download all bytes, falling back to one stream if range support is bad."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Encoding": "identity",
    }

    def open_request(range_header: str | None = None):
        request_headers = dict(headers)
        if range_header:
            request_headers["Range"] = range_header
        request = urllib.request.Request(url, headers=request_headers)
        return urllib.request.urlopen(request, timeout=60)

    def download_linear(expected_size: int | None = None) -> int:
        downloaded = 0
        with open_request() as response, destination.open("wb") as output:
            if response.getcode() != 200:
                raise _InvalidRangeResponse(
                    f"Expected a full response, received HTTP {response.getcode()}"
                )
            response_size = response.headers.get("Content-Length")
            if expected_size is None and response_size and response_size.isdigit():
                expected_size = int(response_size)
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
        if downloaded <= 0 or (expected_size and downloaded != expected_size):
            destination.unlink(missing_ok=True)
            raise IOError(
                f"Incomplete audio response ({downloaded} bytes; expected {expected_size})."
            )
        return downloaded

    total_size = None
    try:
        with open_request("bytes=0-0") as response:
            if response.getcode() == 206:
                content_range = _parse_content_range(
                    response.headers.get("Content-Range")
                )
                if content_range and content_range[0] == 0 and content_range[1] == 0:
                    total_size = content_range[2]
                    if len(response.read(1)) != 1 or total_size <= 0:
                        total_size = None
            # HTTP 200 means ranges are ignored; close this response and stream once.
    except urllib.error.HTTPError:
        raise
    except (OSError, ValueError):
        total_size = None

    if not total_size or total_size <= 0:
        return download_linear()

    chunk_size = 4 * 1024 * 1024
    chunks = [
        (start, min(start + chunk_size - 1, total_size - 1))
        for start in range(0, total_size, chunk_size)
    ]
    write_lock = threading.Lock()

    try:
        with destination.open("w+b") as output:
            output.truncate(total_size)

            def fetch_range(bounds: tuple[int, int]) -> int:
                start, end = bounds
                with open_request(f"bytes={start}-{end}") as response:
                    if response.getcode() != 206:
                        raise _InvalidRangeResponse(
                            f"Expected HTTP 206 for bytes {start}-{end}, "
                            f"received HTTP {response.getcode()}"
                        )
                    if _parse_content_range(
                        response.headers.get("Content-Range")
                    ) != (start, end, total_size):
                        raise _InvalidRangeResponse(
                            f"Server returned an invalid range for bytes {start}-{end}."
                        )
                    data = response.read()
                expected_length = end - start + 1
                if len(data) != expected_length:
                    raise _InvalidRangeResponse(
                        f"Received {len(data)} bytes for bytes {start}-{end}."
                    )
                with write_lock:
                    output.seek(start)
                    output.write(data)
                return len(data)

            with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
                written = sum(pool.map(fetch_range, chunks))
            output.flush()

        actual_size = destination.stat().st_size
        if written != total_size or actual_size != total_size:
            raise _InvalidRangeResponse(
                f"Downloaded {written} bytes into a {actual_size}-byte file; "
                f"expected {total_size}."
            )
        return actual_size
    except _InvalidRangeResponse:
        destination.unlink(missing_ok=True)
        logger.warning("TypeTube CDN returned invalid byte ranges; retrying as a stream.")
        return download_linear(expected_size=total_size)


def _probe_media_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        return float(result.stdout.decode("ascii", errors="replace").strip())
    except (TypeError, ValueError):
        return None


def _resolve_typetube_track(target: str, cluster_host: str):
    from typetube import TypeTubeError, create_client

    public_client = _create_typetube_client(create_client, host=cluster_host)
    try:
        return public_client, public_client.resolve(target)
    except TypeTubeError as error:
        if not _is_typetube_public_limit(error):
            _close_typetube_client(public_client)
            raise
    except Exception:
        _close_typetube_client(public_client)
        raise

    try:
        private_key = owner.get_typetube_api_key()
    except OSError as error:
        logger.exception("Could not read the owner-managed TypeTube key.")
        _close_typetube_client(public_client)
        raise TypeTubeOwnerKeyUnavailable from error
    if not private_key:
        _close_typetube_client(public_client)
        raise TypeTubeFreeQuotaExhausted

    _close_typetube_client(public_client)
    logger.info("TypeTube public access is unavailable; trying owner key.")
    private_client = _create_typetube_client(
        create_client, api_key=private_key, host=cluster_host
    )
    try:
        return private_client, private_client.resolve(target)
    except TypeTubeError as error:
        if _is_typetube_public_limit(error):
            logger.error("TypeTube owner-key resolve failed: %s", error)
            _close_typetube_client(private_client)
            raise TypeTubeOwnerKeyUnavailable from error
        _close_typetube_client(private_client)
        raise
    except Exception:
        _close_typetube_client(private_client)
        raise


def _download_typetube_source(
    client, track, target: str, source_path: Path, cluster_host: str
):
    """Download through an allowed regional cluster; rotate regions on 403."""
    cluster_hosts = [host for _, host in owner.TYPE_TUBE_CLUSTERS]
    try:
        current_index = cluster_hosts.index(cluster_host)
    except ValueError:
        current_index = 0

    while True:
        try:
            started = time.perf_counter()
            bytes_written = _download_audio_url(track.best_audio.url, source_path)
            elapsed = time.perf_counter() - started
            average_speed = bytes_written / (1024 * 1024) / max(elapsed, 0.01)
            return (
                client,
                track,
                bytes_written,
                average_speed,
            )
        except Exception as error:
            status_code = getattr(
                error, "status_code", getattr(error, "code", None)
            )
            if status_code != 403:
                _close_typetube_client(client)
                raise
            _close_typetube_client(client)
            source_path.unlink(missing_ok=True)
            next_region_index = current_index + 1
            while next_region_index < len(cluster_hosts):
                next_label, next_host = owner.TYPE_TUBE_CLUSTERS[next_region_index]
                logger.warning(
                    "TypeTube returned a regional 403; retrying through %s.",
                    next_label,
                )
                try:
                    refreshed_client, refreshed_track = _resolve_typetube_track(
                        target, next_host
                    )
                except TypeTubeOwnerKeyUnavailable:
                    raise
                except TypeTubeSDKVersionError:
                    raise
                except Exception as resolve_error:
                    logger.warning(
                        "TypeTube resolve failed in %s (%s); trying the next region.",
                        next_label, type(resolve_error).__name__,
                    )
                    next_region_index += 1
                    continue
                if (
                    not getattr(refreshed_track, "success", False)
                    or not getattr(refreshed_track, "best_audio", None)
                    or not refreshed_track.best_audio.url
                ):
                    _close_typetube_client(refreshed_client)
                    raise RuntimeError(
                        f"TypeTube could not provide an audio stream from {next_label}."
                    ) from error
                client, track = refreshed_client, refreshed_track
                cluster_host = next_host
                current_index = next_region_index
                break
            else:
                raise
    raise RuntimeError("TypeTube audio download failed after refreshing the stream.")


def download_song_to_ram(
    query: str, metadata_callback=None, picked_override: dict | None = None
) -> dict:
    """Resolve and download YouTube audio through TypeTube, then encode MP3."""
    query = str(query or "").strip()
    picked = picked_override
    if URL_RE.match(query):
        if not recognition.validate_media_url(query):
            raise ValueError("Use a supported public YouTube link.")
        target = query
    else:
        resolve_started = time.perf_counter()
        if not picked:
            picked = resolve_official(query)
        logger.info(
            "SOURCE_SEARCH provider=YouTubeSearch took %.2fs",
            time.perf_counter() - resolve_started,
        )
        if not picked or not picked.get("id"):
            raise ValueError(
                "No confident matching track was found. Try including the artist or version."
            )
        video_id = youtube.get_video_id(str(picked["id"]))
        target = f"https://www.youtube.com/watch?v={video_id}"

    resolve_started = time.perf_counter()
    client = track = cluster_host = None
    last_region_error = None
    for region_label, candidate_host in owner.TYPE_TUBE_CLUSTERS:
        try:
            client, track = _resolve_typetube_track(target, candidate_host)
            cluster_host = candidate_host
            break
        except TypeTubeOwnerKeyUnavailable:
            raise
        except TypeTubeSDKVersionError:
            raise
        except TypeTubeFreeQuotaExhausted as error:
            last_region_error = error
            logger.warning(
                "TypeTube public access is unavailable in %s; trying the next region.",
                region_label,
            )
        except Exception as error:
            last_region_error = error
            logger.warning(
                "TypeTube resolve failed in %s (%s); trying the next region.",
                region_label, type(error).__name__,
            )
    if client is None or track is None or cluster_host is None:
        raise RuntimeError(
            "TypeTube could not resolve this track from its available regions."
        ) from last_region_error
    resolve_seconds = time.perf_counter() - resolve_started
    if (
        not getattr(track, "success", False)
        or not getattr(track, "id", None)
        or not getattr(track, "best_audio", None)
        or not getattr(track.best_audio, "url", None)
    ):
        _close_typetube_client(client)
        raise RuntimeError("TypeTube did not return a playable audio stream.")

    try:
        video_id = youtube.get_video_id(str(track.id))
    except youtube.YouTubeError as error:
        _close_typetube_client(client)
        raise RuntimeError("TypeTube did not return a valid YouTube video ID.") from error
    try:
        duration = int(track.duration_seconds or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if MAX_DURATION_SEC and duration > MAX_DURATION_SEC:
        _close_typetube_client(client)
        raise ValueError(
            f"Too long ({duration // 60}:{duration % 60:02d}). "
            f"Max allowed is {MAX_DURATION_SEC // 60} min."
        )

    track_title = track.title or (picked or {}).get("title") or query
    track_artist = track.author or (picked or {}).get("artist") or ""
    display_title, display_artist = curate_track(str(track_title), str(track_artist))
    if metadata_callback:
        label = (
            f"{display_title} \u00b7 {display_artist}"
            if display_artist else display_title
        )
        try:
            metadata_callback(label)
        except Exception as error:
            logger.warning("Could not report resolved track metadata: %s", error)

    logger.info(
        "SOURCE_RESOLVE provider=TypeTube region=%s took %.2fs latency=%.0fms id=%s",
        next(label for label, host in owner.TYPE_TUBE_CLUSTERS if host == cluster_host),
        resolve_seconds,
        getattr(track, "latency_ms", 0) or 0,
        video_id,
    )
    mime_type = str(track.best_audio.mime_type or "").lower()
    source_suffix = (
        ".m4a" if "mp4" in mime_type or "m4a" in mime_type else
        ".webm" if "webm" in mime_type else
        ".ogg" if "ogg" in mime_type else
        ".mp3" if "mpeg" in mime_type else ".audio"
    )

    with tempfile.TemporaryDirectory(prefix="songbot_") as tmpdir:
        source_path = Path(tmpdir) / f"source-{video_id}{source_suffix}"
        mp3_path = Path(tmpdir) / f"{video_id}.mp3"
        download_started = time.perf_counter()
        client, track, bytes_written, average_speed = _download_typetube_source(
            client, track, target, source_path, cluster_host
        )
        _close_typetube_client(client)
        del client
        download_seconds = time.perf_counter() - download_started
        if not source_path.is_file() or source_path.stat().st_size < 1024:
            raise RuntimeError("TypeTube audio download did not produce a file.")
        source_size = source_path.stat().st_size
        source_mb = source_size / (1024 * 1024)
        if MAX_FILE_MB and source_size > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(
                f"Source audio is too large ({source_mb:.1f} MB > {MAX_FILE_MB} MB)."
            )
        if average_speed is None:
            average_speed = bytes_written / (1024 * 1024) / max(download_seconds, 0.01)
        logger.info(
            "SOURCE_DOWNLOAD provider=TypeTube took %.2fs size=%.2fMB rate=%.2fMB/s",
            download_seconds,
            source_mb,
            average_speed,
        )

        transcode_started = time.perf_counter()
        process = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source_path),
                "-vn", "-codec:a", "libmp3lame", "-b:a", f"{MP3_QUALITY}k",
                "-threads", "0", str(mp3_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if (
            process.returncode
            or not mp3_path.is_file()
            or mp3_path.stat().st_size < 1024
        ):
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Could not convert TypeTube audio to MP3: {detail[:200]}")
        actual_duration = _probe_media_duration(mp3_path)
        if actual_duration is not None and duration > 0:
            tolerance = max(2.0, duration * 0.05)
            if actual_duration + tolerance < duration:
                raise RuntimeError(
                    "TypeTube returned an incomplete audio stream "
                    f"({actual_duration:.1f}s of {duration}s). Please retry the song."
                )
            logger.info(
                "SOURCE_VERIFY expected_duration=%.1fs actual_duration=%.1fs",
                duration,
                actual_duration,
            )
        logger.info(
            "SOURCE_TRANSCODE provider=TypeTube bitrate=%sk took %.2fs",
            MP3_QUALITY,
            time.perf_counter() - transcode_started,
        )

        output_size = mp3_path.stat().st_size
        size_mb = output_size / (1024 * 1024)
        if MAX_FILE_MB and output_size > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(
                f"File too large ({size_mb:.1f} MB > {MAX_FILE_MB} MB). Try a shorter song."
            )
        cover_bytes = fetch_cover_jpeg(video_id)
        if cover_bytes:
            embed_cover_mp3(mp3_path, cover_bytes)
        audio_bytes = mp3_path.read_bytes()

    return {
        "audio_bytes": audio_bytes,
        "audio_filename": f"{video_id}.mp3",
        "cover_bytes": cover_bytes,
        "size_mb": size_mb,
        "title": display_title,
        "uploader": display_artist,
        "duration": duration,
        "album": None,
        "thumbnail": getattr(track, "thumbnail", None),
        "webpage_url": target,
        "id": video_id,
    }


# --- Interactive buttons: Edit (name/artist/cover) + Effects (slowed/8D/concert) ---
# Per-song sessions keyed (chat_id, message_id). Audio bytes kept for the
# newest songs only (RAM cap); older sessions fall back to re-download.
SESSIONS: dict = {}
SESSION_ORDER: list = []
MAX_SESSION_BYTES = 8  # max songs with full audio kept in RAM

PENDING: dict = {}  # user_id -> {op, chat_id, msg_id, expires}
PENDING_TTL = 300
PENDING_PLAYLIST_CREATE: dict[int, dict] = {}
RECOGNITION_REQUESTS: dict[str, dict] = {}
RECOGNITION_REQUEST_TTL = 15 * 60
MAX_RECOGNITION_REQUESTS = 256

EFFECTS = {
    name: {"label": preset["label"], "suffix": preset["suffix"]}
    for name, preset in emyfx.PRESETS.items()
}


def _cb(chat_id: int, msg_id: int, *parts: str) -> str:
    return ":".join(["song", str(chat_id), str(msg_id), *parts])


def main_menu(chat_id: int, msg_id: int) -> dict:
    return inline_keyboard([
        [
            custom_button("Edit", _cb(chat_id, msg_id, "menu", "edit"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["edit"]),
            custom_button("Lyrics", _cb(chat_id, msg_id, "menu", "lyrics"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["lyrics"]),
        ],
        [
            custom_button("Effect", _cb(chat_id, msg_id, "menu", "fx"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["audio_effect"]),
            custom_button("Delete", _cb(chat_id, msg_id, "menu", "delete"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["bin"], style="danger"),
        ],
        [
            custom_button(
                "Playlist",
                _cb(chat_id, msg_id, "menu", "playlist"),
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["cloud"],
            ),
        ],
    ])


def playlist_only_menu() -> dict:
    return inline_keyboard([[
        custom_button(
            "Playlist",
            "playlist:only",
            icon_custom_emoji_id=CUSTOM_EMOJI_IDS["cloud"],
        )
    ]])


def song_menu(chat_id: int, msg_id: int, playlist_only: bool = False) -> dict:
    return playlist_only_menu() if playlist_only else main_menu(chat_id, msg_id)


PLAYLIST_PAGE_SIZE = 10


def playlist_view_markup(
    playlist_id: int, page: int, songs: list[dict], total: int
) -> dict | None:
    rows = [
        [
            custom_button(
                str(song["title"])[:64],
                f"playlist:play:{song['id']}",
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["music"],
            )
        ]
        for song in songs
    ]
    if total > PLAYLIST_PAGE_SIZE:
        rows.append([
            custom_button(
                "Previous",
                f"playlist:page:{playlist_id}:{page - 1}",
                style="success",
            ),
            custom_button(
                "Next",
                f"playlist:page:{playlist_id}:{page + 1}",
                style="success",
            ),
        ])
    return inline_keyboard(rows) if rows else None


def playlist_view_text(playlist: dict, page: int, total: int) -> str:
    page_count = max(1, (total + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)
    text = f"<b>{_esc(playlist['name'])}</b> · {total} song(s)"
    if page_count > 1:
        text += f"\nPage {page + 1} of {page_count}"
    if not total:
        text += "\nThis playlist is empty. Save a song using its Playlist button."
    return text


def _playlist_view_data(
    user_id: int, playlist_id: int, page: int
) -> tuple[dict, list[dict], int] | None:
    playlist = OWNER_CONSOLE.get_playlist(user_id, playlist_id)
    if not playlist:
        return None
    total = OWNER_CONSOLE.playlist_song_count(user_id, playlist_id)
    page_count = max(1, (total + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    songs = OWNER_CONSOLE.list_playlist_songs(
        user_id,
        playlist_id,
        limit=PLAYLIST_PAGE_SIZE,
        offset=page * PLAYLIST_PAGE_SIZE,
    )
    return playlist, songs, page


async def playlist_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type != "private":
        await message.reply_text("Manage your personal playlists in a private chat.")
        return

    args = list(context.args)
    if args and args[0].lower() == "create":
        name = " ".join(args[1:]).strip()
        try:
            playlist_id = OWNER_CONSOLE.create_playlist(user.id, name)
        except ValueError as error:
            await message.reply_text(f"Could not create playlist: {_esc(error)}", parse_mode="HTML")
            return
        pending = PENDING_PLAYLIST_CREATE.pop(user.id, None)
        outcome = ""
        if pending and pending.get("expires", 0) > time.time():
            added = OWNER_CONSOLE.add_playlist_song(
                user.id, playlist_id, pending["song"]
            )
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=pending["chat_id"],
                    message_id=pending["message_id"],
                    reply_markup=song_menu(
                        pending["chat_id"],
                        pending["message_id"],
                        pending.get("playlist_only", False),
                    ),
                )
            except TelegramError as error:
                logger.warning("Could not restore song controls after playlist creation: %s", error)
            outcome = (
                " The current song was added."
                if added else " The song was already saved."
            )
        elif pending:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=pending["chat_id"],
                    message_id=pending["message_id"],
                    reply_markup=song_menu(
                        pending["chat_id"],
                        pending["message_id"],
                        pending.get("playlist_only", False),
                    ),
                )
            except TelegramError as error:
                logger.warning("Could not restore expired playlist prompt: %s", error)
            outcome = " The request to save the current song had expired."
        await message.reply_text(
            f'Created playlist <b>{_esc(name)}</b> (ID <code>{playlist_id}</code>).'
            f"{outcome}\nUse <code>/playlist</code> to browse your playlists.",
            parse_mode="HTML",
        )
        return

    if args and args[0].lower() == "delete":
        if len(args) != 2 or not args[1].isdigit():
            await message.reply_text("Usage: <code>/playlist delete &lt;id&gt;</code>", parse_mode="HTML")
            return
        if OWNER_CONSOLE.delete_playlist(user.id, int(args[1])):
            await message.reply_text("Playlist deleted.")
        else:
            await message.reply_text("Playlist not found.")
        return

    if args and args[0].lower() == "view":
        args = args[1:]
    if args:
        if len(args) != 1 or not args[0].isdigit():
            await message.reply_text(
                "Usage: <code>/playlist</code>, "
                "<code>/playlist create &lt;name&gt;</code>, "
                "<code>/playlist &lt;id&gt;</code>, or "
                "<code>/playlist delete &lt;id&gt;</code>",
                parse_mode="HTML",
            )
            return
        playlist_id = int(args[0])
        data = _playlist_view_data(user.id, playlist_id, 0)
        if not data:
            await message.reply_text("Playlist not found.")
            return
        playlist, songs, page = data
        total = OWNER_CONSOLE.playlist_song_count(user.id, playlist_id)
        await message.reply_text(
            playlist_view_text(playlist, page, total),
            parse_mode="HTML",
            reply_markup=playlist_view_markup(playlist_id, page, songs, total),
        )
        return

    playlists = OWNER_CONSOLE.list_playlists(user.id)
    if not playlists:
        await message.reply_text(
            "You don't have any playlists yet.\n"
            "Create one with <code>/playlist create &lt;name&gt;</code>, "
            "or tap Playlist on a song and create one there.",
            parse_mode="HTML",
        )
        return
    lines = [
        f"• <b>{_esc(item['name'])}</b> — {item['song_count']} song(s) "
        f"(ID <code>{item['id']}</code>)"
        for item in playlists
    ]
    await message.reply_text(
        "<b>Your playlists</b>\n\n" + "\n".join(lines)
        + "\n\nOpen one with <code>/playlist &lt;id&gt;</code>. "
        "Delete one with <code>/playlist delete &lt;id&gt;</code>.",
        parse_mode="HTML",
        reply_markup=playlist_list_markup(playlists),
    )


async def _playlist_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not query or not query.from_user or not query.message:
        return
    parts = str(query.data or "").split(":")
    if len(parts) < 2:
        await _safe_answer(query, "Invalid playlist action.", show_alert=True)
        return
    user_id = query.from_user.id
    try:
        if parts[1] == "only" and len(parts) == 2:
            await _show_playlist_picker(query, playlist_only=True)
        elif parts[1] == "add" and len(parts) == 4:
            playlist_id = int(parts[2])
            playlist_only = parts[3] == "only"
            if parts[3] not in ("only", "full"):
                raise ValueError("Invalid playlist action.")
            audio = getattr(query.message, "audio", None)
            if not audio:
                await _safe_answer(query, "This song is no longer available.", show_alert=True)
                return
            sess = SESSIONS.get((query.message.chat.id, query.message.message_id), {})
            song_data = {
                "file_id": audio.file_id,
                "video_id": sess.get("video_id"),
                "title": audio.title or sess.get("title") or "Song",
                "artist": audio.performer or sess.get("uploader"),
                "duration": audio.duration or sess.get("duration"),
                "applied_effect": sess.get("applied_effect", "off"),
            }
            added = OWNER_CONSOLE.add_playlist_song(
                user_id, playlist_id, song_data
            )
            await query.edit_message_reply_markup(
                reply_markup=song_menu(
                    query.message.chat.id,
                    query.message.message_id,
                    playlist_only,
                )
            )
            await _safe_answer(
                query,
                "Saved to playlist." if added else "Already in that playlist.",
            )
        elif parts[1] == "create" and len(parts) == 3:
            if parts[2] not in ("only", "full"):
                raise ValueError("Invalid playlist action.")
            audio = getattr(query.message, "audio", None)
            if not audio:
                await _safe_answer(query, "This song is no longer available.", show_alert=True)
                return
            now = time.time()
            for pending_user, pending in list(PENDING_PLAYLIST_CREATE.items()):
                if pending.get("expires", 0) <= now:
                    PENDING_PLAYLIST_CREATE.pop(pending_user, None)
            while len(PENDING_PLAYLIST_CREATE) >= 256:
                oldest_user = min(
                    PENDING_PLAYLIST_CREATE,
                    key=lambda pending_user: PENDING_PLAYLIST_CREATE[pending_user]["expires"],
                )
                PENDING_PLAYLIST_CREATE.pop(oldest_user, None)
            sess = SESSIONS.get((query.message.chat.id, query.message.message_id), {})
            PENDING_PLAYLIST_CREATE[user_id] = {
                "chat_id": query.message.chat.id,
                "message_id": query.message.message_id,
                "expires": now + 10 * 60,
                "playlist_only": parts[2] == "only",
                "song": {
                    "file_id": audio.file_id,
                    "video_id": sess.get("video_id"),
                    "title": audio.title or sess.get("title") or "Song",
                    "artist": audio.performer or sess.get("uploader"),
                    "duration": audio.duration or sess.get("duration"),
                    "applied_effect": sess.get("applied_effect", "off"),
                },
            }
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text="Send <code>/playlist create &lt;name&gt;</code> to create a playlist "
                "and save this song to it. Use <code>/cancel</code> to discard.",
                parse_mode="HTML",
                reply_to_message_id=query.message.message_id,
            )
            await _safe_answer(query)
        elif parts[1] == "cancel" and len(parts) == 3:
            if parts[2] not in ("only", "full"):
                raise ValueError("Invalid playlist action.")
            await query.edit_message_reply_markup(
                reply_markup=song_menu(
                    query.message.chat.id,
                    query.message.message_id,
                    parts[2] == "only",
                )
            )
            await _safe_answer(query)
        elif parts[1] == "view" and len(parts) == 4:
            playlist_id, page = int(parts[2]), int(parts[3])
            data = _playlist_view_data(user_id, playlist_id, page)
            if not data:
                await _safe_answer(query, "Playlist not found.", show_alert=True)
                return
            playlist, songs, page = data
            total = OWNER_CONSOLE.playlist_song_count(user_id, playlist_id)
            await query.edit_message_text(
                playlist_view_text(playlist, page, total),
                parse_mode="HTML",
                reply_markup=playlist_view_markup(
                    playlist_id, page, songs, total
                ),
            )
            await _safe_answer(query)
        elif parts[1] == "page" and len(parts) == 4:
            playlist_id, page = int(parts[2]), int(parts[3])
            data = _playlist_view_data(user_id, playlist_id, page)
            if not data:
                await _safe_answer(query, "Playlist not found.", show_alert=True)
                return
            playlist, songs, page = data
            total = OWNER_CONSOLE.playlist_song_count(user_id, playlist_id)
            await query.edit_message_text(
                playlist_view_text(playlist, page, total),
                parse_mode="HTML",
                reply_markup=playlist_view_markup(
                    playlist_id, page, songs, total
                ),
            )
            await _safe_answer(query)
        elif parts[1] == "play" and len(parts) == 3:
            saved_song = OWNER_CONSOLE.get_playlist_song(user_id, int(parts[2]))
            if not saved_song or not saved_song.get("video_id"):
                await _safe_answer(query, "Song not found in your playlists.", show_alert=True)
                return
            if not await _safe_answer(query):
                return
            delivery_update = SimpleNamespace(
                message=query.message,
                effective_user=query.from_user,
                effective_chat=query.message.chat,
            )
            delivery_context = SimpleNamespace(
                args=[
                    f"https://www.youtube.com/watch?v={saved_song['video_id']}"
                ],
                bot=context.bot,
                application=context.application,
                no_controls=True,
            )
            await song(delivery_update, delivery_context)
        else:
            await _safe_answer(query, "Invalid playlist action.", show_alert=True)
    except (ValueError, TelegramError) as error:
        logger.warning("Playlist action failed for user %d: %s", user_id, error)
        await _safe_answer(query, str(error), show_alert=True)


async def _show_playlist_picker(query, *, playlist_only: bool = False) -> None:
    if not query or not query.from_user or not query.message:
        return
    if query.message.chat.type != "private":
        await _safe_answer(
            query,
            "Open a private chat with the bot to save songs to your playlists.",
            show_alert=True,
        )
        return
    playlists = OWNER_CONSOLE.list_playlists(query.from_user.id)
    rows = [
        [
            custom_button(
                f"{item['name']} ({item['song_count']})"[:64],
                f"playlist:add:{item['id']}:"
                f"{'only' if playlist_only else 'full'}",
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["cloud"],
            )
        ]
        for item in playlists[:48]
    ]
    rows.append([
        custom_button(
            "Create playlist",
            f"playlist:create:{'only' if playlist_only else 'full'}",
            icon_custom_emoji_id=CUSTOM_EMOJI_IDS["cloud"],
        ),
        custom_button(
            "Cancel",
            f"playlist:cancel:{'only' if playlist_only else 'full'}",
        ),
    ])
    await query.edit_message_reply_markup(reply_markup=inline_keyboard(rows))
    await _safe_answer(query)


def playlist_list_markup(playlists: list[dict]) -> dict:
    return inline_keyboard([
        [
            custom_button(
                f"{item['name']} ({item['song_count']})"[:64],
                f"playlist:view:{item['id']}:0",
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["cloud"],
            )
        ]
        for item in playlists[:48]
    ])


def parse_song_args(args: list[str]) -> tuple[str, bool]:
    inline_selection = bool(args and args[-1] == "--inline-result")
    query_args = args[:-1] if inline_selection else args
    return " ".join(query_args).strip(), inline_selection


def _resolve_inline_audio(entry: dict) -> InlineQueryResultAudio:
    video_id = youtube.get_video_id(str(entry.get("id") or ""))
    candidate_url = str(entry.get("url") or "")
    if (
        recognition.validate_media_url(candidate_url)
        and youtube.get_video_id(candidate_url) == video_id
    ):
        target = candidate_url
    else:
        target = f"https://www.youtube.com/watch?v={video_id}"

    track = youtube.resolve(target)
    audio = youtube.select_inline_audio_format(track["formats"])
    title, artist = curate_track(
        str(track.get("title") or entry.get("title") or "Unknown"),
        str(
            track.get("author")
            or entry.get("artist")
            or entry.get("channel")
            or ""
        ),
    )
    try:
        duration = int(track.get("duration") or entry.get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0
    thumbnail = track.get("thumbnail") or entry.get("thumbnail")
    if (
        not isinstance(thumbnail, str)
        or not thumbnail.startswith("https://")
        or len(thumbnail) > 2048
    ):
        thumbnail = None
    return InlineQueryResultAudio(
        id=video_id,
        audio_url=audio["url"],
        title=title[:64],
        performer=artist[:64] if artist else None,
        audio_duration=duration or None,
        thumb_url=thumbnail,
    )


async def _inline_link_answer(inline_query, url: str) -> None:
    """Offer a pasted YouTube link as a playable inline audio result."""
    if not recognition.validate_media_url(url):
        await inline_query.answer([], cache_time=0, is_personal=True)
        return
    video_id = youtube.get_video_id(url)
    await _answer_inline_candidates(
        inline_query,
        [{"id": video_id, "url": url}],
    )


async def _answer_inline_candidates(inline_query, candidates: object) -> None:
    entries = candidates if isinstance(candidates, (list, tuple)) else []
    normalized = []
    seen_ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            video_id = youtube.get_video_id(str(entry.get("id") or ""))
        except youtube.YouTubeError:
            continue
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        normalized.append({**entry, "id": video_id})
        if len(normalized) >= 5:
            break

    resolved = await asyncio.gather(
        *(
            asyncio.to_thread(_resolve_inline_audio, entry)
            for entry in normalized
        ),
        return_exceptions=True,
    )
    results = []
    for entry, result in zip(normalized, resolved):
        if isinstance(result, Exception):
            logger.info(
                "Could not resolve inline audio for %s (%s).",
                entry["id"],
                type(result).__name__,
            )
            continue
        results.append(result)
    await inline_query.answer(results, cache_time=0, is_personal=True)


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if not inline_query:
        return
    query = str(inline_query.query or "").strip()
    if len(query) > MAX_INLINE_QUERY_LENGTH:
        await inline_query.answer([], cache_time=0, is_personal=True)
        return
    if not query:
        await inline_query.answer([], cache_time=5, is_personal=True)
        return

    try:
        if URL_RE.match(query):
            await _inline_link_answer(inline_query, query)
            return
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", query):
            await _answer_inline_candidates(
                inline_query,
                [{
                    "id": query,
                    "url": f"https://www.youtube.com/watch?v={query}",
                }],
            )
            return
        candidates = await resolve_candidates_async(
            query, context.application, limit=10
        )
        await _answer_inline_candidates(inline_query, candidates)
    except Exception:
        logger.exception("Inline song search failed for %r", query)
        await inline_query.answer([], cache_time=0, is_personal=True)


def _no_dashes(text: str) -> str:
    """Strip em dashes and hyphens from user facing text."""
    text = str(text).replace(" — ", ", ").replace(" – ", ", ")
    text = text.replace("—", ",").replace("–", ",")
    return text.replace("-", " ")


def _esc(text: object) -> str:
    """HTML escape for Telegram messages (quotes stay raw, Telegram style)."""
    return html.escape(str(text), quote=False)


MUSIC_FACTS = [
    "Lata Mangeshkar recorded over 6,500 songs in her career.",
    "Lata Mangeshkar sang 712 songs for Laxmikant Pyarelal, her highest count with any composer.",
    "Lata Mangeshkar's career spanned seven decades.",
    "Lata Mangeshkar received the Bharat Ratna in 2001.",
    "Asha Bhosle holds the Guinness record for the most recorded artist, over 11,000 songs.",
    "Asha Bhosle has sung in over 20 languages.",
    "Asha Bhosle earned two Grammy nominations.",
    "Asha Bhosle is Lata Mangeshkar's younger sister.",
    "Asha Bhosle sang Piya Tu Ab To Aaja and Dum Maro Dum.",
    "Mohammed Rafi was fondly called the Voice of God.",
    "Mohammed Rafi won six Filmfare Awards plus a National Award.",
    "Rafi and Kishore Kumar sang over 70 songs together.",
    "Kishore Kumar once took the same fee as his idol Rafi, refusing a bigger offer.",
    "Kishore Kumar's real name was Abhas Kumar Ganguly.",
    "Kishore Kumar never took formal classical training.",
    "Kishore Kumar became Rajesh Khanna's signature voice in the 70s.",
    "Kishore Kumar's yodelling was inspired by Jimmie Rodgers.",
    "Mukesh is remembered as Raj Kapoor's voice.",
    "Mukesh sang Kabhi Kabhie Mere Dil Mein.",
    "Mohammed Rafi sang Chaudhvin Ka Chand.",
    "Rafi sang Yahoo for Shammi Kapoor in Junglee.",
    "Kumar Sanu once recorded 28 songs in a single day.",
    "Kavita Krishnamurthy has sung over 20,000 songs.",
    "Kavita Krishnamurthy sang her first film song at 16, a duet with Lata Mangeshkar.",
    "Kavita Krishnamurthy thought Hawa Hawai was meant for Asha Bhosle.",
    "Tu Hi Re from Bombay pairs Kavita Krishnamurthy with Hariharan.",
    "Dola Re Dola pairs Kavita Krishnamurthy with Shreya Ghoshal.",
    "Sonu Nigam sang the Kal Ho Naa Ho title track.",
    "Sonu Nigam hosted Sa Re Ga Ma.",
    "Shankar Mahadevan's Breathless flows without a pause.",
    "Amitabh Bachchan sang Mere Angne Mein.",
    "Armaan Malik is Anu Malik's nephew.",
    "Amaal Mallik and Armaan Malik are brothers.",
    "Armaan Malik sang Main Hoon Hero Tera.",
    "Sunidhi Chauhan sang Sheila Ki Jawani.",
    "Mamta Sharma sang Munni Badnaam Hui.",
    "Neha Kakkar judges Indian Idol.",
    "Kailash Kher broke through with Allah Ke Bande.",
    "Mohit Chauhan sang Sadda Haq and Tum Se Hi.",
    "Udit Narayan sang Papa Kehte Hain.",
    "Shaan sang Tanha Dil.",
    "R. D. Burman was fondly called Pancham.",
    "R. D. Burman composed Sholay.",
    "Mithoon composed Tum Hi Ho.",
    "Pritam composed Kesariya.",
    "Pritam composed Barfi, Yeh Jawaani Hai Deewani and Dangal.",
    "Shankar Ehsaan Loy composed Dil Chahta Hai and Kal Ho Naa Ho.",
    "Amit Trivedi composed Dev D, Queen and Udta Punjab.",
    "Irshad Kamil wrote the lyrics for Rockstar and Tamasha.",
    "Nadeem Shravan composed the original Aashiqui.",
    "Nadeem Shravan also composed Saajan.",
    "Anand Bakshi wrote 24 drafts of Mere Khwabo Mein Jo Aaye.",
    "Anand Bakshi wrote thousands of film songs.",
    "Sahir Ludhianvi wrote Kabhi Kabhie and Pyaasa.",
    "Khayyam composed Kabhi Kabhie and Umrao Jaan.",
    "S. D. Burman composed Guide, Aradhana and Abhimaan.",
    "Aradhana revived Kishore Kumar's singing career.",
    "Madan Mohan composed Woh Kaun Thi.",
    "O. P. Nayyar famously never recorded with Lata Mangeshkar.",
    "Naushad composed Mughal e Azam.",
    "Jatin Lalit composed DDLJ and Kuch Kuch Hota Hai.",
    "Anu Malik composed Main Hoon Na and Baazigar.",
    "Vishal Shekhar composed Om Shanti Om and Chennai Express.",
    "Vishal Dadlani fronts the band Pentagram.",
    "Bappi Lahiri brought disco to Bollywood with Disco Dancer.",
    "Gulzar wrote the Oscar winning lyrics of Jai Ho.",
    "A. R. Rahman is called the Mozart of Madras.",
    "A. R. Rahman's birth name was A. S. Dileep Kumar.",
    "A young A. R. Rahman played keyboards in Ilaiyaraaja's troupe.",
    "Ilaiyaraaja has scored over 8,000 songs in nine languages.",
    "Ilaiyaraaja once composed a film's full score in a single day.",
    "A. R. Rahman spent three years on Rang De Basanti and two months on Slumdog Millionaire.",
    "A. R. Rahman won two Oscars for Slumdog Millionaire.",
    "Slumdog Millionaire won eight Oscars in all.",
    "Jai Ho won the Oscar for Best Original Song.",
    "Naatu Naatu from RRR won the Oscar for Best Original Song.",
    "A. R. Rahman's debut film score was Roja.",
    "A. R. Rahman composed the music for Lagaan.",
    "Lagaan was nominated for the Foreign Language Oscar.",
    "Mother India was India's first Oscar nominated film.",
    "Satyajit Ray received an honorary Oscar.",
    "Bhanu Athaiya was the first Indian to win an Oscar.",
    "Resul Pookutty won the sound mixing Oscar for Slumdog Millionaire.",
    "A. R. Rahman himself sang Dil Se Re.",
    "Senior composers once wrote to the President of India protesting the reign of Laxmikant Pyarelal.",
    "The Saiyaara title track credits three composers.",
    "Kesariya is from Brahmastra.",
    "Tum Hi Ho is from Aashiqui 2.",
    "Aashiqui 2 stars Aditya Roy Kapur and Shraddha Kapoor.",
    "Channa Mereya is from Ae Dil Hai Mushkil.",
    "Arijit Singh sang three songs in Ae Dil Hai Mushkil.",
    "Arijit Singh sang Dilliwaali Girlfriend, Badtameez Dil and Kabira for Yeh Jawaani Hai Deewani.",
    "Tum Hi Ho earned Arijit Singh his first Filmfare Award.",
    "Arijit Singh debuted in Bollywood with Phir Mohabbat from Murder 2.",
    "Arijit Singh competed on Fame Gurukul in 2005 before playback fame.",
    "Arijit Singh is among the most followed artists on Spotify in the world.",
    "Arijit Singh was born in Jiaganj in West Bengal.",
    "Arijit Singh also sings in Bengali.",
    "Early in his career, Arijit Singh worked with composer Pritam.",
    "Agar Tum Saath Ho is from Tamasha.",
    "Hawayein is from Jab Harry Met Sejal.",
    "Sadda Haq and Nadaan Parindey are from Rockstar.",
    "Kun Faya Kun is from Rockstar.",
    "London Thumakda is from Queen.",
    "Gerua and Janam Janam are both from Dilwale.",
    "Laal Ishq is from Ram Leela.",
    "Deewani Mastani is from Bajirao Mastani.",
    "Sanjay Leela Bhansali composes music for his own films.",
    "Chaiyya Chaiyya was shot on top of a moving train.",
    "Chaiyya Chaiyya is sung by Sukhwinder Singh and Sapna Awasthi.",
    "Mere Khwabo Mein Jo Aaye was the first song recorded for DDLJ.",
    "Pyar Kiya To Darna Kya took two years to film on a Sheesh Mahal set.",
    "The Mughal e Azam song set cost about ten times a typical film budget then.",
    "Sunil Dutt got exhausted lip syncing a 15 second high pitch prelude in Humraaz.",
    "Abhijeet recorded Chalti Hai Kya assuming Govinda, but it was filmed on Salman Khan.",
    "Sunn Raha Hai has both male and female versions.",
    "Enna Sona is from OK Jaanu.",
    "Naina is from Dangal.",
    "Bolna is from Kapoor and Sons.",
    "Khairiyat is from Chhichhore.",
    "Shayad is from Love Aaj Kal.",
    "Samjhawan is sung by Arijit Singh and Shreya Ghoshal.",
    "Roke Na Ruke Naina is from Badrinath Ki Dulhania.",
    "Humsafar is from Badrinath Ki Dulhania.",
    "Zaalima is from Raees.",
    "Nashe Si Chadh Gayi is from Befikre.",
    "Ve Maahi is from Kesari.",
    "Tera Ban Jaunga is from Kabir Singh.",
    "Tera Yaar Hoon Main is from Sonu Ke Titu Ki Sweety.",
    "Duniyaa is from Luka Chuppi.",
    "Teri Mitti from Kesari honors soldiers.",
    "Phir Bhi Tumko Chaahunga is from Half Girlfriend.",
    "Muskurane Ki Wajah is from CityLights.",
    "Soch Na Sake is from Airlift.",
    "Raabta Kehte Hain Khuda Ne is from Agent Vinod.",
    "Shreya Ghoshal won Sa Re Ga Ma as a teen, then debuted with Devdas.",
    "Shreya Ghoshal sang Tujh Mein Rab Dikhta Hai.",
    "Rahat Fateh Ali Khan is Nusrat Fateh Ali Khan's nephew.",
    "Rahat Fateh Ali Khan sang Tere Mast Mast Do Nain.",
    "Nusrat Fateh Ali Khan is often called the King of Qawwali.",
    "Afreen Afreen was first sung by Nusrat Fateh Ali Khan.",
    "Noor Jehan was called Mallika e Tarannum.",
    "Mehdi Hassan is called the Shahenshah e Ghazal.",
    "Qawwali is devotional music rooted in Sufi tradition.",
    "Coke Studio Pakistan began in 2008.",
    "Pasoori by Ali Sethi and Shae Gill crossed a billion views.",
    "Kahani Suno made Kaifi Khalil a star.",
    "Jal's Aadat defined pop rock across the subcontinent in the 2000s.",
    "Strings broke through with Duur.",
    "Diljit Dosanjh played the first Punjabi set at Coachella in 2023.",
    "Diljit Dosanjh debuted on The Tonight Show in 2024.",
    "Diljit Dosanjh started by singing devotional songs at a gurdwara.",
    "Diljit Dosanjh released his first album at 16.",
    "Diljit Dosanjh played Chamkila in the 2024 Netflix film.",
    "Proper Patola was the first Punjabi song on Vevo.",
    "Lak 28 Kudi Da topped BBC's Asian chart in 2011.",
    "AP Dhillon played Coachella in 2024.",
    "Brown Munde has over 700 million YouTube views.",
    "Dil Diyan Gallan is from Tiger Zinda Hai.",
    "O Saathi is from Baaghi 2.",
    "Pehli Nazar Mein is from Race.",
    "Jeena Jeena is from Badlapur.",
    "Tere Sang Yaara is from Rustom.",
    "Woh Lamhe from Zeher was Atif Aslam's Bollywood debut.",
    "Raataan Lambiyan is from Shershaah.",
    "Tum Hi Aana is from Marjaavaan.",
    "Lut Gaye is Jubin Nautiyal's smash single.",
    "Teri Mitti is sung by B Praak.",
    "Mann Bharrya was B Praak's breakout single.",
    "Alam Ara from 1931 was India's first talkie.",
    "De De Khuda Ke Naam Par was the first song heard in an Indian film.",
    "Playback singing began with the 1935 Bengali film Bhagya Chakra.",
    "Dhoop Chhaon brought playback singing to Hindi cinema.",
    "Tansen was one of Akbar's Navaratnas.",
    "Tagore wrote the anthems of India and Bangladesh.",
    "Bismillah Khan popularized the shehnai.",
    "M. S. Subbulakshmi was the first musician to get the Bharat Ratna.",
    "S. P. Balasubrahmanyam is credited with over 40,000 songs.",
    "Ravi Shankar won Grammy Awards and the Bharat Ratna.",
    "Zakir Hussain is a world famous tabla maestro.",
    "Amjad Ali Khan is a sarod maestro.",
    "Hariprasad Chaurasia is a bansuri legend.",
    "Shivkumar Sharma popularized the santoor.",
    "Hariharan is half of the duo Colonial Cousins.",
    "The Hanuman Chalisa, sung by Hariharan, is India's most watched YouTube video.",
    "Rowdy Baby is among the most watched Tamil songs online.",
    "Anirudh composed the viral Why This Kolaveri Di.",
    "Lucky Ali broke through with O Sanam.",
    "Alisha Chinai's Made in India was a 90s pop landmark.",
    "Daler Mehndi's Tunak Tunak became a global meme decades later.",
    "Mundian To Bach Ke was remixed with Jay Z as Beware of the Boys.",
    "M. I. A.'s Paper Planes features in Slumdog Millionaire.",
    "The Pussycat Dolls released an English remix of Jai Ho.",
    "The Singh Is Kinng title track features Snoop Dogg.",
    "Kylie Minogue sang Chiggy Wiggy for Blue.",
    "Farhan Akhtar sang his own songs in Rock On.",
    "Priyanka Chopra recorded Exotic with Pitbull.",
    "Akon sang Chammak Challo for Ra.One.",
    "Blinding Lights is Spotify's most streamed song and the first to cross 5 billion plays.",
    "Shape of You is Spotify's second most streamed song.",
    "Thinking Out Loud was the first song to cross 500 million Spotify plays.",
    "One Dance was the first song to cross a billion Spotify plays.",
    "Coldplay's Viva la Vida was Spotify's first most streamed song.",
    "The Killers held Spotify's streaming crown for over three years.",
    "Taylor Swift's Fate of Ophelia set Spotify's biggest single day record.",
    "Mariah Carey's Christmas hit holds Spotify's biggest non debut day record.",
    "Die With A Smile spent 201 days atop Spotify's daily chart.",
    "Rockstar gave Post Malone 17 straight weeks atop Spotify's weekly chart.",
    "Ed Sheeran wrote songs for One Direction.",
    "Bad Bunny was Spotify's most streamed artist three years running.",
    "Taylor Swift's Eras Tour was the first tour to cross 2 billion dollars.",
    "Beyonce has won more Grammys than anyone in history.",
    "Stevie Wonder won Album of the Year three times.",
    "U2 has 22 Grammys, the most for a band.",
    "Michael Jackson won eight Grammys in one night.",
    "John Williams has over 50 Oscar nominations.",
    "Gangnam Style was YouTube's first billion view video.",
    "Gangnam Style was the first video to cross 2 billion views.",
    "See You Again held the most viewed crown for only 25 days.",
    "Despacito was the first video to cross 5 billion views.",
    "Baby Shark passed Despacito as the most watched video in 2020.",
    "Baby Shark was the first video to cross 10 billion views.",
    "Wheels on the Bus passed Despacito as the second most watched video.",
    "A Nike ad with Ronaldinho was the first video to hit a million views.",
    "Charlie Bit My Finger was sold as an NFT.",
    "The Beatles spent 176 weeks atop the UK albums chart.",
    "Counting Beatles, Wings and solo work, Paul McCartney has 23 UK number one albums.",
    "Robbie Williams has 16 UK number one albums, the most for a solo artist.",
    "Bohemian Rhapsody was Christmas number one twice with the same recording.",
    "Bohemian Rhapsody runs nearly six minutes long.",
    "Queen have spent over 48 years on the UK albums chart.",
    "ABBA won Eurovision with Waterloo.",
    "ABBA returned with Voyage after 40 years.",
    "Michael Jackson debuted the moonwalk in 1983.",
    "Thriller is the best selling album of all time.",
    "White Christmas is the best selling single ever.",
    "Yesterday is among the most covered songs ever.",
    "Happy Birthday is the most recognized song in English.",
    "I Will Always Love You was written by Dolly Parton.",
    "My Way began life as a French song.",
    "Old Town Road spent a record 19 weeks atop the Hot 100.",
    "Bailando spent 41 weeks atop Billboard's Latin chart.",
    "Despacito spent 16 weeks atop the Hot 100.",
    "Waka Waka was the 2010 World Cup anthem.",
    "Heat Waves spent 91 weeks on the Hot 100.",
    "BTS Dynamite set a 24 hour YouTube debut record.",
    "Beethoven wrote his ninth symphony while deaf.",
    "Mozart composed more than 600 works.",
    "Mozart left his Requiem unfinished.",
    "Bach composed over 1,000 works.",
    "Vivaldi was nicknamed the Red Priest.",
    "Handel wrote Messiah in 24 days.",
    "The 1812 Overture uses real cannons.",
    "Fans once swooned over Liszt in a frenzy called Lisztomania.",
    "Schubert wrote over 600 songs.",
    "Wagner's Ring cycle runs over 14 hours.",
    "A piano has 88 keys and a guitar has six strings.",
    "A violin has four strings.",
    "A trumpet has three valves.",
    "Flutes are among the oldest known instruments.",
    "A vinyl record spins at 33 or 45 RPM.",
    "The tabla is a pair of hand drums.",
    "The sitar went global through Ravi Shankar.",
    "An earworm is a song stuck in your head.",
    "Your heartbeat can subtly sync with music tempo.",
    "Group singing is linked to better mood in studies.",
    "Calm music may help lower stress hormones.",
    "Spotify launched in India in 2019.",
    "Jingle Bells was the first song played in space.",
]


_fact_index = 0


def next_fact() -> str:
    """Return the next music fact, cycling without repeats until exhausted."""
    global _fact_index
    fact = MUSIC_FACTS[_fact_index % len(MUSIC_FACTS)]
    _fact_index += 1
    return fact


def display_query(query: str) -> str:
    """Title-case a user query for display ('gehra hua' -> 'Gehra Hua')."""
    text = " ".join(
        word[:1].upper() + word[1:].lower() for word in str(query).split()
    )
    if len(text) > 60:
        text = text[:57].rstrip() + "…"
    return text


def status_html(icon_id: str, fallback: str, heading: str, body: str | None = None) -> str:
    """One consistent single-line card style for every status message."""
    heading = _no_dashes(heading)
    text = f'<tg-emoji emoji-id="{icon_id}">{fallback}</tg-emoji> <b>{heading}</b>'
    if body:
        text += f" {_no_dashes(body)}"
    return text


def _punctuated(heading: str) -> str:
    return heading if heading.endswith((".", "!", "?", "…")) else heading + "."


def warn_html(heading: str, detail: str | None = None) -> str:
    body = _esc(detail) if detail else None
    return status_html(WARN_ICON, "⚠️", _punctuated(heading), body)


def error_html(heading: str, detail: str) -> str:
    return status_html(ERROR_ICON, "❌", _punctuated(heading), _esc(detail))


def searching_status_html(query: str) -> str:
    return status_html(
        SEARCH_ICON, "🔍", f'Searching "{_esc(display_query(query))}".',
        _esc(next_fact()),
    )


def downloading_status_html(track_name: str | None = None) -> str:
    body = f"<code>{_esc(track_name)}</code>" if track_name else None
    return status_html(CUSTOM_EMOJI_IDS["download"], "⬇️", "Downloading…", body)


def rendering_status_html(effect_name: str) -> str:
    return status_html(MUSIC_ICON, "🎵", f"Rendering {_esc(effect_name)}…")


def uploading_status_html(title: str, _size_mb: float = 0.0) -> str:
    return status_html(
        UPLOAD_ICON, "⬆️", "Uploading…",
        f"<code>{_esc(title)}</code>",
    )


def recognition_status_html() -> str:
    return status_html(SEARCH_ICON, "🔍", "Listening…")


def edit_menu(chat_id: int, msg_id: int) -> dict:
    return inline_keyboard([
        [
            custom_button("Edit name", _cb(chat_id, msg_id, "edit", "name"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["edit"]),
            custom_button("Edit artist", _cb(chat_id, msg_id, "edit", "artist"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["edit_artist"]),
        ],
        [
            custom_button("Edit thumbnail", _cb(chat_id, msg_id, "edit", "thumb"),
                          icon_custom_emoji_id=CUSTOM_EMOJI_IDS["edit_thumbnail"]),
        ],
        [custom_button("Back", _cb(chat_id, msg_id, "back", "main"))],
    ])


def fx_menu(
    chat_id: int, msg_id: int, active_effect: str = "off"
) -> dict:
    buttons = []
    for name, effect in EFFECTS.items():
        label = effect["label"]
        if name == active_effect:
            buttons.append(custom_button(
                "Original",
                _cb(chat_id, msg_id, "fx", "off"),
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["effect_off"],
            ))
        else:
            buttons.append(custom_button(
                label,
                _cb(chat_id, msg_id, "fx", name),
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["audio_effect"],
            ))
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.append([custom_button("Back", _cb(chat_id, msg_id, "back", "main"))])
    return inline_keyboard([
        *rows,
    ])


def remember_session(chat_id: int, msg_id: int, data: dict) -> None:
    key = (chat_id, msg_id)
    SESSIONS[key] = data
    if key in SESSION_ORDER:
        SESSION_ORDER.remove(key)
    SESSION_ORDER.append(key)
    # RAM cap: strip audio bytes from oldest sessions, keep metadata.
    with_bytes = [
        k for k in SESSION_ORDER
        if (
            SESSIONS.get(k, {}).get("original_audio_bytes")
            or SESSIONS.get(k, {}).get("audio_bytes")
        )
    ]
    while len(with_bytes) > MAX_SESSION_BYTES:
        old = with_bytes.pop(0)
        SESSIONS[old].pop("audio_bytes", None)
        SESSIONS[old].pop("original_audio_bytes", None)


async def apply_effect(audio_bytes: bytes, effect: str, cover_bytes: bytes | None) -> bytes:
    """Render an audio preset without blocking the event loop."""
    with tempfile.TemporaryDirectory(prefix="songfx_") as tmpdir:
        src = Path(tmpdir) / "in.mp3"
        out = Path(tmpdir) / "out.mp3"
        await asyncio.to_thread(src.write_bytes, audio_bytes)
        await emyfx.render_to_path(
            src, out, effect, bitrate=f"{MP3_QUALITY}k", timeout=120,
        )
        if cover_bytes:
            await asyncio.to_thread(embed_cover_mp3, out, cover_bytes)
        return await asyncio.to_thread(out.read_bytes)



# Custom Telegram emoji IDs
MUSIC_ICON = CUSTOM_EMOJI_IDS["music"]
SEARCH_ICON = CUSTOM_EMOJI_IDS["search"]
UPLOAD_ICON = CUSTOM_EMOJI_IDS["upload"]
WARN_ICON = CUSTOM_EMOJI_IDS["warning"]
ERROR_ICON = CUSTOM_EMOJI_IDS["error"]
SETTINGS_ICON = CUSTOM_EMOJI_IDS["settings"]
UPDATE_CHANNEL_URL = os.getenv("UPDATE_CHANNEL_URL", "https://t.me/EndBots").strip()


def welcome_keyboard() -> dict:
    return inline_keyboard([[
        custom_button(
            "Update Channel", url=UPDATE_CHANNEL_URL, style="success"
        ),
    ]])
START_WELCOME_HTML = (
    f'<tg-emoji emoji-id="{MUSIC_ICON}">🎵</tg-emoji> Welcome to Emy\n'
    "Search for any song and get straight to the music. From the song you can't "
    "remember the name of to the one you've had on repeat all week, Emy makes "
    "it easy to find what you're looking for.\n"
    "\n"
    "Send a song name to get started, or use /playlist to save and revisit songs.\n"
    "\n"
    "That's it. Find your song, hit play, enjoy."
)


HELP_HTML = (
    f'<tg-emoji emoji-id="{MUSIC_ICON}">🎵</tg-emoji> <b>Welcome to Emy</b>\n'
    "\n"
    "Emy lets you search for songs by name or find music from video and audio links. "
    "You can use /settings to customize your music with effects like Slowed, 8D, 360°, 3D, Concert, Vinyl, and LoFi. "
    "Save songs to your private playlists with /playlist. "
    "You can also search for songs directly in any chat by typing @Emeliyabot followed by the song name."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send clean, polished welcome message."""
    await OWNER_CONSOLE.start_broadcasts(update, context)
    chat = update.effective_chat
    user = update.effective_user
    markup = welcome_keyboard()
    if chat and user and chat.type in ("group", "supergroup"):
        await send_ephemeral_message(
            context.bot, chat.id, user.id, START_WELCOME_HTML, parse_mode="HTML",
            reply_markup=markup,
        )
    elif update.message:
        await update.message.reply_text(
            START_WELCOME_HTML, parse_mode="HTML", reply_markup=markup
        )
    elif chat:
        await context.bot.send_message(
            chat.id, START_WELCOME_HTML, parse_mode="HTML", reply_markup=markup
        )



async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the help overview without any buttons."""
    chat = update.effective_chat
    user = update.effective_user
    if chat and user and chat.type in ("group", "supergroup"):
        await send_ephemeral_message(
            context.bot, chat.id, user.id, HELP_HTML, parse_mode="HTML",
        )
    elif update.message:
        await update.message.reply_text(HELP_HTML, parse_mode="HTML")
    elif chat:
        await context.bot.send_message(chat.id, HELP_HTML, parse_mode="HTML")


SEARCH_PAGE_SIZE = 10
SEARCH_PAGE_COLS = 2


def _result_button_label(candidate: dict) -> str:
    title, artist = curate_track(
        derive_title(candidate), derive_artist(candidate)
    )
    label = f"{title} · {artist}" if artist else title
    label = _no_dashes(label)
    if len(label) > 32:
        label = label[:31].rstrip() + "…"
    return label


def search_results_keyboard(token: str, selection: dict) -> dict:
    candidates = selection["candidates"]
    page = selection.get("page", 0)
    start = page * SEARCH_PAGE_SIZE
    page_items = candidates[start:start + SEARCH_PAGE_SIZE]
    rows = []
    for chunk in range(0, len(page_items), SEARCH_PAGE_COLS):
        row = []
        for offset, candidate in enumerate(
            page_items[chunk:chunk + SEARCH_PAGE_COLS], start=start + chunk
        ):
            row.append(custom_button(
                _result_button_label(candidate),
                f"searchpick:{token}:{offset}",
                icon_custom_emoji_id=CUSTOM_EMOJI_IDS["music"],
                style="primary",
            ))
        rows.append(row)
    rows.append([
        custom_button(
            "Previous",
            f"searchpage:{token}:{page - 1}",
            icon_custom_emoji_id="5258236805890710909",
            style="success",
        ),
        custom_button("Close", f"searchclose:{token}", style="danger"),
        custom_button(
            "Next",
            f"searchpage:{token}:{page + 1}",
            icon_custom_emoji_id="5260450573768990626",
            style="success",
        ),
    ])
    return inline_keyboard(rows)


def search_results_text(selection: dict) -> str:
    total = len(selection["candidates"])
    page_count = max(
        1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
    )
    page = selection.get("page", 0) + 1
    query = str(selection.get("query") or "").strip()
    heading = f'Results for "{_esc(_no_dashes(display_query(query)))}"' if query else "Results"
    return (
        f'<tg-emoji emoji-id="{MUSIC_ICON}">🎵</tg-emoji> <b>{heading}</b> '
        f"(Page {page} of {page_count} · {total} found)"
    )


async def song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            status_html(
                SEARCH_ICON, "🔍", "How to search:",
                "<code>/search song name or link</code>, or just send a song name.",
            ),
            parse_mode="HTML",
        )
        return

    t0 = time.perf_counter()
    query = " ".join(context.args).strip()
    no_controls = bool(getattr(context, "no_controls", False))
    uid = update.effective_user.id if update.effective_user else None
    default_fx = get_default_effect(uid)
    recognition_candidate = getattr(context, "recognition_candidate", None)
    recognized_song = getattr(context, "recognized_song", None)

    if not recognition_candidate and not URL_RE.match(query.strip()):
        search_status = await update.message.reply_text(
            searching_status_html(query),
            parse_mode="HTML",
        )
        try:
            candidates = await resolve_candidates_async(
                    query, context.application, limit=25
            )
        except Exception as error:
            logger.exception("Search failed for query %r", query[:100])
            await search_status.edit_text(
                error_html(
                    "Search temporarily unavailable",
                    "The music catalog could not respond. Please try again.",
                ),
                parse_mode="HTML",
            )
            return
        if not candidates:
            await search_status.edit_text(
                warn_html("No matches found", "Try adding the artist or version."),
                parse_mode="HTML",
            )
            return

        now = time.time()
        for old_token, old_selection in list(SEARCH_SELECTIONS.items()):
            if old_selection["expires"] <= now:
                SEARCH_SELECTIONS.pop(old_token, None)
        selection_token = os.urandom(6).hex()
        selection = {
            "query": query,
            "requester_id": uid,
            "candidates": candidates,
            "expires": now + SEARCH_SELECTION_TTL,
            "processing": False,
            "page": 0,
        }
        SEARCH_SELECTIONS[selection_token] = selection
        while len(SEARCH_SELECTIONS) > 256:
            oldest = min(
                SEARCH_SELECTIONS,
                key=lambda key: SEARCH_SELECTIONS[key]["expires"],
            )
            SEARCH_SELECTIONS.pop(oldest, None)
        try:
            await search_status.edit_text(
                search_results_text(selection),
                parse_mode="HTML",
                reply_markup=search_results_keyboard(selection_token, selection),
            )
        except TelegramError:
            SEARCH_SELECTIONS.pop(selection_token, None)
            raise
        return

    cache_video_id = None
    try:
        if recognition_candidate and recognition_candidate.get("id"):
            cache_video_id = youtube.get_video_id(
                str(recognition_candidate["id"])
            )
        elif URL_RE.match(query.strip()):
            cache_video_id = youtube.get_video_id(query)
    except youtube.YouTubeError:
        cache_video_id = None

    cache_variant = (
        f"{TELEGRAM_AUDIO_CACHE_VERSION}:{MP3_QUALITY}:{default_fx}"
    )
    cached_audio = None
    if cache_video_id:
        try:
            cached_audio = await asyncio.to_thread(
                OWNER_CONSOLE.get_cached_audio, cache_video_id, cache_variant
            )
        except Exception as error:
            logger.warning("Telegram audio cache read failed: %s", type(error).__name__)
    if cached_audio:
        try:
            sent = await update.message.reply_audio(
                audio=cached_audio["file_id"],
                title=_no_dashes(cached_audio["title"])[:64],
                performer=_no_dashes(cached_audio.get("performer") or "")[:64] or None,
                duration=cached_audio.get("duration") or None,
            )
        except BadRequest as error:
            message = str(error).lower()
            if "file identifier" not in message and "file_id" not in message:
                raise
            logger.warning(
                "Cached Telegram audio ID was rejected; rebuilding video_id=%s",
                cache_video_id,
            )
            try:
                await asyncio.to_thread(
                    OWNER_CONSOLE.delete_cached_audio, cache_video_id, cache_variant
                )
            except Exception as cache_error:
                logger.warning(
                    "Could not remove rejected Telegram audio ID: %s",
                    type(cache_error).__name__,
                )
        else:
            chat_id = update.effective_chat.id
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id,
                    sent.message_id,
                    reply_markup=song_menu(chat_id, sent.message_id, no_controls),
                )
            except TelegramError as error:
                logger.warning("Could not attach cached song controls: %s", error)
            remember_session(chat_id, sent.message_id, {
                "query": query,
                "video_id": cache_video_id,
                "title": cached_audio["title"],
                "uploader": cached_audio.get("performer"),
                "base_title": cached_audio["base_title"],
                "applied_effect": default_fx,
                "duration": cached_audio.get("duration") or 0,
                "album": None,
                "file_id": sent.audio.file_id if sent.audio else cached_audio["file_id"],
                "cover_bytes": None,
                "original_audio_filename": f"{cache_video_id}.mp3",
                "playlist_only": no_controls,
            })
            logger.info(
                "AUDIO_CACHE_HIT video_id=%s variant=%s", cache_video_id, cache_variant
            )
            return

    status = await update.message.reply_text(
        searching_status_html(query),
        parse_mode="HTML",
    )

    audio_data: bytes | None = None
    thumb_data: bytes | None = None
    try:
        await update.message.chat.send_action(ChatAction.UPLOAD_VOICE)
        # Download happens in a worker thread; result lives purely in RAM.
        # Disk file is already deleted inside download_song_to_ram().
        await status.edit_text(
            downloading_status_html(),
            parse_mode="HTML",
        )
        t_dl = time.perf_counter()
        loop = asyncio.get_running_loop()
        metadata_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

        def on_metadata_ready(track_name: str) -> None:
            def enqueue() -> None:
                if not metadata_queue.full():
                    metadata_queue.put_nowait(track_name)

            loop.call_soon_threadsafe(enqueue)

        download_task = asyncio.create_task(
            asyncio.to_thread(
                download_song_to_ram, query, on_metadata_ready, recognition_candidate
            )
        )
        metadata_task = asyncio.create_task(metadata_queue.get())
        try:
            completed, _ = await asyncio.wait(
                (download_task, metadata_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if metadata_task in completed:
                track_name = metadata_task.result()
                try:
                    await status.edit_text(
                        downloading_status_html(track_name),
                        parse_mode="HTML",
                    )
                    metadata_status_updated = True
                except TelegramError as error:
                    logger.warning("Could not update download title: %s", error)
                    metadata_status_updated = False
            else:
                metadata_status_updated = False
            meta = await download_task
            if recognized_song and recognition.title_match_ratio(
                recognized_song["title"], meta.get("lyrics_title") or meta["title"]
            ) < 0.5:
                raise recognition.RecognitionError(
                    "The available track did not match the recognized title closely enough."
                )
            if not metadata_status_updated:
                title = meta.get("lyrics_title") or meta.get("title")
                artist = meta.get("uploader")
                track_name = f"{title} · {artist}" if title and artist else title
                if track_name:
                    try:
                        await status.edit_text(
                            downloading_status_html(track_name),
                            parse_mode="HTML",
                        )
                    except TelegramError as error:
                        logger.warning("Could not update download title: %s", error)
        finally:
            if not metadata_task.done():
                metadata_task.cancel()
            await asyncio.gather(metadata_task, return_exceptions=True)
        logger.info(
            "FETCH_PIPELINE '%s' took %.2fs; output=%.2fMiB format=%s",
            query, time.perf_counter() - t_dl, meta["size_mb"],
            Path(meta.get("audio_filename", "audio.mp3")).suffix,
        )
        duration = int(meta.get("duration") or 0)
        album = meta.get("album")
        original_audio_data = meta["audio_bytes"]
        original_audio_filename = meta.get(
            "audio_filename", f"{meta['id']}.mp3"
        )
        base_title = meta["title"]
        applied_effect = "off"

        # Default effect from /settings: render before upload.
        if default_fx in EFFECTS:
            try:
                await status.edit_text(
                    rendering_status_html(EFFECTS[default_fx]["label"]),
                    parse_mode="HTML",
                )
                effect_started = time.perf_counter()
                out_bytes = await apply_effect(
                    meta["audio_bytes"], default_fx, meta.get("cover_bytes")
                )
                logger.info(
                    "RENDER '%s' effect=%s took %.2fs",
                    query, default_fx, time.perf_counter() - effect_started,
                )
                meta["audio_bytes"] = out_bytes
                meta["audio_filename"] = f"{meta['id']}.mp3"
                meta["size_mb"] = len(out_bytes) / (1024 * 1024)
                meta["title"] = f"{meta['title']} {EFFECTS[default_fx]['suffix']}"[:64]
                applied_effect = default_fx
            except Exception as e:
                logger.warning("Default effect render failed, sending plain: %s", str(e)[:150])

        await status.edit_text(
            uploading_status_html(meta["title"][:60], meta["size_mb"]),
            parse_mode="HTML",
        )
        await update.message.chat.send_action(ChatAction.UPLOAD_VOICE)

        audio_data = meta["audio_bytes"]
        audio_file = InputFile(
            audio_data,
            filename=meta.get("audio_filename", f"{meta['id']}.mp3"),
        )

        # Upload duration depends on file size, connection speed, and Telegram processing.
        # No caption: curated title/performer + album-art thumbnail card.
        performer = _no_dashes(meta.get("uploader") or "")[:64] or None
        thumb_data = meta.get("cover_bytes")
        thumb_file = InputFile(thumb_data, filename="cover.jpg") if thumb_data else None
        t_up = time.perf_counter()
        telegram_request_seconds = 0.0
        sent = None
        for attempt in (1, 2, 3):
            try:
                request_started = time.perf_counter()
                sent = await update.message.reply_audio(
                    audio=audio_file,
                    title=_no_dashes(meta["title"])[:64],
                    performer=performer,
                    duration=duration or None,
                    thumbnail=thumb_file,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=60,
                    pool_timeout=60,
                )
                telegram_request_seconds += time.perf_counter() - request_started
                break
            except TimedOut:
                telegram_request_seconds += time.perf_counter() - request_started
                if attempt == 3:
                    raise
                wait = 3 * attempt
                logger.warning("Telegram upload timed out (attempt %d/3); retrying in %ds...", attempt, wait)
                await asyncio.sleep(wait)
        upload_stage_seconds = time.perf_counter() - t_up
        upload_mbps = (
            len(audio_data) * 8 / upload_stage_seconds / 1_000_000
            if audio_data and upload_stage_seconds > 0 else 0.0
        )
        logger.info(
            "UPLOAD '%s' request=%.2fs stage=%.2fs size=%.2fMiB effective=%.2fMbps; "
            "request time includes Telegram processing; TOTAL %.2fs",
            query, telegram_request_seconds, upload_stage_seconds,
            meta["size_mb"], upload_mbps, time.perf_counter() - t0,
        )

        chat_id = update.effective_chat.id
        try:
            await context.bot.edit_message_reply_markup(
                chat_id,
                sent.message_id,
                reply_markup=song_menu(chat_id, sent.message_id, no_controls),
            )
        except TelegramError as error:
            logger.warning("Could not attach song controls: %s", error)
        remember_session(chat_id, sent.message_id, {
            "query": query, "video_id": meta["id"],
            "title": meta["title"], "uploader": meta["uploader"],
            "base_title": base_title, "applied_effect": applied_effect,
            "duration": duration, "album": album,
            "file_id": sent.audio.file_id if sent and sent.audio else None,
            "cover_bytes": thumb_data,
            "original_audio_bytes": original_audio_data,
            "original_audio_filename": original_audio_filename,
            "playlist_only": no_controls,
        })
        if sent and sent.audio and sent.audio.file_id:
            stored_variant = (
                f"{TELEGRAM_AUDIO_CACHE_VERSION}:{MP3_QUALITY}:{applied_effect}"
            )
            try:
                await asyncio.to_thread(
                    OWNER_CONSOLE.cache_audio,
                    meta["id"],
                    stored_variant,
                    file_id=sent.audio.file_id,
                    title=meta["title"],
                    base_title=base_title,
                    performer=performer,
                    duration=duration or None,
                )
            except Exception as error:
                logger.warning("Telegram audio cache write failed: %s", type(error).__name__)
        await status.delete()

    except Exception as e:
        logger.exception("Failed to process /song %s", query)
        err_msg = error_html("Download failed", friendly_error(e))
        try:
            await status.edit_text(err_msg, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(err_msg, parse_mode="HTML")
    finally:
        # Release RAM immediately after send (no local files to clean)
        if "meta" in locals() and isinstance(meta, dict):
            meta.pop("audio_bytes", None)
            meta.pop("cover_bytes", None)


def _parse_cb(data: str) -> dict | None:
    # song:<chat_id>:<msg_id>:<group>:<op>
    p = (data or "").split(":")
    if len(p) != 5 or p[0] != "song":
        return None
    try:
        return {"chat_id": int(p[1]), "msg_id": int(p[2]), "group": p[3], "op": p[4]}
    except ValueError:
        return None


async def _safe_answer(query, *args, **kwargs) -> bool:
    """Answer a callback query without crashing on stale or duplicate taps.

    Returns False when the query already expired or was handled elsewhere
    (e.g. a second bot instance answered first). Callers that go on to do
    slow work should abort on False instead of processing the tap twice.
    """
    try:
        await query.answer(*args, **kwargs)
        return True
    except TelegramError as error:
        low = str(error).lower()
        if "query is too old" in low or "query id is invalid" in low:
            logger.warning(
                "Ignoring stale callback query (expired or answered elsewhere)."
            )
            return False
        raise


def _thumb_for(sess: dict):
    """Fresh thumbnail object for edits: custom thumb file_id wins,
    else original cover bytes, else None."""
    if sess.get("thumb_file_id"):
        return sess["thumb_file_id"]
    if sess.get("cover_bytes"):
        buf = io.BytesIO(sess["cover_bytes"])
        buf.name = "cover.jpg"
        return buf
    return None


def _recognition_media(message) -> dict | None:
    media = (
        getattr(message, "audio", None)
        or getattr(message, "voice", None)
        or getattr(message, "video", None)
        or getattr(message, "video_note", None)
        or getattr(message, "document", None)
    )
    if media:
        mime_type = getattr(media, "mime_type", None)
        filename = getattr(media, "file_name", None)
        if recognition.is_audio_or_video(mime_type, filename):
            return {
                "kind": "file",
                "file_id": media.file_id,
                "file_size": media.file_size,
                "filename": filename or "upload",
            }

    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    match = re.search(r"https?://\S+", text, flags=re.IGNORECASE)
    if match:
        url = match.group(0).rstrip(".,!?;:)]}>")
        if recognition.validate_media_url(url):
            return {"kind": "url", "url": url}
        return {"kind": "unsupported_url"}
    return None


def _store_recognition_request(
    source: dict,
    user_id: int,
    chat_id: int,
    message_id: int,
    token: str | None = None,
) -> str:
    now = time.time()
    for old_token, request in list(RECOGNITION_REQUESTS.items()):
        if request["expires"] <= now:
            RECOGNITION_REQUESTS.pop(old_token, None)
    while len(RECOGNITION_REQUESTS) >= MAX_RECOGNITION_REQUESTS:
        oldest = min(
            RECOGNITION_REQUESTS,
            key=lambda key: RECOGNITION_REQUESTS[key]["created"],
        )
        RECOGNITION_REQUESTS.pop(oldest, None)

    token = token or os.urandom(9).hex()
    RECOGNITION_REQUESTS[token] = {
        **source,
        "requester_id": user_id,
        "created": now,
        "expires": now + RECOGNITION_REQUEST_TTL,
        "chat_id": chat_id,
        "prompt_message_id": message_id,
        "processing": False,
    }
    return token


async def receive_recognition_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    media_url: str | None = None,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    pending = PENDING.get(user.id)
    if pending and pending.get("expires", 0) >= time.time():
        return

    source = (
        {"kind": "url", "url": media_url}
        if media_url and recognition.validate_media_url(media_url)
        else {"kind": "unsupported_url"}
        if media_url
        else _recognition_media(message)
    )
    if not source:
        return
    if source["kind"] == "url" and recognition.is_music_url(source["url"]):
        delivery_update = SimpleNamespace(
            message=message,
            effective_user=user,
            effective_chat=chat,
        )
        delivery_context = SimpleNamespace(
            args=[source["url"]],
            bot=context.bot,
            application=getattr(context, "application", None),
        )
        await song(delivery_update, delivery_context)
        return
    if source["kind"] == "unsupported_url":
        await message.reply_text(
            warn_html(
                "Unsupported link",
                "Send a public YouTube video link.",
            ),
            parse_mode="HTML",
        )
        return

    if source["kind"] == "url":
        work_dir = None
        status = None
        try:
            try:
                status = await message.reply_text(
                    status_html(
                        CUSTOM_EMOJI_IDS["streaming"], "🎬", "Fetching Videos"
                    ),
                    parse_mode="HTML",
                )
            except TelegramError:
                status = None
            work_dir = Path(
                await asyncio.to_thread(tempfile.mkdtemp, prefix="emy_preview_")
            )
            preview_path, info = await recognition.download_url_preview(
                source["url"], work_dir
            )
            preview_bytes = await asyncio.to_thread(preview_path.read_bytes)
            token = os.urandom(9).hex()
            video = await message.reply_video(
                video=InputFile(
                    preview_bytes,
                    filename=f"{info.get('id') or 'video'}_720p.mp4",
                ),
                reply_markup=inline_keyboard([[
                    custom_button(
                        "Recognize Song",
                        f"recognize:{token}",
                        icon_custom_emoji_id=CUSTOM_EMOJI_IDS["music"],
                    )
                ]]),
                supports_streaming=True,
                read_timeout=60,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60,
            )
            video_media = getattr(video, "video", None) or getattr(video, "document", None)
            if not video_media:
                raise RuntimeError("Telegram did not return the uploaded video file.")
            token = _store_recognition_request(
                {
                    "kind": "file",
                    "preview_video": True,
                    "file_id": video_media.file_id,
                    "file_size": len(preview_bytes),
                    "filename": f"{info.get('id') or 'video'}.mp4",
                },
                user.id,
                chat.id,
                video.message_id,
                token=token,
            )
        except recognition.RecognitionError as error:
            await message.reply_text(
                error_html("Preview failed", str(error)), parse_mode="HTML"
            )
        except TelegramError as error:
            logger.warning("Could not send URL video preview: %s", error)
            await message.reply_text(
                error_html(
                    "Preview failed",
                    "I couldn't send the video preview. "
                    "Try a shorter public link.",
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("URL video preview failed for %r", source["url"])
            await message.reply_text(
                error_html(
                    "Preview failed",
                    "I couldn't prepare that preview. Try a public video link.",
                ),
                parse_mode="HTML",
            )
        finally:
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass
            if work_dir:
                await asyncio.to_thread(shutil.rmtree, work_dir, True)
        return

    token = os.urandom(9).hex()
    button = custom_button(
        "Recognize Song",
        f"recognize:{token}",
        icon_custom_emoji_id=CUSTOM_EMOJI_IDS["music"],
    )
    prompt = await message.reply_text(
        "Tap below when you are ready to identify the music.",
        reply_markup=inline_keyboard([[button]]),
    )
    _store_recognition_request(
        source, user.id, chat.id, prompt.message_id, token=token
    )


async def _recognize_song_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = (query.data or "").partition(":")[2]
    request = RECOGNITION_REQUESTS.get(token)
    if not request or request["expires"] <= time.time():
        RECOGNITION_REQUESTS.pop(token, None)
        await _safe_answer(query, "This recognition request has expired.", show_alert=True)
        return
    if not query.from_user or query.from_user.id != request["requester_id"]:
        await _safe_answer(query, "This recognition request belongs to another user.", show_alert=True)
        return
    if request["processing"]:
        await _safe_answer(query, "Recognition is already in progress.")
        return

    request["processing"] = True
    work_dir = None
    recognition_status = None
    try:
        if not await _safe_answer(query):
            request["processing"] = False
            return
        if request.get("preview_video"):
            recognition_status = await context.bot.send_message(
                request["chat_id"],
                recognition_status_html(),
                parse_mode="HTML",
                reply_to_message_id=query.message.message_id,
            )
        else:
            recognition_status = query.message
            await recognition_status.edit_text(
                recognition_status_html(),
                parse_mode="HTML",
            )
        work_dir = Path(
            await asyncio.to_thread(tempfile.mkdtemp, prefix="emy_recognition_")
        )
        if request["kind"] == "url":
            sample_path = await recognition.download_url_sample(
                request["url"], work_dir
            )
        else:
            if request.get("file_size") and request["file_size"] > recognition.MAX_RECOGNITION_FILE_BYTES:
                raise recognition.RecognitionError(
                    "This upload is over the 20 MB recognition limit."
                )
            source_path = work_dir / Path(request["filename"]).name
            try:
                media_file = await context.bot.get_file(request["file_id"])
                if media_file.file_size and media_file.file_size > recognition.MAX_RECOGNITION_FILE_BYTES:
                    raise recognition.RecognitionError(
                        "This upload is over the 20 MB recognition limit."
                    )
                await media_file.download_to_drive(custom_path=str(source_path))
            except TelegramError as error:
                raise recognition.RecognitionError(
                    "Telegram could not retrieve this upload. Please send the file again."
                ) from error
            if source_path.stat().st_size > recognition.MAX_RECOGNITION_FILE_BYTES:
                raise recognition.RecognitionError(
                    "This upload is over the 20 MB recognition limit."
                )
            sample_path = work_dir / "sample.mp3"
            await recognition.extract_uploaded_sample(source_path, sample_path)

        match = await recognition.recognize_sample(
            sample_path
        )
        if not match:
            await recognition_status.edit_text(
                warn_html("No match found", "Try a longer or clearer clip."),
                parse_mode="HTML",
                reply_markup=(
                    None if request.get("preview_video") else inline_keyboard([[
                        custom_button("Recognize Song", f"recognize:{token}"),
                    ]])
                ),
            )
            request["processing"] = False
            return

        recognized_query = f"{match['title']} by {match['artist']}"
        candidate = await resolve_recognition_candidate_async(
            match, context.application
        )
        if not candidate or not candidate.get("id"):
            raise recognition.RecognitionError(
                "A close catalog match could not be found. "
                "Try recognizing a clearer or longer clip."
            )

        if request.get("preview_video") and recognition_status:
            try:
                await recognition_status.delete()
            except TelegramError as error:
                logger.warning(
                    "Could not remove recognition status before delivery: %s",
                    error,
                )
        elif recognition_status:
            try:
                await recognition_status.edit_text(
                    status_html(MUSIC_ICON, "🎵", "Match found, sending…"),
                    parse_mode="HTML",
                )
            except TelegramError as error:
                logger.warning(
                    "Could not update recognition status before delivery: %s",
                    error,
                )
        delivery_update = SimpleNamespace(
            message=query.message,
            effective_user=query.from_user,
            effective_chat=query.message.chat,
        )
        delivery_context = SimpleNamespace(
            args=[recognized_query],
            bot=context.bot,
            recognition_candidate=candidate,
            recognized_song=match,
        )
        await song(delivery_update, delivery_context)
        RECOGNITION_REQUESTS.pop(token, None)
    except recognition.RecognitionError as error:
        if recognition_status:
            await recognition_status.edit_text(
                error_html("Couldn't recognize that", str(error)),
                parse_mode="HTML",
                reply_markup=(
                    None if request.get("preview_video") else inline_keyboard([[
                        custom_button("Recognize Song", f"recognize:{token}"),
                    ]])
                ),
            )
        else:
            logger.warning("Could not start song recognition: %s", error)
        request["processing"] = False
    except Exception:
        logger.exception("Song recognition failed for requester %s", request["requester_id"])
        if recognition_status:
            await recognition_status.edit_text(
                error_html("Recognition failed", "Please try again."),
                parse_mode="HTML",
                reply_markup=(
                    None if request.get("preview_video") else inline_keyboard([[
                        custom_button("Recognize Song", f"recognize:{token}"),
                    ]])
                ),
            )
        request["processing"] = False
    finally:
        if work_dir:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
        if RECOGNITION_REQUESTS.get(token) is request:
            request["processing"] = False


async def _search_pick_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await _safe_answer(query, "Invalid search result.", show_alert=True)
        return
    _, token, index_text = parts
    selection = SEARCH_SELECTIONS.get(token)
    if not selection or selection["expires"] <= time.time():
        SEARCH_SELECTIONS.pop(token, None)
        await _safe_answer(query, "These search results have expired.", show_alert=True)
        return
    if not query.from_user or query.from_user.id != selection["requester_id"]:
        await _safe_answer(query, "These search results belong to another user.", show_alert=True)
        return
    if selection["processing"]:
        await _safe_answer(query, "That track is already being prepared.")
        return
    try:
        index = int(index_text)
        candidate = selection["candidates"][index]
    except (ValueError, IndexError):
        await _safe_answer(query, "That track is no longer available.", show_alert=True)
        return

    selection["processing"] = True
    if not await _safe_answer(query):
        selection["processing"] = False
        return
    delivery_update = SimpleNamespace(
        message=query.message,
        effective_user=query.from_user,
        effective_chat=query.message.chat,
    )
    delivery_context = SimpleNamespace(
        args=[selection["query"]],
        bot=context.bot,
        recognition_candidate=candidate,
    )
    try:
        try:
            await query.edit_message_text(
                status_html(MUSIC_ICON, "🎵", "Preparing…"),
                parse_mode="HTML",
            )
        except TelegramError as error:
            logger.warning("Could not update selected song prompt: %s", error)
        await song(delivery_update, delivery_context)
    finally:
        SEARCH_SELECTIONS.pop(token, None)


async def _search_navigation_callback(query) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 2 and not (
        len(parts) == 3 and parts[0] == "searchpage"
    ):
        await _safe_answer(query, "Invalid search control.", show_alert=True)
        return

    action = parts[0]
    token = parts[1]
    selection = SEARCH_SELECTIONS.get(token)
    if not selection or selection["expires"] <= time.time():
        SEARCH_SELECTIONS.pop(token, None)
        await _safe_answer(query, "These search results have expired.", show_alert=True)
        return
    if not query.from_user or query.from_user.id != selection["requester_id"]:
        await _safe_answer(query, "These search results belong to another user.", show_alert=True)
        return
    if selection["processing"]:
        await _safe_answer(query, "A selected track is already being prepared.")
        return

    if action == "searchclose":
        try:
            await query.delete_message()
        except TelegramError as error:
            logger.warning("Could not close song search results: %s", error)
            await _safe_answer(query, "Could not close these results.", show_alert=True)
            return
        SEARCH_SELECTIONS.pop(token, None)
        await _safe_answer(query)
        return

    try:
        page = int(parts[2])
    except (IndexError, ValueError):
        await _safe_answer(query, "Invalid results page.", show_alert=True)
        return
    page_count = max(
        1, (len(selection["candidates"]) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
    )
    if page < 0 or page >= page_count:
        await _safe_answer(
            query,
            "You're already at the first page."
            if page < 0 else "You're already at the last page."
        )
        return

    selection["page"] = page
    try:
        await query.edit_message_text(
            search_results_text(selection),
            parse_mode="HTML",
            reply_markup=search_results_keyboard(token, selection),
        )
    except TelegramError as error:
        logger.warning("Could not display song search page %d: %s", page + 1, error)
        await _safe_answer(query, "Could not display that results page.", show_alert=True)
        return
    await _safe_answer(query)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data if q else ""
    if data.startswith("playlist:"):
        await _playlist_callback(q, context)
        return
    if data.startswith("searchpick:"):
        await _search_pick_callback(q, context)
        return
    if data.startswith("searchpage:") or data.startswith("searchclose:"):
        await _search_navigation_callback(q)
        return
    if data.startswith("recognize:"):
        await _recognize_song_callback(q, context)
        return
    if data.startswith("settings:"):
        ephemeral_target = ephemeral_message_target(q.message if q else None)
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if ephemeral_target:
            chat_id, receiver_user_id, ephemeral_message_id = ephemeral_target
            if not q.from_user or q.from_user.id != receiver_user_id:
                await _safe_answer(q, "This private response is for another user.", show_alert=True)
                return
            await _safe_answer(q)
            if action == "close":
                await call_api(
                    context.bot, "deleteEphemeralMessage", chat_id=chat_id,
                    receiver_user_id=receiver_user_id,
                    ephemeral_message_id=ephemeral_message_id,
                )
                return
            if action == "fx" and len(parts) > 2 and parts[2] in ("off", *EFFECTS):
                await asyncio.to_thread(set_default_effect, receiver_user_id, parts[2])
                text, mode = settings_text(parts[2])
                await call_api(
                    context.bot, "editEphemeralMessageText",
                    chat_id=chat_id, receiver_user_id=receiver_user_id,
                    ephemeral_message_id=ephemeral_message_id,
                    text=text, parse_mode=mode,
                    reply_markup=settings_menu(parts[2]),
                )
            return
        await _safe_answer(q)
        if action == "close":
            try:
                await q.delete_message()
            except Exception:
                pass
            return
        if action == "fx" and len(parts) > 2 and parts[2] in ("off", *EFFECTS):
            uid = q.from_user.id if q.from_user else None
            if uid is not None:
                set_default_effect(uid, parts[2])
            text, mode = settings_text(parts[2])
            try:
                await q.edit_message_text(text, parse_mode=mode, reply_markup=settings_menu(parts[2]))
            except Exception:
                pass
        return
    parsed = _parse_cb(data)
    if not parsed:
        if q:
            await _safe_answer(q)
        return
    chat_id, msg_id, group, op = parsed["chat_id"], parsed["msg_id"], parsed["group"], parsed["op"]
    if group == "menu" and op == "playlist":
        await _show_playlist_picker(q)
        return
    await _safe_answer(q)
    sess = SESSIONS.get((chat_id, msg_id))

    if group == "menu" and op == "edit":
        await q.edit_message_reply_markup(reply_markup=edit_menu(chat_id, msg_id))
    elif group == "menu" and op == "fx":
        active_effect = (
            _session_effect(sess) if sess else "off"
        )
        await q.edit_message_reply_markup(
            reply_markup=fx_menu(chat_id, msg_id, active_effect)
        )
    elif group == "menu" and op == "lyrics":
        if not sess:
            await context.bot.send_message(
                chat_id,
                warn_html(
                    "Session expired",
                    "Send the song again to search its lyrics.",
                ),
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )
            return
        title = (sess.get("lyrics_title") or sess.get("title") or "").strip()
        for effect in EFFECTS.values():
            suffix = effect["suffix"]
            if suffix and title.endswith(suffix):
                title = title[:-len(suffix)].strip()
                break
        title = re.sub(r"^\s*Audio\s*[•·-]\s*", "", title, flags=re.IGNORECASE)
        if not title:
            await context.bot.send_message(
                chat_id,
                warn_html(
                    "No title found",
                    "I couldn't identify this song's title for a lyrics lookup.",
                ),
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )
            return
        try:
            lyrics_session = context.application.bot_data.get("lyrics_http_session")
            result = await lrclib.find_lyrics(
                lyrics_session,
                title,
                artist=sess.get("uploader") or "",
                album=sess.get("album"),
                duration=sess.get("duration"),
            )
            if not result:
                await context.bot.send_message(
                    chat_id,
                    warn_html("No lyrics found", title),
                    parse_mode="HTML",
                    reply_to_message_id=msg_id,
                )
                return
            chunks = [
                _esc(chunk)
                for chunk in lrclib.split_lyrics(result["lyrics"])
            ]
            heading = _esc(result["title"])
            if result["artist"]:
                heading += f" · {_esc(result['artist'])}"
            chunks[0] = f"<b>{heading}</b>\n\n{chunks[0]}"
            for chunk in chunks:
                await context.bot.send_message(
                    chat_id, chunk,
                    parse_mode="HTML",
                    reply_to_message_id=msg_id,
                )
        except (RuntimeError, TelegramError) as error:
            logger.warning("LRCLIB lookup failed for title %r: %s", title, error)
            await context.bot.send_message(
                chat_id,
                warn_html("Lyrics unavailable", "Please try again later."),
                parse_mode="HTML",
                reply_to_message_id=msg_id,
            )
    elif group == "menu" and op == "delete":
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            SESSIONS.pop((chat_id, msg_id), None)
            if (chat_id, msg_id) in SESSION_ORDER:
                SESSION_ORDER.remove((chat_id, msg_id))
            for uid, p in list(PENDING.items()):
                if p.get("chat_id") == chat_id and p.get("msg_id") == msg_id:
                    PENDING.pop(uid, None)
            await _safe_answer(q, "Song deleted.")
        except Exception as e:
            logger.warning("Could not delete message %s in chat %s: %s", msg_id, chat_id, e)
            try:
                await q.delete_message()
                await _safe_answer(q, "Song deleted.")
            except Exception:
                await _safe_answer(q, "Could not delete the song.", show_alert=True)
        return
    elif group == "back":
        await q.edit_message_reply_markup(reply_markup=main_menu(chat_id, msg_id))
    elif group == "edit":
        if not sess or not sess.get("file_id"):
            await context.bot.send_message(
                chat_id,
                warn_html("Session expired", "Send the song again."),
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            return
        if op in ("name", "artist"):
            PENDING[q.from_user.id] = {"op": op, "chat_id": chat_id, "msg_id": msg_id, "expires": time.time() + PENDING_TTL}
            what = "song name" if op == "name" else "artist name"
            await context.bot.send_message(
                chat_id, f"Send the new {what} (or /cancel):", reply_to_message_id=msg_id
            )
        elif op == "thumb":
            PENDING[q.from_user.id] = {"op": "thumb", "chat_id": chat_id, "msg_id": msg_id, "expires": time.time() + PENDING_TTL}
            await context.bot.send_message(
                chat_id, "Send a photo for the cover (or /cancel):", reply_to_message_id=msg_id
            )
    elif group == "fx":
        if op == "off":
            spec = {"label": "Original", "suffix": ""}
        else:
            spec = EFFECTS.get(op)
        if not spec:
            return
        if not sess:
            await context.bot.send_message(
                chat_id,
                warn_html("Session expired", "Send the song again to use effects."),
                reply_to_message_id=msg_id,
                parse_mode="HTML",
            )
            return
        await run_effect(update, context, chat_id, msg_id, sess, op, spec)


async def run_effect(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, msg_id: int, sess: dict, effect: str, spec: dict) -> None:
    status = await context.bot.send_message(
        chat_id,
        rendering_status_html(spec["label"])
        if effect != "off" else status_html(
            CUSTOM_EMOJI_IDS["effect_off"], "🎵", "Restoring original…"
        ),
        reply_to_message_id=msg_id,
        parse_mode="HTML",
    )
    try:
        base_title = _base_song_title(sess)
        new_title = (
            f"{base_title} {EFFECTS[effect]['suffix']}"[:64]
            if effect in EFFECTS else base_title[:64]
        )
        card_title = _no_dashes(new_title)
        card_performer = _no_dashes(sess.get("uploader") or "") or None
        cover_bytes = sess.get("cover_bytes")
        original_audio = await _original_audio_bytes(sess)
        if effect == "off":
            out_bytes = original_audio
        else:
            effect_started = time.perf_counter()
            out_bytes = await apply_effect(original_audio, effect, cover_bytes)
            logger.info(
                "RENDER video_id=%s effect=%s took %.2fs",
                sess.get("video_id"), effect,
                time.perf_counter() - effect_started,
            )
        media = InputMediaAudio(
            media=InputFile(
                out_bytes,
                filename=(
                    sess.get("original_audio_filename", "song.mp3")
                    if effect == "off"
                    else f"{sess.get('video_id', 'song')}_{effect}.mp3"
                ),
                attach=True,
            ),
            title=card_title,
            performer=card_performer,
            duration=sess.get("duration") or None,
            thumbnail=(
                InputFile(cover_bytes, filename="cover.jpg", attach=True)
                if cover_bytes else None
            ),
        )

        sent = await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=msg_id,
            media=media,
            reply_markup=fx_menu(chat_id, msg_id, effect),
            read_timeout=180,
            write_timeout=180,
            connect_timeout=60,
            pool_timeout=60,
        )
        new_file_id = sent.audio.file_id if sent and sent.audio else None
        if not new_file_id:
            raise RuntimeError("Telegram did not return the updated audio file.")
        updated = {
            **sess,
            "base_title": base_title,
            "title": new_title,
            "applied_effect": effect,
            "file_id": new_file_id,
        }
        remember_session(chat_id, msg_id, updated)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.exception("Effect %s failed", effect)
        try:
            await status.edit_text(
                error_html("Effect failed", friendly_error(e)),
                parse_mode="HTML",
            )
        except Exception:
            pass


def _session_effect(sess: dict) -> str:
    effect = sess.get("applied_effect")
    if effect == "off" or effect in EFFECTS:
        return effect
    title = str(sess.get("title") or "")
    for name, spec in EFFECTS.items():
        if spec["suffix"] and title.endswith(spec["suffix"]):
            return name
    return "off"


def _base_song_title(sess: dict) -> str:
    base_title = str(sess.get("base_title") or sess.get("title") or "Song")
    effect = _session_effect(sess)
    suffix = EFFECTS.get(effect, {}).get("suffix", "")
    if suffix and base_title.endswith(suffix):
        base_title = base_title[:-len(suffix)].rstrip()
    return base_title


async def _original_audio_bytes(sess: dict) -> bytes:
    audio_bytes = sess.get("original_audio_bytes")
    if audio_bytes:
        return audio_bytes
    if _session_effect(sess) == "off" and sess.get("audio_bytes"):
        audio_bytes = sess["audio_bytes"]
        sess["original_audio_bytes"] = audio_bytes
        return audio_bytes

    video_id = sess.get("video_id")
    if not video_id:
        raise RuntimeError("The original audio is no longer available for this edit.")
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    meta = await asyncio.to_thread(download_song_to_ram, source_url)
    audio_bytes = meta["audio_bytes"]
    sess["original_audio_bytes"] = audio_bytes
    sess["original_audio_filename"] = meta.get(
        "audio_filename", f"{video_id}.mp3"
    )
    sess.setdefault("base_title", meta.get("title") or _base_song_title(sess))
    if meta.get("cover_bytes") and not sess.get("cover_bytes"):
        sess["cover_bytes"] = meta["cover_bytes"]
    return audio_bytes


def _convert_thumbnail_jpeg(image_data: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="emy_thumb_") as tmpdir:
        source = Path(tmpdir) / "source"
        target = Path(tmpdir) / "thumbnail.jpg"
        source.write_bytes(image_data)
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source),
                "-frames:v", "1",
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                "-q:v", "5", str(target),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode or not target.is_file():
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Could not prepare the new cover: {detail[:200]}")
        thumbnail = target.read_bytes()
        if len(thumbnail) > 200 * 1024:
            raise ValueError("The prepared cover exceeds Telegram's 200 KB thumbnail limit.")
        return thumbnail


async def _edited_audio_bytes(
    sess: dict, cover_bytes: bytes | None = None
) -> bytes:
    original = await _original_audio_bytes(sess)
    effect = _session_effect(sess)
    if effect == "off":
        return original
    return await apply_effect(
        original, effect, cover_bytes or sess.get("cover_bytes")
    )


async def _send_edited_song(
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict,
    sess: dict,
    *,
    title: str,
    artist: str | None,
    cover_bytes: bytes | None = None,
) -> None:
    chat_id = pending["chat_id"]
    old_message_id = pending["msg_id"]
    effect = _session_effect(sess)
    base_title = str(title or "Song")
    if effect in EFFECTS:
        suffix = EFFECTS[effect]["suffix"]
        if suffix and base_title.endswith(suffix):
            base_title = base_title[:-len(suffix)].rstrip()
    visible_title = (
        f"{base_title} {EFFECTS[effect]['suffix']}"[:64]
        if effect in EFFECTS else base_title[:64]
    )
    audio_bytes = await _edited_audio_bytes(
        {**sess, "applied_effect": effect, "base_title": base_title},
        cover_bytes,
    )
    media_source = InputFile(
        audio_bytes,
        filename=(
            sess.get("original_audio_filename", "song.mp3")
            if effect == "off"
            else f"{sess.get('video_id') or 'song'}_edited.mp3"
        ),
        attach=True,
    )
    thumbnail = (
        InputFile(cover_bytes, filename="cover.jpg", attach=True)
        if cover_bytes else None
    )
    media = InputMediaAudio(
        media=media_source,
        title=_no_dashes(visible_title),
        performer=_no_dashes(artist) if artist else None,
        duration=sess.get("duration") or None,
        thumbnail=thumbnail,
    )
    sent = await context.bot.edit_message_media(
        chat_id=chat_id,
        message_id=old_message_id,
        media=media,
        reply_markup=main_menu(chat_id, old_message_id),
        read_timeout=180,
        write_timeout=180,
        connect_timeout=60,
        pool_timeout=60,
    )
    new_file_id = sent.audio.file_id if sent.audio else sess["file_id"]
    updated = {
        **sess,
        "title": visible_title,
        "base_title": base_title,
        "uploader": artist,
        "file_id": new_file_id,
        "original_audio_bytes": (
            sess.get("original_audio_bytes")
            or (audio_bytes if effect == "off" else None)
        ),
        "cover_bytes": cover_bytes or sess.get("cover_bytes"),
        "applied_effect": effect,
    }
    if cover_bytes:
        updated["thumb_file_id"] = None
    remember_session(chat_id, old_message_id, updated)


async def on_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    p = PENDING.get(uid) if uid is not None else None
    if not p or p["expires"] < time.time() or p["op"] not in ("name", "artist"):
        return
    if update.effective_chat.id != p["chat_id"]:
        return
    message = update.effective_message
    if message is None:
        return
    new_val = (message.text or "").strip()[:64]
    if not new_val:
        return
    PENDING.pop(uid, None)
    sess = SESSIONS.get((p["chat_id"], p["msg_id"]))
    if not sess or not sess.get("file_id"):
        await message.reply_text(
            warn_html("Session expired", "Send the song again."),
            parse_mode="HTML",
        )
        return
    try:
        title = new_val if p["op"] == "name" else _base_song_title(sess)
        performer = new_val if p["op"] == "artist" else sess.get("uploader")
        await _send_edited_song(
            context, p, sess, title=title, artist=performer
        )
    except Exception as e:
        logger.exception("Edit failed")
        await message.reply_text(
            error_html("Edit failed", friendly_error(e)),
            parse_mode="HTML",
        )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = getattr(update.effective_message, "text", "") or ""
    if re.search(r"https?://", text, flags=re.IGNORECASE):
        await receive_recognition_input(update, context)
        return
    await song(update, context)


async def on_direct_private_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if getattr(user, "is_bot", False) or (
        context.bot and getattr(context.bot, "id", None) == user.id
    ):
        return

    # Pending edit replies count in every chat type. Without this, answering
    # the "Send the new name" prompt in a group is silently ignored.
    pending = PENDING.get(user.id)
    if (
        pending
        and pending.get("expires", 0) >= time.time()
        and pending.get("op") in ("name", "artist")
        and pending.get("chat_id") == chat.id
    ):
        await on_pending_text(update, context)
        return

    if chat.type != "private":
        return

    text = (message.text or "").strip()
    if (
        not text
        or text.startswith("/")
        or re.search(r"https?://", text, flags=re.IGNORECASE)
    ):
        return
    search_context = SimpleNamespace(
        args=[text],
        bot=context.bot,
        application=context.application,
    )
    await song(update, search_context)


async def on_pending_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    p = PENDING.get(uid) if uid is not None else None
    if not p or p["expires"] < time.time() or p["op"] != "thumb":
        return
    chat = update.effective_chat
    message = update.effective_message
    if (
        not chat
        or not message
        or chat.id != p["chat_id"]
        or not getattr(message, "photo", None)
    ):
        return
    PENDING.pop(uid, None)
    sess = SESSIONS.get((p["chat_id"], p["msg_id"]))
    if not sess or not sess.get("file_id"):
        await message.reply_text(
            warn_html("Session expired", "Send the song again."),
            parse_mode="HTML",
        )
        return
    try:
        cover_file = await context.bot.get_file(message.photo[-1].file_id)
        cover_data = bytes(await cover_file.download_as_bytearray())
        cover_jpeg = await asyncio.to_thread(
            _convert_thumbnail_jpeg, cover_data
        )
        await _send_edited_song(
            context,
            p,
            sess,
            title=sess.get("title") or "Song",
            artist=sess.get("uploader"),
            cover_bytes=cover_jpeg,
        )
    except Exception as e:
        logger.exception("Thumbnail edit failed")
        await message.reply_text(
            error_html("Cover update failed", friendly_error(e)),
            parse_mode="HTML",
        )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if uid is not None and PENDING.pop(uid, None):
        await update.message.reply_text("Cancelled.")
    elif uid is not None and uid in PENDING_PLAYLIST_CREATE:
        pending = PENDING_PLAYLIST_CREATE.pop(uid)
        if pending:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=pending["chat_id"],
                    message_id=pending["message_id"],
                    reply_markup=song_menu(
                        pending["chat_id"],
                        pending["message_id"],
                        pending.get("playlist_only", False),
                    ),
                )
            except TelegramError as error:
                logger.warning("Could not restore song controls after cancellation: %s", error)
        await update.message.reply_text("Playlist creation cancelled.")
    elif OWNER_CONSOLE.is_owner(uid):
        await OWNER_CONSOLE.cancel_input(update, context)
    else:
        await update.message.reply_text(
            warn_html("Nothing to cancel"),
            parse_mode="HTML",
        )


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preview YouTube videos; play YouTube Music links as audio."""
    if not context.args:
        await update.message.reply_text(
            status_html(
                SEARCH_ICON, "🔍", "How to play:",
                "<code>/play link or video id</code>, music links work too.",
            ),
            parse_mode="HTML",
        )
        return
    args = list(context.args)
    no_controls = args[-1] == "--no-controls"
    if no_controls:
        args.pop()
    if not args:
        await update.message.reply_text(
            warn_html("Nothing to play", "Send a link or a video id after /play."),
            parse_mode="HTML",
        )
        return
    target = args[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", target):
        target = f"https://www.youtube.com/watch?v={target}"
    if not URL_RE.match(target):
        await update.message.reply_text(
            warn_html("Nothing to play", "Send a link or a video id after /play."),
            parse_mode="HTML",
        )
        return
    if (
        not no_controls
        and recognition.validate_media_url(target)
        and not recognition.is_music_url(target)
    ):
        await receive_recognition_input(update, context, media_url=target)
        return
    delivery_update = SimpleNamespace(
        message=update.effective_message,
        effective_user=update.effective_user,
        effective_chat=update.effective_chat,
    )
    delivery_context = SimpleNamespace(
        args=[target],
        no_controls=no_controls,
        bot=context.bot,
        application=getattr(context, "application", None),
    )
    await song(delivery_update, delivery_context)


# --- Per-user settings: default song effect (off or any FFmpeg preset) ---
USER_SETTINGS_FILE = Path(os.getenv("USER_SETTINGS_FILE", "user.json"))
LEGACY_USER_SETTINGS_FILE = USER_SETTINGS_FILE.with_name("user_settings.json")
USER_SETTINGS: dict = {}
settings_source = (
    USER_SETTINGS_FILE
    if USER_SETTINGS_FILE.exists()
    else LEGACY_USER_SETTINGS_FILE
)
if settings_source.exists():
    try:
        USER_SETTINGS = json.loads(settings_source.read_text(encoding="utf-8"))
    except Exception:
        USER_SETTINGS = {}
    else:
        if settings_source != USER_SETTINGS_FILE:
            try:
                settings_source.replace(USER_SETTINGS_FILE)
            except OSError as error:
                logging.getLogger(__name__).warning(
                    "Could not migrate user settings to %s: %s",
                    USER_SETTINGS_FILE,
                    error,
                )


def get_default_effect(user_id: int | None) -> str:
    if user_id is None:
        return "off"
    fx = USER_SETTINGS.get(str(user_id), {}).get("effect", "off")
    return fx if fx in EFFECTS else "off"


def set_default_effect(user_id: int, effect: str) -> None:
    USER_SETTINGS[str(user_id)] = {"effect": effect}
    try:
        USER_SETTINGS_FILE.write_text(json.dumps(USER_SETTINGS, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def settings_menu(current: str) -> dict:
    def label(key: str, name: str) -> str:
        return name

    def button(key: str, name: str) -> dict:
        return custom_button(
            label(key, name), f"settings:fx:{key}",
            icon_custom_emoji_id=(
                CUSTOM_EMOJI_IDS["effect_off"]
                if key == "off" else CUSTOM_EMOJI_IDS["audio_effect"]
            ),
            style="success" if key == current else "primary",
        )

    return inline_keyboard([
        [
            button("off", "Off"),
            button("slowed", "Slowed"),
        ],
        [
            button("8d", "8D"),
            button("concert", "Concert"),
        ],
        [
            button("vinyl", "Vinyl"),
            button("lofi", "LoFi"),
        ],
        [
            button("360", "360°"),
            button("3d", "3D"),
        ],
        [custom_button("Back", "settings:close:x",
                       icon_custom_emoji_id=CUSTOM_EMOJI_IDS["back"],
                       style="danger")],
    ])


def settings_text(current: str) -> tuple:
    if current == "off":
        status = "no effects applied"
    else:
        effect_name = {
            "concert": "Concert Hall",
            "lofi": "LoFi",
        }.get(current, EFFECTS[current]["label"])
        status = f"current effect: <b>{effect_name}</b>"
    return (
        f'<tg-emoji emoji-id="{SETTINGS_ICON}">⚙️</tg-emoji> <b>Settings</b> · {status}\n'
        "Tap an effect below: Slowed (slower), 8D (spatial), 360° (orbit), "
        "3D (gentle depth), Concert (live), Vinyl (classic), LoFi (mellow).",
        "HTML",
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    current = get_default_effect(uid)
    text, mode = settings_text(current)
    chat = update.effective_chat
    if chat and uid is not None and chat.type in ("group", "supergroup"):
        await send_ephemeral_message(
            context.bot, chat.id, uid, text, parse_mode=mode,
            reply_markup=settings_menu(current),
        )
    elif update.message:
        await update.message.reply_text(text, parse_mode=mode, reply_markup=settings_menu(current))
    elif chat:
        await context.bot.send_message(
            chat.id, text, parse_mode=mode, reply_markup=settings_menu(current)
        )


BOT_COMMANDS = [
    BotCommand("start", "Welcome to Emy"),
    BotCommand("search", "Find and play a song"),
    BotCommand("play", "Play a link or video id directly"),
    BotCommand("playlist", "Create and manage your playlists"),
    BotCommand("settings", "Choose an audio effect"),
    BotCommand("cancel", "Cancel the current input"),
    BotCommand("help", "Show available commands"),
]

OWNER_COMMANDS = [
    BotCommand("owner", "Open the owner console"),
    BotCommand("ping", "Measure bot API latency"),
    BotCommand("stats", "Show detailed bot statistics"),
    BotCommand("apikey", "Set the private TypeTube API key"),
    BotCommand("broadcast", "Create a managed broadcast"),
    BotCommand("broadcast_status", "Review recent broadcasts"),
    BotCommand("broadcast_cancel", "Cancel a running broadcast"),
    BotCommand("broadcast_resume", "Retry a broadcast"),
]


async def ping_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if (
        not message
        or not user
        or not chat
        or not OWNER_CONSOLE.owner_id
        or user.id != OWNER_CONSOLE.owner_id
        or chat.type != "private"
    ):
        return

    started = time.perf_counter()
    await context.bot.get_me()
    latency_ms = (time.perf_counter() - started) * 1000
    await message.reply_text(
        f"Pong! <code>Latency: {latency_ms:.1f} ms</code>",
        parse_mode="HTML",
    )


def download_profile_photo_jpeg(url: str) -> bytes:
    """Fetch a PNG/JPEG profile image and convert it to Telegram-ready JPEG."""
    request = urllib.request.Request(url, headers={"User-Agent": "Emy Telegram Bot"})
    with urllib.request.urlopen(request, timeout=20) as response:
        image_data = response.read(10 * 1024 * 1024 + 1)
    if not image_data or len(image_data) > 10 * 1024 * 1024:
        raise ValueError("Profile image is empty or exceeds the 10 MB limit.")

    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        source_suffix = ".png"
    elif image_data.startswith(b"\xff\xd8\xff"):
        source_suffix = ".jpg"
    else:
        raise ValueError("Profile image URL did not return a PNG or JPEG image.")

    with tempfile.TemporaryDirectory(prefix="emy_profile_") as temp_dir:
        source = Path(temp_dir) / f"source{source_suffix}"
        target = Path(temp_dir) / "profile.jpg"
        source.write_bytes(image_data)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-frames:v", "1", "-q:v", "2", str(target),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not target.is_file():
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Could not convert profile image to JPEG: {detail[:200]}")
        jpeg_data = target.read_bytes()
        if not jpeg_data.startswith(b"\xff\xd8\xff"):
            raise RuntimeError("Image conversion did not produce a valid JPEG.")
        return jpeg_data


async def post_init(app) -> None:
    """Configure bot metadata and reusable HTTP resources."""
    app.bot_data["lyrics_http_session"] = lrclib.create_http_session()
    logger.info(
        "Bot display name is managed in BotFather; skipping startup name changes."
    )
    try:
        await app.bot.set_my_description(BOT_DESCRIPTION)
        logger.info("Bot description set")
    except Exception as e:
        logger.warning("Could not set bot description: %s", str(e)[:150])
    try:
        await app.bot.set_my_short_description(BOT_ABOUT)
        logger.info("Bot about text set")
    except Exception as e:
        logger.warning("Could not set bot about text: %s", str(e)[:150])
    if BOT_PROFILE_PHOTO:
        profile_path = Path(BOT_PROFILE_PHOTO).expanduser()
        if not profile_path.is_file():
            logger.error("Configured bot profile photo does not exist: %s", profile_path)
        elif profile_path.suffix.lower() not in (".jpg", ".jpeg"):
            logger.error("Bot profile photo must be a JPEG file: %s", profile_path)
        else:
            try:
                photo_bytes = await asyncio.to_thread(profile_path.read_bytes)
                await upload_bot_profile_photo(
                    app.bot, photo_bytes, profile_path.name
                )
                logger.info("Bot profile photo updated from %s", profile_path)
            except (OSError, TelegramError):
                logger.exception("Could not set bot profile photo from %s", profile_path)
    elif BOT_PROFILE_PHOTO_URL:
        try:
            photo_bytes = await asyncio.to_thread(
                download_profile_photo_jpeg, BOT_PROFILE_PHOTO_URL
            )
            await upload_bot_profile_photo(
                app.bot, photo_bytes, "emy-profile.jpg"
            )
            logger.info("Bot profile photo updated from configured URL")
        except (OSError, TelegramError, ValueError, RuntimeError, subprocess.SubprocessError):
            logger.exception("Could not set bot profile photo from configured URL")

    resolved_packs: dict[str, dict[str, list[str]]] = {}
    for pack_name in CUSTOM_EMOJI_PACK_NAMES:
        try:
            resolved_packs.update(
                await resolve_custom_emoji_packs(app.bot, (pack_name,))
            )
        except TelegramError as error:
            logger.warning(
                "Could not resolve custom emoji pack %s: %s",
                pack_name, str(error)[:200],
            )
    app.bot_data["resolved_custom_emoji_packs"] = resolved_packs
    for pack_name, emoji_ids in resolved_packs.items():
        logger.info(
            "Resolved custom emoji pack %s (%d distinct emoji)",
            pack_name, len(emoji_ids),
        )
    command_payload = [
        {"command": command.command, "description": command.description}
        for command in BOT_COMMANDS
    ]
    command_scopes = (
        ("default", BotCommandScopeDefault()),
        ("all_private_chats", BotCommandScopeAllPrivateChats()),
        ("all_group_chats", BotCommandScopeAllGroupChats()),
        ("all_chat_administrators", BotCommandScopeAllChatAdministrators()),
    )
    for scope_name, command_scope in command_scopes:
        try:
            if not await app.bot.set_my_commands(
                BOT_COMMANDS, scope=command_scope
            ):
                raise RuntimeError("Telegram did not confirm setMyCommands.")
            get_result = await app.bot.get_my_commands(scope=command_scope)
            actual_commands = [
                {"command": command.command, "description": command.description}
                for command in get_result
            ]
            if actual_commands != command_payload:
                raise RuntimeError(
                    f"Telegram returned a different command list for "
                    f"{scope_name}: {actual_commands}"
                )
            logger.info(
                "Verified %d bot commands for Telegram scope %s",
                len(actual_commands),
                scope_name,
            )
        except Exception as error:
            logger.warning(
                "Could not set or verify bot commands for scope %s: %s",
                scope_name,
                str(error)[:200],
            )
    if OWNER_CONSOLE.owner_id:
        try:
            if not await app.bot.set_my_commands(
                OWNER_COMMANDS,
                scope=BotCommandScopeChat(OWNER_CONSOLE.owner_id),
            ):
                raise RuntimeError("Telegram did not confirm owner commands.")
        except (TelegramError, RuntimeError) as error:
            logger.warning(
                "Could not set private owner commands: %s", str(error)[:200]
            )
    else:
        logger.warning("BOT_OWNER_ID is not set; owner-only commands are disabled.")


async def upload_bot_profile_photo(bot, photo_bytes: bytes, filename: str) -> None:
    """Upload a static profile photo using the Bot API's JSON photo field."""
    photo_payload = json.dumps({
        "type": "static",
        "photo": "attach://profile_photo",
    })
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Please use 'Bot\.setMyProfilePhoto' instead of 'Bot\.do_api_request.*",
        )
        await bot.do_api_request(
            "setMyProfilePhoto",
            api_kwargs={
                "photo": photo_payload,
                "profile_photo": InputFile(
                    io.BytesIO(photo_bytes), filename=filename
                ),
            },
        )


async def post_shutdown(app) -> None:
    await OWNER_CONSOLE.close()
    await lrclib.close_http_session(
        app.bot_data.get("lyrics_http_session")
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN not set. Copy .env.example to .env and add your token from @BotFather."
        )
    # 320k MP3s are big (~2.4MB/min); generous timeouts required.
    # 180s write survives ~500 kbps uplinks for a 12 MB file.
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60.0,
        read_timeout=180.0,
        write_timeout=180.0,
        media_write_timeout=180.0,
        pool_timeout=60.0,
    )
    app_builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(8)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    app_builder = configure_local_api(app_builder, TELEGRAM_LOCAL_API_URL)
    if TELEGRAM_LOCAL_API_URL:
        logger.info(
            "Telegram Bot API transport: local server at %s",
            TELEGRAM_LOCAL_API_URL.rstrip("/"),
        )
    else:
        logger.info("Telegram Bot API transport: hosted Bot API")
    app = app_builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", OWNER_CONSOLE.stop_broadcasts))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("play", play_cmd))
    app.add_handler(InlineQueryHandler(inline_search))
    app.add_handler(CommandHandler("playlist", playlist_command))
    app.add_handler(CommandHandler("owner", OWNER_CONSOLE.owner_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("stats", OWNER_CONSOLE.stats_command))
    app.add_handler(CommandHandler("apikey", OWNER_CONSOLE.apikey_command))
    app.add_handler(CommandHandler("broadcast", OWNER_CONSOLE.begin_broadcast))
    app.add_handler(
        CommandHandler("broadcast_button", OWNER_CONSOLE.broadcast_button_command)
    )
    app.add_handler(
        CommandHandler(
            "broadcast_buttons_clear", OWNER_CONSOLE.broadcast_buttons_clear
        )
    )
    app.add_handler(CommandHandler("broadcast_status", OWNER_CONSOLE.broadcast_status))
    app.add_handler(CommandHandler("broadcast_cancel", OWNER_CONSOLE.broadcast_cancel))
    app.add_handler(CommandHandler("broadcast_resume", OWNER_CONSOLE.broadcast_resume))
    app.add_handler(
        TypeHandler(Update, OWNER_CONSOLE.record_update),
        group=-3,
    )
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            OWNER_CONSOLE.capture_owner_message,
        ),
        group=-2,
    )
    app.add_handler(
        CallbackQueryHandler(OWNER_CONSOLE.callback, pattern=r"^owner:"),
    )
    app.add_handler(CallbackQueryHandler(on_button))
    recognition_filters = (
        (filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://"))
        | filters.VIDEO
        | filters.VIDEO_NOTE
        | filters.AUDIO
        | filters.VOICE
        | filters.Document.ALL
    )
    app.add_handler(
        MessageHandler(recognition_filters, receive_recognition_input),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_direct_private_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_pending_photo))
    logger.info("Song search and media provider: native YouTube integration")
    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
