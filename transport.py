"""Telegram Bot API endpoint configuration helpers."""

from urllib.parse import urlsplit

__all__ = ["local_api_urls", "configure_local_api"]


def local_api_urls(base_url: str | None) -> tuple[str, str] | None:
    """Return Bot API and file API URLs for a Local Bot API server."""
    value = (base_url or "").strip()
    if not value:
        return None

    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not any(character.isspace() for character in parsed.netloc)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
        parsed = None
    if not valid or parsed is None:
        raise ValueError(
            "TELEGRAM_LOCAL_API_URL must be an HTTP(S) origin, "
            "for example http://127.0.0.1:8081."
        )

    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/bot", f"{origin}/file/bot"


def configure_local_api(builder, base_url: str | None):
    urls = local_api_urls(base_url)
    if urls is None:
        return builder
    bot_url, file_url = urls
    return (
        builder
        .base_url(bot_url)
        .base_file_url(file_url)
        .local_mode(True)
    )
