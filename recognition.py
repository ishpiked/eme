"""Bounded audio extraction and Shazam-based song recognition helpers."""

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import download_range_func


logger = logging.getLogger(__name__)

MAX_RECOGNITION_FILE_BYTES = 20 * 1024 * 1024
RECOGNITION_SAMPLE_SECONDS = 10
SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com", "tiktok.com", "pinterest.com", "pin.it",
    "vimeo.com", "soundcloud.com", "facebook.com",
    "twitter.com", "x.com", "twitch.tv", "reddit.com",
    "bilibili.com", "dailymotion.com", "mixcloud.com", "bandcamp.com",
    "streamable.com", "archive.org", "rumble.com", "odysee.com",
    "vk.com", "rutube.ru",
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
    opts = {
        "format": "bestaudio/best[height<=480]",
        "outtmpl": str(work_dir / "recognition.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "max_filesize": MAX_RECOGNITION_FILE_BYTES,
        "download_ranges": download_range_func(
            None, [(0, RECOGNITION_SAMPLE_SECONDS)]
        ),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(url, download=True)
            if not info:
                raise RecognitionError("The link did not provide accessible media.")
            prepared_path = Path(downloader.prepare_filename(info))
        sample_path = prepared_path.with_suffix(".mp3")
        if not sample_path.is_file():
            candidates = list(work_dir.glob("recognition*.mp3"))
            if not candidates:
                raise RecognitionError("The link did not yield an audio sample.")
            sample_path = candidates[0]
        if sample_path.stat().st_size > MAX_RECOGNITION_FILE_BYTES:
            raise RecognitionError("The extracted audio sample exceeds the size limit.")
        return sample_path
    except RecognitionError:
        raise
    except Exception as error:
        logger.info("Media URL extraction failed (%s).", type(error).__name__)
        raise RecognitionError(
            "This link is unavailable or unsupported. It may be private, removed, "
            "or restricted by its platform."
        ) from error


async def download_url_sample(url: str, work_dir: Path) -> Path:
    if not validate_media_url(url):
        raise RecognitionError("Use a public link from a supported video or music platform.")
    return await asyncio.to_thread(_download_url_sample, url, work_dir)


def _download_url_preview(url: str, work_dir: Path) -> tuple[Path, dict]:
    opts = {
        "format": (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=720]+bestaudio/best[height<=720]"
        ),
        "outtmpl": str(work_dir / "preview.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "max_filesize": MAX_RECOGNITION_FILE_BYTES,
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(url, download=True)
            if not info:
                raise RecognitionError("The link did not provide accessible video.")
            prepared_path = Path(downloader.prepare_filename(info))
        preview_path = prepared_path.with_suffix(".mp4")
        if not preview_path.is_file():
            matches = sorted(work_dir.glob("preview.*"))
            if not matches:
                raise RecognitionError("The link did not yield a video preview.")
            preview_path = matches[0]
        if preview_path.stat().st_size > MAX_RECOGNITION_FILE_BYTES:
            raise RecognitionError("The 720p video preview exceeds the 20 MB size limit.")
        if not info.get("width") and not info.get("height"):
            raise RecognitionError("This link did not provide a video stream.")
        if info.get("height") and info["height"] > 720:
            raise RecognitionError("The source did not provide a video at 720p or lower.")
        return preview_path, info
    except RecognitionError:
        raise
    except Exception as error:
        logger.info("Media URL preview extraction failed (%s).", type(error).__name__)
        raise RecognitionError(
            "This link is unavailable or cannot provide a video preview at 720p or lower."
        ) from error


async def download_url_preview(url: str, work_dir: Path) -> tuple[Path, dict]:
    if not validate_media_url(url):
        raise RecognitionError("Use a public link from a supported video platform.")
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
        max_timeout=2,
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
    def words(value: str) -> set[str]:
        return {
            word for word in re.findall(r"[^\W_]+", value.casefold())
            if len(word) > 1
        }

    expected = words(recognized_title)
    candidate = words(candidate_title)
    if not expected or not candidate:
        return 0.0
    return len(expected & candidate) / len(expected)
