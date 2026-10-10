"""Thin helpers for Telegram Bot API features newer than the PTB wrappers."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterable, Mapping
from typing import Any


CUSTOM_EMOJI_IDS = {
    "verified": "5357069174512303778",
    "bin": "5258130763148172425",
    "settings": "5258096772776991776",
    "music": "5258289810082111221",
    "profile": "5258011929993026890",
    "edit": "5258331647358540449",
    "link": "5260730055880876557",
    "add_playlist": "5359529383319084413",
    "announcement": "5260268501515377807",
    "cloud": "5280867942056108177",
    "search": "5429571366384842791",
    "download": "5258336354642697821",
    "upload": "5258043150110301407",
    "error": "5260342697075416641",
    "info": "5258503720928288433",
    "streaming": "5427181942934088912",
    "audio_effect": "5260325873688518261",
    "menu": "5226513232549664618",
    "back": "5258084656674250503",
    "fast_download": "5323761960829862762",
    "warning": "5258474669769497337",
    "edit_thumbnail": "5258254475386167466",
    "edit_artist": "5370546867786523009",
    "effect_off": "5258267368877989660",
    "lyrics": "5258334469152054985",
}

CUSTOM_EMOJI_PACK_NAMES = ("aicreate", "tgiosicons", "tgmacicons")
SUPPORTED_BUTTON_STYLES = frozenset({"primary", "success", "danger"})


async def call_api(bot: Any, method: str, **payload: Any) -> Any:
    """Call a Bot API endpoint using PTB's pooled request client."""
    return await bot.do_api_request(method, api_kwargs=payload)


async def send_rich_message(
    bot: Any,
    chat_id: int | str,
    rich_message: Mapping[str, Any],
    **options: Any,
) -> Any:
    """Send a Telegram InputRichMessage payload without lossy local conversion."""
    return await call_api(
        bot, "sendRichMessage", chat_id=chat_id,
        rich_message=dict(rich_message), **options,
    )


async def send_ephemeral_message(
    bot: Any,
    chat_id: int | str,
    receiver_user_id: int,
    text: str,
    **options: Any,
) -> Any:
    """Send a private-to-recipient response in a Telegram group."""
    return await call_api(
        bot, "sendMessage", chat_id=chat_id, text=text,
        ephemeral_message_parameters={"receiver_user_id": receiver_user_id},
        **options,
    )


def rich_custom_emoji(emoji_id: str, alternative_text: str) -> dict[str, str]:
    """Build a custom-emoji rich-text entity using a caller-supplied verified ID."""
    if not emoji_id or not alternative_text:
        raise ValueError("A custom emoji ID and alternative text are required.")
    return {
        "type": "custom_emoji",
        "custom_emoji_id": emoji_id,
        "alternative_text": alternative_text,
    }


def ephemeral_message_target(message: Any) -> tuple[int, int, int] | None:
    """Extract (chat, receiver, ephemeral-message) IDs from an incoming message."""
    if message is None:
        return None
    raw: dict[str, Any] = {}
    if hasattr(message, "to_dict"):
        raw = message.to_dict()
    extras = getattr(message, "api_kwargs", {})
    if isinstance(extras, Mapping):
        raw.update(extras)

    receiver = getattr(message, "receiver_user", raw.get("receiver_user"))
    receiver_id = getattr(receiver, "id", None)
    if receiver_id is None and isinstance(receiver, Mapping):
        receiver_id = receiver.get("id")
    ephemeral_id = getattr(
        message, "ephemeral_message_id", raw.get("ephemeral_message_id")
    )
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if all(isinstance(value, int) for value in (chat_id, receiver_id, ephemeral_id)):
        return chat_id, receiver_id, ephemeral_id
    return None


def custom_button(
    text: str,
    callback_data: str | None = None,
    *,
    icon_custom_emoji_id: str | None = None,
    style: str | None = None,
    url: str | None = None,
) -> dict[str, str]:
    """Build an inline button using Bot API-supported icon and style fields."""
    button = {"text": text}
    if url:
        button["url"] = url
    elif callback_data:
        button["callback_data"] = callback_data
    else:
        raise ValueError("An inline button needs callback_data or url.")
    if icon_custom_emoji_id:
        button["icon_custom_emoji_id"] = icon_custom_emoji_id
    if style:
        if style not in SUPPORTED_BUTTON_STYLES:
            raise ValueError(f"Unsupported Telegram inline button style: {style}")
        button["style"] = style
    return button


def inline_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


async def resolve_custom_emoji_packs(
    bot: Any,
    set_names: tuple[str, ...] = CUSTOM_EMOJI_PACK_NAMES,
) -> dict[str, dict[str, list[str]]]:
    """Resolve custom-emoji IDs from Telegram sticker-set metadata."""
    resolved: dict[str, dict[str, list[str]]] = {}
    for set_name in set_names:
        sticker_set = await bot.get_sticker_set(set_name)
        by_emoji: dict[str, list[str]] = {}
        for sticker in sticker_set.stickers:
            emoji = sticker.emoji
            emoji_id = sticker.custom_emoji_id
            if emoji and emoji_id:
                by_emoji.setdefault(emoji, []).append(str(emoji_id))
        resolved[set_name] = by_emoji
    return resolved


async def stream_text_response(
    bot: Any,
    chat_id: int | str,
    chunks: AsyncIterable[str],
    *,
    chat_type: str,
    update_interval: float = 0.8,
    minimum_delta: int = 24,
) -> Any:
    """Stream generated text in a private chat, then publish its final message."""
    if chat_type != "private":
        raise ValueError("Telegram message drafts are supported only in private chats.")
    if update_interval <= 0 or minimum_delta < 1:
        raise ValueError("Draft update interval and minimum delta must be positive.")

    draft_id = secrets.randbelow(2**31 - 1) + 1
    parts: list[str] = []
    text_length = 0
    last_sent = ""
    last_update = 0.0
    async for chunk in chunks:
        if not chunk:
            continue
        parts.append(chunk)
        text_length += len(chunk)
        now = time.monotonic()
        should_update = (
            text_length != len(last_sent)
            and (
                not last_sent
                or (
                    now - last_update >= update_interval
                    and text_length - len(last_sent) >= minimum_delta
                )
            )
        )
        if should_update:
            text = "".join(parts)
            await call_api(
                bot, "sendMessageDraft", chat_id=chat_id,
                draft_id=draft_id, text=text,
            )
            last_sent = text
            last_update = now

    if not parts:
        raise ValueError("Cannot finalize an empty streaming response.")
    text = "".join(parts)
    return await call_api(
        bot, "sendMessage", chat_id=chat_id, text=text,
    )


async def stream_rich_response(
    bot: Any,
    chat_id: int | str,
    drafts: AsyncIterable[Mapping[str, Any]],
    final_message: Mapping[str, Any],
    *,
    chat_type: str,
    update_interval: float = 0.8,
) -> Any:
    """Stream InputRichMessage snapshots and finalize them as a rich message."""
    if chat_type != "private":
        raise ValueError("Telegram message drafts are supported only in private chats.")
    if update_interval <= 0:
        raise ValueError("Draft update interval must be positive.")

    draft_id = secrets.randbelow(2**31 - 1) + 1
    last_payload = ""
    last_update = 0.0
    async for draft in drafts:
        serialized = json.dumps(draft, ensure_ascii=False, sort_keys=True)
        now = time.monotonic()
        if serialized == last_payload or (
            last_payload and now - last_update < update_interval
        ):
            continue
        await call_api(
            bot, "sendRichMessageDraft", chat_id=chat_id,
            draft_id=draft_id, rich_message=dict(draft),
        )
        last_payload = serialized
        last_update = now

    return await call_api(
        bot, "sendRichMessage", chat_id=chat_id, rich_message=dict(final_message),
    )
