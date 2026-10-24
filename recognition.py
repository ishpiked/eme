"""Bounded audio extraction and Shazam-based song recognition helpers."""

import asyncio
from difflib import SequenceMatcher
import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import youtube


logger = logging.getLogger(__name__)

MAX_RECOGNITION_FILE_BYTES = 20 * 1024 * 1024
RECOGNITION_SAMPLE_SECONDS = 10
SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
)


class RecognitionError(Exception):
    """Expected failures in media extraction or provider recognition."""


def validate_media_url(url: str) -> bool:
    if len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme in ("http", "https")
            and bool(hostname)
            and parsed.username is None
            and parsed.password is None
            and any(
                hostname == domain or hostname.endswith("." + domain)
                for domain in SUPPORTED_DOMAINS
            )
        )
    except ValueError:
        return False


def is_music_url(url: str) -> bool:
    """True for YouTube Music links, which point straight at music."""
    try:
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "music.youtube.com"


def is_audio_or_video(mime_type: str | None, filename: str | None) -> bool:
    if mime_type and (
        mime_type.startswith("audio/") or mime_type.startswith("video/")
    ):
        return True
    suffix = Path(filename or "").suffix.lower()
    return suffix in {
        ".aac", ".flac", ".m4a", ".m4v", ".mkv", ".mov", ".mp3",
        ".mp4", ".mpeg", ".mpg", ".ogg", ".opus", ".wav", ".webm",
        ".wma", ".3gp",
    }


def _extract_uploaded_sample(source_path: Path, sample_path: Path) -> None:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error",
                "-i", str(source_path), "-t", str(RECOGNITION_SAMPLE_SECONDS),
                "-vn", "-ac", "2", "-ar", "44100", "-b:a", "128k",
                str(sample_path),
            ],
            capture_output=True,
            timeout=35,
        )
    except subprocess.TimeoutExpired as error:
        raise RecognitionError("Audio extraction timed out. Please try a shorter clip.") from error
    except FileNotFoundError as error:
        raise RecognitionError("FFmpeg is required to extract audio from this upload.") from error
    if result.returncode or not sample_path.is_file() or sample_path.stat().st_size < 2048:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RecognitionError(
            f"Could not extract a usable audio sample: {detail[:200]}"
        )


async def extract_uploaded_sample(source_path: Path, sample_path: Path) -> None:
    await asyncio.to_thread(_extract_uploaded_sample, source_path, sample_path)


def _download_url_sample(url: str, work_dir: Path) -> Path:
    try:
        player = youtube.resolve(url)
        audio_format = youtube.select_audio_format(player["formats"])
        suffix = youtube.format_extension(audio_format)
        source_path = work_dir / f"recognition-source{suffix}"
        youtube.download_format(
            audio_format,
            source_path,
            max_bytes=MAX_RECOGNITION_FILE_BYTES,
        )
        sample_path = work_dir / "recognition-sample.mp3"
        _extract_uploaded_sample(source_path, sample_path)
        return sample_path
    except RecognitionError:
        raise
    except Exception as error:
        logger.info("YouTube audio extraction failed (%s).", type(error).__name__)
        raise RecognitionError(
            "This YouTube link is unavailable or does not expose a direct audio "
            "stream. It may be private, removed, or restricted."
        ) from error


async def download_url_sample(url: str, work_dir: Path) -> Path:
    if not validate_media_url(url):
        raise RecognitionError("Use a public link from YouTube.")
    return await asyncio.to_thread(_download_url_sample, url, work_dir)


def _download_url_preview(url: str, work_dir: Path) -> tuple[Path, dict]:
    try:
        player = youtube.resolve(url)
        selected = youtube.select_preview_format(
            player["formats"], MAX_RECOGNITION_FILE_BYTES
        )
        preview_path = work_dir / "preview.mp4"
        youtube.download_format(
            selected,
            preview_path,
            max_bytes=MAX_RECOGNITION_FILE_BYTES,
        )
        if preview_path.stat().st_size > MAX_RECOGNITION_FILE_BYTES:
            raise RecognitionError("The 720p video preview exceeds the 20 MB size limit.")
        return preview_path, {
            "id": player["id"],
            "title": player["title"],
            "width": selected["width"],
            "height": selected["height"],
        }
    except RecognitionError:
        raise
    except Exception as error:
        logger.info("YouTube video preview extraction failed (%s).", type(error).__name__)
        raise RecognitionError(
            "This YouTube link is unavailable or cannot provide a direct video "
            "preview at 720p or lower."
        ) from error


async def download_url_preview(url: str, work_dir: Path) -> tuple[Path, dict]:
    if not validate_media_url(url):
        raise RecognitionError("Use a public YouTube link.")
    return await asyncio.to_thread(_download_url_preview, url, work_dir)


def parse_recognition_response(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        raise RecognitionError("The recognition service returned an invalid response.")
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return None
    track = payload.get("track")
    if not isinstance(track, dict):
        return None
    title = track.get("title")
    artist = track.get("subtitle")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(artist, str) or not artist.strip():
        return None
    return {"title": title.strip(), "artist": artist.strip()}


async def _run_shazam(sample_path: Path) -> dict:
    from aiohttp_retry import ExponentialRetry
    from shazamio import HTTPClient, Shazam

    retry_options = ExponentialRetry(
        attempts=2,
        max_timeout=1,
        statuses={429, 500, 502, 503, 504},
    )
    shazam = Shazam(http_client=HTTPClient(retry_options=retry_options))
    return await asyncio.wait_for(
        shazam.recognize(str(sample_path)),
        timeout=30,
    )


async def recognize_sample(sample_path: Path) -> dict | None:
    if not sample_path.is_file():
        raise RecognitionError("The audio sample is missing.")
    if sample_path.stat().st_size > MAX_RECOGNITION_FILE_BYTES:
        raise RecognitionError("The audio sample exceeds the size limit.")
    try:
        payload = await _run_shazam(sample_path)
    except asyncio.TimeoutError as error:
        raise RecognitionError(
            "Song recognition timed out. Please try again."
        ) from error
    except ImportError as error:
        raise RecognitionError(
            "Song recognition support is missing. Install the bot requirements."
        ) from error
    except Exception as error:
        logger.warning(
            "Shazam recognition failed (%s).",
            type(error).__name__,
            exc_info=True,
        )
        raise RecognitionError(
            "The free recognition service is unavailable right now. Please try again."
        ) from error
    return parse_recognition_response(payload)


def title_match_ratio(recognized_title: str, candidate_title: str) -> float:
    def normalize(value: str) -> str:
        return " ".join(re.findall(r"[^\W_]+", value.casefold()))

    expected_text = normalize(recognized_title)
    candidate_text = normalize(candidate_title)
    if not expected_text or not candidate_text:
        return 0.0
    if expected_text == candidate_text:
        return 1.0

    expected = set(expected_text.split())
    candidate = set(candidate_text.split())
    if expected.issubset(candidate):
        return 1.0
    if not expected or not candidate:
        return 0.0
    word_overlap = len(expected & candidate) / len(expected)
    length_tolerance = max(2, int(len(expected_text) * 0.25))
    text_similarity = 0.0
    if abs(len(expected_text) - len(candidate_text)) <= length_tolerance:
        text_similarity = SequenceMatcher(
            None, expected_text, candidate_text
        ).ratio()
        if text_similarity < 0.75:
            text_similarity = 0.0
    return max(word_overlap, text_similarity)
