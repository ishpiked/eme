from __future__ import annotations

import asyncio
from html import escape, unescape
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from features import SUPPORTED_BUTTON_STYLES, call_api, custom_button
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import KeyboardButtonStyle
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)

OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)
TYPE_TUBE_CLUSTERS = (
    ("Netherlands", "tap://nl.clusters.typetube.xyz"),
    ("Singapore", "tap://sg.clusters.typetube.xyz"),
    ("India", "tap://in.clusters.typetube.xyz"),
)
BROADCAST_MAX_BUTTONS = 10
BROADCAST_MAX_BUTTONS_PER_ROW = 2
BROADCAST_MAX_ROWS = 5
BROADCAST_TEXT_LIMIT = 4096
BROADCAST_CAPTION_LIMIT = 1024


def _data_dir() -> Path:
    configured = os.getenv("BOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "emy-song-bot"


DATA_DIR = _data_dir()
DATABASE_FILE = Path(
    os.getenv("BOT_DATABASE_FILE", str(DATA_DIR / "bot-state.sqlite3"))
).expanduser()
TYPE_TUBE_KEY_FILE = Path(
    os.getenv("TYPETUBE_KEY_FILE", str(DATA_DIR / "typetube-api-key"))
).expanduser()


def get_typetube_api_key() -> str | None:
    try:
        value = TYPE_TUBE_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Could not read the TypeTube credential file.")
        raise
    return value or None


def _save_api_key(value: str) -> None:
    TYPE_TUBE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix="typetube-key-", dir=TYPE_TUBE_KEY_FILE.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as key_file:
            key_file.write(value)
            key_file.flush()
            os.fsync(key_file.fileno())
        temporary.replace(TYPE_TUBE_KEY_FILE)
        if os.name != "nt":
            os.chmod(TYPE_TUBE_KEY_FILE, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows))


class OwnerConsole:
    def __init__(self, owner_id: int = OWNER_ID, database_file: Path = DATABASE_FILE):
        self.owner_id = owner_id
        self.database_file = Path(database_file)
        self.awaiting: dict[int, dict] = {}
        self.broadcast_tasks: dict[int, asyncio.Task] = {}
        self._initialized = False

    def _open_connection(self) -> sqlite3.Connection:
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_file, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _connect(self) -> sqlite3.Connection:
        if not self._initialized:
            self._initialize_database()
        return self._open_connection()

    def _initialize_database(self) -> None:
        with self._open_connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    silent INTEGER NOT NULL DEFAULT 0,
                    protect_content INTEGER NOT NULL DEFAULT 0,
                    audience TEXT NOT NULL DEFAULT 'all',
                    buttons_json TEXT NOT NULL DEFAULT '[]',
                    content_type TEXT NOT NULL DEFAULT 'media',
                    rich_text TEXT
                );
                CREATE TABLE IF NOT EXISTS broadcast_recipients (
                    broadcast_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    PRIMARY KEY (broadcast_id, user_id),
                    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(id)
                );
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS playlists_user_name_idx
                    ON playlists(user_id, name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    song_key TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    video_id TEXT,
                    title TEXT NOT NULL,
                    artist TEXT,
                    duration INTEGER,
                    added_at TEXT NOT NULL,
                    UNIQUE(playlist_id, song_key),
                    FOREIGN KEY (playlist_id) REFERENCES playlists(id)
                );
                CREATE TABLE IF NOT EXISTS telegram_audio_cache (
                    video_id TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    base_title TEXT NOT NULL,
                    performer TEXT,
                    duration INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, variant)
                );
                CREATE INDEX IF NOT EXISTS users_last_seen_idx
                    ON users(last_seen);
                CREATE INDEX IF NOT EXISTS broadcast_recipient_status_idx
                    ON broadcast_recipients(broadcast_id, status);
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(broadcasts)")
            }
            if "audience" not in columns:
                db.execute(
                    "ALTER TABLE broadcasts ADD COLUMN audience "
                    "TEXT NOT NULL DEFAULT 'all'"
                )
            if "buttons_json" not in columns:
                db.execute(
                    "ALTER TABLE broadcasts ADD COLUMN buttons_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "content_type" not in columns:
                db.execute(
                    "ALTER TABLE broadcasts ADD COLUMN content_type "
                    "TEXT NOT NULL DEFAULT 'media'"
                )
            if "rich_text" not in columns:
                db.execute("ALTER TABLE broadcasts ADD COLUMN rich_text TEXT")
            db.execute(
                "UPDATE broadcasts SET status='interrupted' WHERE status='running'"
            )
        self._initialized = True

    def create_playlist(self, user_id: int, name: str) -> int:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("Playlist names must be between 1 and 40 characters.")
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO playlists (user_id, name, created_at) "
                    "VALUES (?, ?, ?)",
                    (user_id, name, datetime.now(timezone.utc).isoformat()),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise ValueError("You already have a playlist with that name.") from error

    def get_cached_audio(self, video_id: str, variant: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT file_id, title, base_title, performer, duration "
                "FROM telegram_audio_cache WHERE video_id=? AND variant=?",
                (video_id, variant),
            ).fetchone()
        return dict(row) if row else None

    def cache_audio(
        self,
        video_id: str,
        variant: str,
        *,
        file_id: str,
        title: str,
        base_title: str,
        performer: str | None,
        duration: int | None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO telegram_audio_cache (
                    video_id, variant, file_id, title, base_title,
                    performer, duration, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, variant) DO UPDATE SET
                    file_id=excluded.file_id,
                    title=excluded.title,
                    base_title=excluded.base_title,
                    performer=excluded.performer,
                    duration=excluded.duration,
                    created_at=excluded.created_at
                """,
                (
                    video_id, variant, file_id, title, base_title,
                    performer, duration,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.execute(
                "DELETE FROM telegram_audio_cache WHERE rowid NOT IN "
                "(SELECT rowid FROM telegram_audio_cache "
                "ORDER BY created_at DESC, rowid DESC LIMIT 2048)"
            )

    def delete_cached_audio(self, video_id: str, variant: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM telegram_audio_cache WHERE video_id=? AND variant=?",
                (video_id, variant),
            )

    def list_playlists(self, user_id: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT p.id, p.name, COUNT(s.id) AS song_count
                FROM playlists p
                LEFT JOIN playlist_songs s ON s.playlist_id=p.id
                WHERE p.user_id=?
                GROUP BY p.id
                ORDER BY p.created_at, p.id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_playlist(self, user_id: int, playlist_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, name FROM playlists WHERE id=? AND user_id=?",
                (playlist_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def add_playlist_song(
        self, user_id: int, playlist_id: int, song: dict
    ) -> bool:
        video_id = song.get("video_id")
        if not video_id:
            raise ValueError("This song has no YouTube video ID to save.")
        song_key = f"{video_id}:{song.get('applied_effect', 'off')}"
        with self._connect() as db:
            if not db.execute(
                "SELECT 1 FROM playlists WHERE id=? AND user_id=?",
                (playlist_id, user_id),
            ).fetchone():
                raise ValueError("That playlist was not found.")
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO playlist_songs (
                    playlist_id, song_key, file_id, video_id, title, artist,
                    duration, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_id, song_key, "", video_id,
                    song.get("title") or "Song", song.get("artist"),
                    song.get("duration"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def list_playlist_songs(
        self, user_id: int, playlist_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT s.id, s.video_id, s.title, s.artist, s.duration
                FROM playlist_songs s
                JOIN playlists p ON p.id=s.playlist_id
                WHERE p.id=? AND p.user_id=?
                ORDER BY s.id
                LIMIT ? OFFSET ?
                """,
                (playlist_id, user_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def playlist_song_count(self, user_id: int, playlist_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*)
                FROM playlist_songs s
                JOIN playlists p ON p.id=s.playlist_id
                WHERE p.id=? AND p.user_id=?
                """,
                (playlist_id, user_id),
            ).fetchone()
        return row[0]

    def get_playlist_song(self, user_id: int, song_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT s.video_id, s.title, s.artist, s.duration
                FROM playlist_songs s
                JOIN playlists p ON p.id=s.playlist_id
                WHERE s.id=? AND p.user_id=?
                """,
                (song_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM playlists WHERE id=? AND user_id=?",
                (playlist_id, user_id),
            ).fetchone()
            if not exists:
                return False
            db.execute(
                "DELETE FROM playlist_songs WHERE playlist_id=?",
                (playlist_id,),
            )
            db.execute(
                "DELETE FROM playlists WHERE id=? AND user_id=?",
                (playlist_id, user_id),
            )
        return True

    def is_owner(self, user_id: int | None) -> bool:
        return bool(self.owner_id and user_id == self.owner_id)

    async def _require_owner_private(self, update: Update) -> bool:
        user, chat, message = (
            update.effective_user, update.effective_chat, update.effective_message
        )
        if self.is_owner(user.id if user else None) and chat and chat.type == "private":
            return True
        return False

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE users SET active=? WHERE user_id=?",
                (int(active), user_id),
            )

    async def record_update(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user, chat = update.effective_user, update.effective_chat
        if not user or not chat or chat.type != "private" or user.is_bot:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO users (
                    user_id, first_seen, last_seen, username, first_name, last_name
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name
                """,
                (
                    user.id, now, now, user.username,
                    user.first_name, user.last_name,
                ),
            )

    async def owner_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user, message, chat = (
            update.effective_user, update.effective_message, update.effective_chat
        )
        if not message:
            return
        if not await self._require_owner_private(update):
            return
        await message.reply_text(
            "<b>Owner Console</b>\n\n"
            "<b>Available commands</b>\n"
            "• <code>/stats</code> — view bot statistics and broadcast results\n"
            "• <code>/broadcast</code> — open the button-based composer\n"
            "• <code>/broadcast_status</code> — review recent delivery progress\n"
            "• <code>/broadcast_resume &lt;id&gt;</code> — resume an interrupted broadcast\n\n"
            "Compose content, preview the exact delivery, manage styled buttons, "
            "choose audience and delivery options, then confirm before sending.",
            parse_mode="HTML",
            reply_markup=_keyboard([
                InlineKeyboardButton(
                    "Stats", callback_data="owner:stats",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "TypeTube API key", callback_data="owner:apikey",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "Compose broadcast", callback_data="owner:broadcast:start",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "Broadcast status", callback_data="owner:broadcast:status",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
            ]),
        )

    async def apikey_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if not message or not await self._require_owner_private(update):
            return
        if context.args and context.args[0].lower() in ("remove", "delete"):
            try:
                TYPE_TUBE_KEY_FILE.unlink(missing_ok=True)
            except OSError as error:
                logger.exception("Could not remove the TypeTube API key.")
                await message.reply_text(f"Could not remove the API key: {error}")
            else:
                await message.reply_text("TypeTube API key removed.")
            return
        await self._prompt_for_api_key(message)

    async def _prompt_for_api_key(self, message) -> None:
        prompt = await message.reply_text(
            "<b>TypeTube API key</b>\n\n"
            "Send your private key here. It will be verified before it is saved.\n\n"
            "Use <code>/cancel</code> to abort.",
            parse_mode="HTML",
        )
        self.awaiting[self.owner_id] = {
            "kind": "api_key",
            "prompt_chat_id": prompt.chat_id,
            "prompt_message_id": prompt.message_id,
        }

    async def stats_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user, message, chat = (
            update.effective_user, update.effective_message, update.effective_chat
        )
        if not message or not await self._require_owner_private(update):
            return
        await message.reply_text(await self._stats_text(), parse_mode="HTML")

    async def _stats_text(self) -> str:
        now = datetime.now(timezone.utc)
        day = (now.timestamp() - 86400)
        week = now.timestamp() - 7 * 86400
        month = now.timestamp() - 30 * 86400
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active = db.execute(
                "SELECT COUNT(*) FROM users WHERE active=1"
            ).fetchone()[0]
            seen_day, seen_week, seen_month = db.execute(
                "SELECT "
                "SUM(last_seen >= ?), SUM(last_seen >= ?), SUM(last_seen >= ?) "
                "FROM users",
                (
                    datetime.fromtimestamp(day, timezone.utc).isoformat(),
                    datetime.fromtimestamp(week, timezone.utc).isoformat(),
                    datetime.fromtimestamp(month, timezone.utc).isoformat(),
                ),
            ).fetchone()
            joined_today, joined_week, joined_month = db.execute(
                "SELECT "
                "SUM(first_seen >= ?), SUM(first_seen >= ?), SUM(first_seen >= ?) "
                "FROM users",
                (
                    datetime.fromtimestamp(day, timezone.utc).isoformat(),
                    datetime.fromtimestamp(week, timezone.utc).isoformat(),
                    datetime.fromtimestamp(month, timezone.utc).isoformat(),
                ),
            ).fetchone()
            broadcasts = db.execute(
                "SELECT COUNT(*) FROM broadcasts"
            ).fetchone()[0]
            unreachable = db.execute(
                "SELECT COUNT(*) FROM users WHERE active=0"
            ).fetchone()[0]
            total_sent, total_failed = db.execute(
                "SELECT SUM(status='sent'), "
                "SUM(status IN ('failed','blocked')) "
                "FROM broadcast_recipients"
            ).fetchone()
            recent = db.execute(
                """
                SELECT b.id, b.status,
                       SUM(r.status='sent') AS sent,
                       SUM(r.status IN ('failed','blocked')) AS failed,
                       SUM(r.status='pending') AS pending
                FROM broadcasts b
                LEFT JOIN broadcast_recipients r ON r.broadcast_id=b.id
                GROUP BY b.id ORDER BY b.id DESC LIMIT 5
                """
            ).fetchall()
        latest_text = (
            f"The latest broadcast is number {recent[0]['id']}, marked as "
            f"{escape(recent[0]['status'])}, with {recent[0]['sent'] or 0} "
            f"delivered, {recent[0]['failed'] or 0} failed and "
            f"{recent[0]['pending'] or 0} pending."
            if recent else "The bot has not initiated a broadcast yet."
        )
        user_word = "user" if total == 1 else "users"
        reachable_word = "user" if active == 1 else "users"
        unreachable_word = "user" if unreachable == 1 else "users"
        broadcast_word = "broadcast" if broadcasts == 1 else "broadcasts"
        return (
            '<tg-emoji emoji-id="5258330865674494479">📊</tg-emoji> '
            "<b>Bot Statistics</b>\n\n"
            f"The bot has {total} registered {user_word}, with {active} reachable "
            f"{reachable_word} and {unreachable} inactive or unreachable "
            f"{unreachable_word}. "
            f"User activity in the last 24 hours, 7 days and 30 days stands at "
            f"{seen_day or 0}, {seen_week or 0} and {seen_month or 0} respectively. "
            f"New users recorded during the same periods are {joined_today or 0}, "
            f"{joined_week or 0} and {joined_month or 0}.\n\n"
            f"The bot has initiated {broadcasts} {broadcast_word}, with "
            f"{total_sent or 0} successful deliveries and "
            f"{total_failed or 0} failed deliveries.\n\n"
            f"{latest_text}"
        )

    async def begin_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        message = update.effective_message
        if not message or not await self._require_owner_private(update):
            return
        await self._start_broadcast(message)

    async def _start_broadcast(self, message) -> None:
        self.awaiting[self.owner_id] = {
            "kind": "broadcast_content",
            "silent": False,
            "protect_content": False,
            "audience": "all",
            "buttons": [],
        }
        await message.reply_text(
            "<b>Broadcast Composer</b>\n\n"
            "Send the announcement text or a photo, video, GIF, file, or audio. "
            "Telegram formatting and media captions are preserved. Premium emoji "
            "can be written as <code>{emoji_id}</code>.\n\n"
            "After the content arrives, use the buttons to preview it, build "
            "styled URL buttons, choose recipients, and confirm delivery. "
            "Nothing is sent until you confirm.",
            parse_mode="HTML",
        )

    async def capture_owner_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user, message, chat = (
            update.effective_user, update.effective_message, update.effective_chat
        )
        if not user or user.id != self.owner_id or not chat or chat.type != "private":
            return
        pending = self.awaiting.get(user.id)
        if not pending or not message:
            return
        kind = pending.get("kind")
        if kind == "api_key":
            key = (message.text or "").strip()
            try:
                if not key or len(key) > 4096 or any(char.isspace() for char in key):
                    raise ValueError("The API key must be a non-empty single-line value.")
                from typetube import create_client

                def validate_key() -> None:
                    last_error = None
                    for _, cluster_host in TYPE_TUBE_CLUSTERS:
                        client = create_client(
                            api_key=key,
                            host=cluster_host,
                            port=8443,
                            timeout_ms=10000,
                        )
                        try:
                            client.ping()
                            return
                        except Exception as error:
                            last_error = error
                        finally:
                            client.close()
                    if last_error:
                        raise last_error
                    raise ValueError("TypeTube did not validate this API key.")

                await asyncio.to_thread(validate_key)
                _save_api_key(key)
            except Exception as error:
                logger.warning("TypeTube API key validation failed: %s", type(error).__name__)
                await message.reply_text("Could not verify or save the TypeTube API key.")
            else:
                await message.reply_text("TypeTube API key saved.")
            finally:
                prompt_chat_id = pending.get("prompt_chat_id")
                prompt_message_id = pending.get("prompt_message_id")
                self.awaiting.pop(user.id, None)
                if prompt_chat_id and prompt_message_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=prompt_chat_id,
                            message_id=prompt_message_id,
                        )
                    except TelegramError as error:
                        logger.warning("Could not delete TypeTube key prompt: %s", error)
                try:
                    await message.delete()
                except TelegramError as error:
                    logger.warning("Could not delete submitted TypeTube key: %s", error)
            raise ApplicationHandlerStop
        if kind == "broadcast_content":
            content_type = "text" if message.text is not None else "media"
            rich_text = (
                message.text_html
                if message.text is not None
                else message.caption_html
            ) or ""
            text_limit = (
                BROADCAST_TEXT_LIMIT
                if content_type == "text"
                else BROADCAST_CAPTION_LIMIT
            )
            if self._broadcast_plain_length(rich_text) > text_limit:
                pending["kind"] = "broadcast_content"
                await message.reply_text(
                    f"This {'message' if content_type == 'text' else 'caption'} "
                    f"is too long. Keep it within {text_limit} characters and send it again."
                )
                raise ApplicationHandlerStop
            self._update_broadcast_content(pending, message, chat.id)
            pending["kind"] = "broadcast_draft"
            if pending.get("control_chat_id") and pending.get("control_message_id"):
                await self._edit_broadcast_panel(context.bot, pending)
            else:
                control_message = await message.reply_text(
                    self._broadcast_panel_text(pending),
                    parse_mode="HTML",
                    reply_markup=self._draft_markup(pending),
                )
                pending["control_chat_id"] = control_message.chat_id
                pending["control_message_id"] = control_message.message_id
            raise ApplicationHandlerStop
        if kind == "broadcast_button_label":
            label = (message.text or "").strip()
            emoji_ids = re.findall(r"\{([0-9]+)\}", label)
            if len(emoji_ids) > 1:
                await message.reply_text("A button can have at most one custom emoji icon.")
                raise ApplicationHandlerStop
            label = re.sub(r"\{[0-9]+\}", "", label).strip()
            if not label or len(label) > 64:
                await message.reply_text("Send a button label between 1 and 64 characters.")
                raise ApplicationHandlerStop
            pending["button_label"] = label
            pending["button_emoji_id"] = emoji_ids[0] if emoji_ids else None
            pending["kind"] = "broadcast_button_url"
            await self._edit_broadcast_panel(
                context.bot,
                pending,
                text=(
                    "<b>Add a link button</b>\n\n"
                    f"Label: <b>{escape(label)}</b>\n\n"
                    "Now send its HTTPS URL. Use /cancel to discard the draft."
                ),
                reply_markup=self._button_input_markup(),
            )
            raise ApplicationHandlerStop
        if kind == "broadcast_button_url":
            url = (message.text or "").strip()
            parsed_url = urlparse(url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.hostname
                or len(url) > 2048
                or any(char.isspace() for char in url)
            ):
                await message.reply_text(
                    "Send a valid HTTPS URL no longer than 2,048 characters."
                )
                raise ApplicationHandlerStop
            pending["button_url"] = url
            pending["kind"] = "broadcast_button_style"
            await self._edit_broadcast_panel(
                context.bot,
                pending,
                text=(
                    "<b>Choose a button style</b>\n\n"
                    f"{escape(pending['button_label'])}\n"
                    f"<code>{escape(url)}</code>"
                ),
                reply_markup=self._button_style_markup(),
            )
            raise ApplicationHandlerStop

    async def broadcast_button_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._require_owner_private(update):
            return
        await self._broadcast_button_command(update, context.bot)
        raise ApplicationHandlerStop

    async def _broadcast_button_command(self, update: Update, bot) -> None:
        message = update.effective_message
        draft = self.awaiting.get(self.owner_id)
        if not message or not draft or draft.get("kind") != "broadcast_draft":
            if message:
                await message.reply_text("Start a draft with /broadcast first.")
            return
        parts = (message.text or "").partition(" ")[2].split("|")
        if len(parts) not in (3, 4):
            await message.reply_text(
                "Usage: /broadcast_button Label | https://example.com | "
                "primary|success|danger [| row]"
            )
            return
        label, url, style = (part.strip() for part in parts[:3])
        row_number = None
        if len(parts) == 4:
            row_text = parts[3].strip()
            if not row_text.isdigit() or int(row_text) < 1:
                await message.reply_text("The optional row number must be positive.")
                return
            row_number = int(row_text) - 1
        emoji_ids = re.findall(r"\{([0-9]+)\}", label)
        if len(emoji_ids) > 1:
            await message.reply_text(
                "A button can have at most one custom emoji icon."
            )
            return
        label = re.sub(r"\{[0-9]+\}", "", label).strip()
        parsed_url = urlparse(url)
        if (
            not label or len(label) > 64 or not parsed_url.hostname
            or parsed_url.scheme not in ("http", "https")
            or len(url) > 2048
            or any(char.isspace() for char in url)
        ):
            await message.reply_text(
                "Use a button label (up to 64 characters) and a valid HTTP(S) URL."
            )
            return
        if style not in SUPPORTED_BUTTON_STYLES:
            await message.reply_text(
                "Choose one of these button styles: primary, success, danger."
            )
            return
        rows = draft.setdefault("buttons", [])
        if row_number is None:
            row_number = len(rows) - 1 if rows else 0
        if row_number > len(rows) or row_number >= BROADCAST_MAX_ROWS:
            await message.reply_text(
                f"Button rows must be added in order (up to {BROADCAST_MAX_ROWS})."
            )
            return
        if sum(map(len, rows)) >= BROADCAST_MAX_BUTTONS:
            await message.reply_text(
                f"A broadcast can have at most {BROADCAST_MAX_BUTTONS} buttons."
            )
            return
        if row_number == len(rows):
            rows.append([])
        if len(rows[row_number]) >= BROADCAST_MAX_BUTTONS_PER_ROW:
            await message.reply_text(
                f"Each row can have at most {BROADCAST_MAX_BUTTONS_PER_ROW} buttons."
            )
            return
        rows[row_number].append(custom_button(
            label,
            url=url,
            style=style,
            icon_custom_emoji_id=emoji_ids[0] if emoji_ids else None,
        ))
        await self._refresh_draft_markup(bot, draft)
        await message.reply_text(
            f"Added {label!r} ({style}) in row {row_number + 1}.",
        )

    async def broadcast_buttons_clear(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if not message or not await self._require_owner_private(update):
            return
        draft = self.awaiting.get(self.owner_id)
        if not draft or draft.get("kind") != "broadcast_draft":
            await message.reply_text("Start a draft with /broadcast first.")
            return
        draft["buttons"] = []
        await self._refresh_draft_markup(context.bot, draft)
        await message.reply_text("All draft buttons have been removed.")

    async def _refresh_draft_markup(self, bot, draft: dict) -> None:
        try:
            await self._edit_broadcast_panel(bot, draft)
        except TelegramError as error:
            logger.warning("Could not refresh broadcast draft controls: %s", error)

    async def _edit_broadcast_panel(
        self,
        bot,
        draft: dict,
        *,
        text: str | None = None,
        reply_markup=None,
    ) -> None:
        chat_id = draft.get("control_chat_id")
        message_id = draft.get("control_message_id")
        if not chat_id or not message_id:
            return
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text or self._broadcast_panel_text(draft),
            parse_mode="HTML",
            reply_markup=(
                reply_markup if reply_markup is not None else self._draft_markup(draft)
            ),
        )

    @staticmethod
    def _button_input_markup() -> InlineKeyboardMarkup:
        return _keyboard([
            InlineKeyboardButton(
                "Back to button manager",
                callback_data="owner:broadcast:buttons",
                style=KeyboardButtonStyle.DANGER,
            )
        ])

    def _draft_markup(self, draft: dict) -> InlineKeyboardMarkup:
        button_count = sum(map(len, draft.get("buttons", [])))
        return _keyboard(
            [
                InlineKeyboardButton(
                    "Preview to me",
                    callback_data="owner:broadcast:test",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "Edit content",
                    callback_data="owner:broadcast:edit",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔕 Silent: {'On' if draft['silent'] else 'Off'}",
                    callback_data="owner:broadcast:silent",
                    style=(
                        KeyboardButtonStyle.SUCCESS
                        if draft["silent"] else KeyboardButtonStyle.PRIMARY
                    ),
                ),
                InlineKeyboardButton(
                    f"🔒 Protect: {'On' if draft['protect_content'] else 'Off'}",
                    callback_data="owner:broadcast:protect",
                    style=(
                        KeyboardButtonStyle.SUCCESS
                        if draft["protect_content"] else KeyboardButtonStyle.PRIMARY
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "Audience: all active users"
                    if draft["audience"] == "all"
                    else "Audience: active in 30 days",
                    callback_data="owner:broadcast:audience",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Manage buttons ({button_count})",
                    callback_data="owner:broadcast:buttons",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
            ],
            [
                InlineKeyboardButton(
                    "Send to all users",
                    callback_data="owner:broadcast:send",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
            ],
            [
                InlineKeyboardButton(
                    "Cancel broadcast",
                    callback_data="owner:broadcast:discard_prompt",
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
        )

    @staticmethod
    def _button_style_markup() -> InlineKeyboardMarkup:
        return _keyboard(
            [
                InlineKeyboardButton(
                    "Primary", callback_data="owner:broadcast:style:primary",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "Success", callback_data="owner:broadcast:style:success",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "Danger", callback_data="owner:broadcast:style:danger",
                    style=KeyboardButtonStyle.DANGER,
                ),
            ],
            [
                InlineKeyboardButton(
                    "Cancel button", callback_data="owner:broadcast:buttons",
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
        )

    def _button_manager_markup(self, draft: dict) -> InlineKeyboardMarkup:
        rows = []
        for row_index, button_row in enumerate(draft.get("buttons", [])):
            for button_index, button in enumerate(button_row):
                rows.append([
                    InlineKeyboardButton(
                        f"Remove {button['text'][:32]}",
                        callback_data=(
                            f"owner:broadcast:button_remove:{row_index}:{button_index}"
                        ),
                        style=KeyboardButtonStyle.DANGER,
                    )
                ])
        if sum(map(len, draft.get("buttons", []))) < BROADCAST_MAX_BUTTONS:
            rows.append([
                InlineKeyboardButton(
                    "Add link button",
                    callback_data="owner:broadcast:button_add",
                    style=KeyboardButtonStyle.SUCCESS,
                )
            ])
        if draft.get("buttons"):
            rows.append([
                InlineKeyboardButton(
                    "Clear all buttons",
                    callback_data="owner:broadcast:button_clear",
                    style=KeyboardButtonStyle.DANGER,
                )
            ])
        rows.append([
            InlineKeyboardButton(
                "Back to composer",
                callback_data="owner:broadcast:back",
                style=KeyboardButtonStyle.PRIMARY,
            )
        ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _button_manager_text(draft: dict) -> str:
        buttons = draft.get("buttons", [])
        total = sum(map(len, buttons))
        lines = [
            "<b>Broadcast Buttons</b>\n",
            f"{total}/{BROADCAST_MAX_BUTTONS} buttons configured. "
            f"Each row supports up to {BROADCAST_MAX_BUTTONS_PER_ROW} buttons "
            f"({BROADCAST_MAX_ROWS} rows maximum).",
        ]
        for row_index, button_row in enumerate(buttons, start=1):
            labels = ", ".join(
                escape(button.get("text", "Button"))
                for button in button_row
            )
            lines.append(f"\n<b>Row {row_index}:</b> {labels}")
        if not total:
            lines.append("\nNo buttons added yet.")
        lines.append("\nUse the buttons below to add or remove links.")
        return "\n".join(lines)

    @staticmethod
    def _broadcast_plain_length(value: str | None) -> int:
        plain = re.sub(r"<[^>]*>", "", value or "")
        return len(unescape(plain))

    @staticmethod
    def _update_broadcast_content(draft: dict, message, chat_id: int) -> None:
        draft.update({
            "source_chat_id": chat_id,
            "source_message_id": message.message_id,
            "content_type": "text" if message.text is not None else "media",
            "rich_text": (
                message.text_html
                if message.text is not None
                else message.caption_html
            ) or "",
        })

    @staticmethod
    def _broadcast_panel_text(draft: dict) -> str:
        content_type = draft.get("content_type")
        rich_text = draft.get("rich_text") or ""
        preview = unescape(re.sub(r"<[^>]*>", "", rich_text))
        preview = preview[:240] + ("…" if len(preview) > 240 else "")
        media = "Text" if content_type == "text" else "Media"
        if content_type == "media" and not rich_text:
            preview = "(No caption)"
        button_count = sum(map(len, draft.get("buttons", [])))
        audience = (
            "all active users"
            if draft.get("audience") == "all"
            else "users active in the last 30 days"
        )
        return (
            "<b>Broadcast Composer</b>\n\n"
            f"<b>Content:</b> {media} · "
            f"{OwnerConsole._broadcast_plain_length(rich_text)} characters\n"
            f"<b>Buttons:</b> {button_count}/{BROADCAST_MAX_BUTTONS}\n"
            f"<b>Audience:</b> {audience}\n"
            f"<b>Silent:</b> {'On' if draft.get('silent') else 'Off'} · "
            f"<b>Protected:</b> {'On' if draft.get('protect_content') else 'Off'}\n\n"
            f"<blockquote>{escape(preview)}</blockquote>\n\n"
            "Preview and check the final card before confirming delivery."
        )

    async def callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not str(query.data or "").startswith("owner:"):
            return
        if not self.is_owner(query.from_user.id) or not query.message or query.message.chat.type != "private":
            await query.answer()
            return
        await query.answer()
        action = query.data.split(":")
        if action == ["owner", "stats"]:
            await query.message.reply_text(await self._stats_text(), parse_mode="HTML")
        elif action == ["owner", "apikey"]:
            await self._prompt_for_api_key(query.message)
        elif action[:2] == ["owner", "broadcast"]:
            if action[2:] == ["start"]:
                await self._start_broadcast(query.message)
            elif action[2:] == ["status"]:
                text, markup = self._broadcast_status_view()
                await query.message.reply_text(
                    text, parse_mode="HTML", reply_markup=markup
                )
            elif len(action) == 4 and action[2] == "cancel_prompt":
                try:
                    broadcast_id = int(action[3])
                except ValueError:
                    await query.message.reply_text("Invalid broadcast ID.")
                else:
                    task = self.broadcast_tasks.get(broadcast_id)
                    if not task or task.done():
                        await query.message.reply_text(
                            "That broadcast is no longer running."
                        )
                    else:
                        await query.edit_message_text(
                            f"<b>Cancel broadcast #{broadcast_id}?</b>\n\n"
                            "Messages already delivered cannot be recalled. "
                            "Pending deliveries will stop.",
                            parse_mode="HTML",
                            reply_markup=_keyboard(
                                [
                                    InlineKeyboardButton(
                                        "Confirm cancellation",
                                        callback_data=(
                                            f"owner:broadcast:cancel_id:{broadcast_id}"
                                        ),
                                        style=KeyboardButtonStyle.DANGER,
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "Keep sending",
                                        callback_data="owner:broadcast:status",
                                        style=KeyboardButtonStyle.SUCCESS,
                                    )
                                ],
                            ),
                        )
            elif len(action) == 4 and action[2] == "cancel_id":
                try:
                    broadcast_id = int(action[3])
                except ValueError:
                    await query.message.reply_text("Invalid broadcast ID.")
                else:
                    task = self.broadcast_tasks.get(broadcast_id)
                    if not task or task.done():
                        await query.message.reply_text("That broadcast is not running.")
                    else:
                        task.cancel()
                        await query.message.reply_text(
                            f"Cancellation requested for broadcast #{broadcast_id}."
                        )
            elif len(action) == 4 and action[2] == "resume_id":
                try:
                    broadcast_id = int(action[3])
                except ValueError:
                    await query.message.reply_text("Invalid broadcast ID.")
                else:
                    status_message = await query.message.reply_text(
                        f"Resuming broadcast #{broadcast_id}…"
                    )
                    if not self._queue_broadcast_resume(
                        broadcast_id, context.bot, status_message
                    ):
                        await status_message.edit_text(
                            "That broadcast is already running or cannot be resumed."
                        )
            else:
                await self._broadcast_callback(query, action[2:], context)

    async def _broadcast_callback(self, query, action: list[str], context) -> None:
        draft = self.awaiting.get(self.owner_id)
        if action == ["discard_prompt"]:
            await query.edit_message_text(
                "<b>Discard this broadcast draft?</b>\n\nNothing has been sent yet.",
                parse_mode="HTML",
                reply_markup=_keyboard(
                    [
                        InlineKeyboardButton(
                            "Discard draft",
                            callback_data="owner:broadcast:cancel",
                            style=KeyboardButtonStyle.DANGER,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Keep editing",
                            callback_data="owner:broadcast:back",
                            style=KeyboardButtonStyle.SUCCESS,
                        )
                    ],
                ),
            )
            return
        if action == ["cancel"]:
            self.awaiting.pop(self.owner_id, None)
            await query.edit_message_text(
                "<b>Broadcast discarded.</b>\n\nNothing was sent.",
                parse_mode="HTML",
            )
            return
        if not draft or draft.get("kind") not in (
            "broadcast_draft",
            "broadcast_button_style",
            "broadcast_content",
            "broadcast_button_label",
            "broadcast_button_url",
        ):
            await query.edit_message_text("This broadcast draft has expired.")
            return
        if action in (["silent"], ["protect"], ["audience"]):
            if action == ["silent"]:
                draft["silent"] = not draft["silent"]
            elif action == ["protect"]:
                draft["protect_content"] = not draft["protect_content"]
            else:
                draft["audience"] = (
                    "recent" if draft["audience"] == "all" else "all"
                )
            await query.edit_message_text(
                self._broadcast_panel_text(draft),
                parse_mode="HTML",
                reply_markup=self._draft_markup(draft),
            )
            return
        if action == ["buttons"]:
            for key in ("button_label", "button_url", "button_emoji_id"):
                draft.pop(key, None)
            draft["kind"] = "broadcast_draft"
            await query.edit_message_text(
                self._button_manager_text(draft),
                parse_mode="HTML",
                reply_markup=self._button_manager_markup(draft),
            )
            return
        if action == ["edit"]:
            if draft.get("kind") != "broadcast_draft":
                await query.message.reply_text("Finish button setup first.")
                return
            draft["kind"] = "broadcast_content"
            await query.edit_message_text(
                "<b>Edit announcement content</b>\n\n"
                "Send the replacement text or media now. Formatting and captions "
                "will be preserved; the current buttons and delivery options stay unchanged.",
                parse_mode="HTML",
                reply_markup=_keyboard([
                    InlineKeyboardButton(
                        "Back to composer",
                        callback_data="owner:broadcast:back",
                        style=KeyboardButtonStyle.DANGER,
                    )
                ]),
            )
            return
        if action == ["button_add"]:
            if sum(map(len, draft.get("buttons", []))) >= BROADCAST_MAX_BUTTONS:
                await query.edit_message_text(
                    self._button_manager_text(draft),
                    parse_mode="HTML",
                    reply_markup=self._button_manager_markup(draft),
                )
                return
            draft["kind"] = "broadcast_button_label"
            await query.edit_message_text(
                "<b>Add a link button</b>\n\nSend its label (up to 64 characters). "
                "Optionally include one premium emoji as <code>{emoji_id}</code>.",
                parse_mode="HTML",
                reply_markup=_keyboard([
                    InlineKeyboardButton(
                        "Back to button manager",
                        callback_data="owner:broadcast:buttons",
                        style=KeyboardButtonStyle.DANGER,
                    )
                ]),
            )
            return
        if action and action[0] == "button_remove" and len(action) == 3:
            try:
                row_index, button_index = int(action[1]), int(action[2])
                draft["buttons"][row_index][button_index]
            except (IndexError, KeyError, TypeError, ValueError):
                await query.edit_message_text(
                    self._button_manager_text(draft),
                    parse_mode="HTML",
                    reply_markup=self._button_manager_markup(draft),
                )
                return
            draft["buttons"][row_index].pop(button_index)
            if not draft["buttons"][row_index]:
                draft["buttons"].pop(row_index)
            draft["kind"] = "broadcast_draft"
            await query.edit_message_text(
                self._button_manager_text(draft),
                parse_mode="HTML",
                reply_markup=self._button_manager_markup(draft),
            )
            return
        if action == ["button_clear"]:
            draft["buttons"] = []
            draft["kind"] = "broadcast_draft"
            await query.edit_message_text(
                self._button_manager_text(draft),
                parse_mode="HTML",
                reply_markup=self._button_manager_markup(draft),
            )
            return
        if action and action[0] == "style" and len(action) == 2:
            style = action[1]
            if (
                draft.get("kind") != "broadcast_button_style"
                or style not in SUPPORTED_BUTTON_STYLES
            ):
                await query.edit_message_text(
                    "This button setup has expired.",
                    reply_markup=self._button_manager_markup(draft),
                )
                return
            buttons = draft.setdefault("buttons", [])
            if sum(map(len, buttons)) >= BROADCAST_MAX_BUTTONS:
                await query.edit_message_text(
                    f"The maximum of {BROADCAST_MAX_BUTTONS} buttons has been reached."
                )
                draft["kind"] = "broadcast_draft"
                return
            if not buttons or len(buttons[-1]) >= BROADCAST_MAX_BUTTONS_PER_ROW:
                if len(buttons) >= BROADCAST_MAX_ROWS:
                    await query.edit_message_text(
                        f"The maximum of {BROADCAST_MAX_ROWS} button rows has been reached."
                    )
                    draft["kind"] = "broadcast_draft"
                    return
                buttons.append([])
            buttons[-1].append(custom_button(
                draft.pop("button_label"),
                url=draft.pop("button_url"),
                style=style,
                icon_custom_emoji_id=draft.pop("button_emoji_id", None),
            ))
            draft["kind"] = "broadcast_draft"
            await query.edit_message_text(
                self._button_manager_text(draft),
                parse_mode="HTML",
                reply_markup=self._button_manager_markup(draft),
            )
            return
        if action == ["back"]:
            draft["kind"] = "broadcast_draft"
            draft["confirming"] = False
            await query.edit_message_text(
                self._broadcast_panel_text(draft),
                parse_mode="HTML",
                reply_markup=self._draft_markup(draft),
            )
            return
        if action == ["test"]:
            if draft.get("kind") != "broadcast_draft":
                await query.message.reply_text("Finish or cancel button setup first.")
                return
            try:
                await self._send_broadcast_message(context.bot, draft, self.owner_id)
            except TelegramError as error:
                await query.message.reply_text(
                    f"Preview failed: {escape(str(error))}",
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text("Preview sent to your chat.")
            return
        if action == ["send"]:
            if draft.get("kind") != "broadcast_draft":
                await query.message.reply_text("Finish or cancel button setup first.")
                return
            if not draft.get("source_message_id"):
                await query.message.reply_text("Add announcement content first.")
                return
            draft["confirming"] = True
            recipients = self._audience_count(draft["audience"])
            audience_text = (
                "active users seen in the last 30 days"
                if draft["audience"] == "recent" else "all active users"
            )
            await query.edit_message_text(
                "<b>Confirm broadcast</b>\n\n"
                f"This will deliver the previewed announcement to "
                f"<b>{recipients}</b> {audience_text}.\n"
                f"Silent: {'On' if draft['silent'] else 'Off'} · "
                f"Protected: {'On' if draft['protect_content'] else 'Off'}\n\n"
                "This action cannot be undone.",
                parse_mode="HTML",
                reply_markup=_keyboard(
                    [
                        InlineKeyboardButton(
                            f"Confirm · {recipients} users",
                            callback_data="owner:broadcast:confirm",
                            style=KeyboardButtonStyle.SUCCESS,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Back to options",
                            callback_data="owner:broadcast:back",
                            style=KeyboardButtonStyle.PRIMARY,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Discard broadcast", callback_data="owner:broadcast:cancel",
                            style=KeyboardButtonStyle.DANGER,
                        )
                    ],
                ),
            )
            return
        if action == ["confirm"]:
            if draft.get("kind") != "broadcast_draft" or not draft.get("confirming"):
                await query.edit_message_text("Review and confirm the broadcast first.")
                return
            broadcast_id = self._create_broadcast(draft)
            self.awaiting.pop(self.owner_id, None)
            recipients = self._pending_count(broadcast_id)
            if recipients == 0:
                self._set_broadcast_status(broadcast_id, "completed")
                await query.edit_message_text("No reachable users are registered yet.")
                return
            await query.edit_message_text(
                f"<b>Broadcast #{broadcast_id} started</b>\n\n"
                f"Queued for {recipients} reachable users.\n"
                f"Silent: {'yes' if draft['silent'] else 'no'} · "
                f"Protected: {'yes' if draft['protect_content'] else 'no'}",
                parse_mode="HTML",
            )
            status_message = await query.message.reply_text(
                f"Broadcast #{broadcast_id}: preparing delivery…"
            )
            task = asyncio.create_task(
                self._deliver_broadcast(broadcast_id, context.bot, status_message)
            )
            self.broadcast_tasks[broadcast_id] = task
            task.add_done_callback(
                lambda completed, key=broadcast_id: self._broadcast_done(key, completed)
            )

    def _broadcast_done(self, broadcast_id: int, task: asyncio.Task) -> None:
        self.broadcast_tasks.pop(broadcast_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "Broadcast #%d stopped unexpectedly: %s",
                broadcast_id, error, exc_info=error,
            )

    def _create_broadcast(self, draft: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO broadcasts (
                    created_at, status, source_chat_id, source_message_id,
                    silent, protect_content, audience, buttons_json,
                    content_type, rich_text
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now, draft["source_chat_id"], draft["source_message_id"],
                    int(draft["silent"]), int(draft["protect_content"]),
                    draft["audience"],
                    json.dumps(draft.get("buttons", []), ensure_ascii=False),
                    draft.get("content_type", "media"),
                    self._rich_text_with_custom_emoji(draft.get("rich_text")),
                ),
            )
            broadcast_id = int(cursor.lastrowid)
            if draft["audience"] == "recent":
                cutoff = datetime.fromtimestamp(
                    time.time() - 30 * 86400, timezone.utc
                ).isoformat()
                db.execute(
                    """
                    INSERT INTO broadcast_recipients (broadcast_id, user_id)
                    SELECT ?, user_id FROM users
                    WHERE active=1 AND user_id != ? AND last_seen >= ?
                    """,
                    (broadcast_id, self.owner_id, cutoff),
                )
            else:
                db.execute(
                    """
                    INSERT INTO broadcast_recipients (broadcast_id, user_id)
                    SELECT ?, user_id FROM users WHERE active=1 AND user_id != ?
                    """,
                    (broadcast_id, self.owner_id),
                )
            db.execute(
                "UPDATE broadcasts SET status='running' WHERE id=?",
                (broadcast_id,),
            )
            return broadcast_id

    def _pending_count(self, broadcast_id: int) -> int:
        with self._connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM broadcast_recipients "
                "WHERE broadcast_id=? AND status='pending'",
                (broadcast_id,),
            ).fetchone()[0]

    def _audience_count(self, audience: str) -> int:
        with self._connect() as db:
            if audience == "recent":
                cutoff = datetime.fromtimestamp(
                    time.time() - 30 * 86400, timezone.utc
                ).isoformat()
                return db.execute(
                    "SELECT COUNT(*) FROM users WHERE active=1 AND last_seen >= ?",
                    (cutoff,),
                ).fetchone()[0]
            return db.execute(
                "SELECT COUNT(*) FROM users WHERE active=1"
            ).fetchone()[0]

    def _set_broadcast_status(self, broadcast_id: int, status: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE broadcasts SET status=? WHERE id=?",
                (status, broadcast_id),
            )

    async def _deliver_broadcast(self, broadcast_id: int, bot, status_message) -> None:
        with self._connect() as db:
            broadcast = db.execute(
                "SELECT * FROM broadcasts WHERE id=?", (broadcast_id,)
            ).fetchone()
            recipients = [
                row["user_id"] for row in db.execute(
                    "SELECT user_id FROM broadcast_recipients "
                    "WHERE broadcast_id=? AND status='pending' ORDER BY user_id",
                    (broadcast_id,),
                )
            ]
        sent = failed = blocked = 0
        try:
            for index, user_id in enumerate(recipients, start=1):
                attempts = 0
                while True:
                    try:
                        await self._send_broadcast_message(bot, broadcast, user_id)
                        self._set_recipient(broadcast_id, user_id, "sent")
                        sent += 1
                        break
                    except RetryAfter as error:
                        attempts += 1
                        if attempts > 3:
                            self._set_recipient(
                                broadcast_id, user_id, "failed", str(error)[:300]
                            )
                            failed += 1
                            break
                        await asyncio.sleep(float(error.retry_after) + 0.5)
                    except Forbidden as error:
                        self._set_recipient(
                            broadcast_id, user_id, "blocked", str(error)[:300]
                        )
                        self._deactivate_user(user_id)
                        blocked += 1
                        break
                    except TelegramError as error:
                        self._set_recipient(
                            broadcast_id, user_id, "failed", str(error)[:300]
                        )
                        failed += 1
                        logger.warning(
                            "Broadcast #%d failed for user %d: %s",
                            broadcast_id, user_id, error,
                        )
                        break
                if index % 20 == 0:
                    await self._edit_progress(
                        status_message, broadcast_id, sent, failed, blocked, index,
                        len(recipients),
                    )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self._set_broadcast_status(broadcast_id, "cancelled")
            await self._edit_progress(
                status_message, broadcast_id, sent, failed, blocked,
                sent + failed + blocked, len(recipients), cancelled=True,
            )
            raise
        status = "completed" if not self._pending_count(broadcast_id) else "partial"
        self._set_broadcast_status(broadcast_id, status)
        await self._edit_progress(
            status_message, broadcast_id, sent, failed, blocked,
            len(recipients), len(recipients), finished=True,
        )

    @staticmethod
    def _rich_text_with_custom_emoji(value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(
            r"\{([0-9]+)\}",
            r'<tg-emoji emoji-id="\1">✨</tg-emoji>',
            value,
        )

    async def _send_broadcast_message(self, bot, broadcast, chat_id: int) -> None:
        def value(name: str, default=None):
            try:
                return broadcast[name]
            except (IndexError, KeyError, TypeError):
                return default

        raw_buttons = value("buttons_json")
        if raw_buttons is None:
            raw_buttons = json.dumps(value("buttons", []), ensure_ascii=False)
        buttons = json.loads(raw_buttons or "[]")
        markup = {"inline_keyboard": buttons} if buttons else None
        options = {
            "chat_id": chat_id,
            "disable_notification": bool(value("silent", False)),
            "protect_content": bool(value("protect_content", False)),
        }
        if value("content_type") == "text":
            send_options = dict(options)
            send_options.update(
                text=self._rich_text_with_custom_emoji(value("rich_text")) or "",
                parse_mode="HTML",
            )
            if markup:
                send_options["reply_markup"] = json.dumps(
                    markup, ensure_ascii=False
                )
            await call_api(
                bot,
                "sendMessage",
                **send_options,
            )
            return
        copy_options = {
            "chat_id": chat_id,
            "from_chat_id": value("source_chat_id"),
            "message_id": value("source_message_id"),
            "disable_notification": bool(value("silent", False)),
            "protect_content": bool(value("protect_content", False)),
        }
        rich_text = value("rich_text")
        if rich_text:
            copy_options["caption"] = self._rich_text_with_custom_emoji(rich_text)
            copy_options["parse_mode"] = "HTML"
        if markup:
            copy_options["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        await bot.copy_message(**copy_options)

    def _set_recipient(
        self, broadcast_id: int, user_id: int, status: str, error: str | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE broadcast_recipients SET status=?, error=? "
                "WHERE broadcast_id=? AND user_id=?",
                (status, error, broadcast_id, user_id),
            )

    def _deactivate_user(self, user_id: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))

    async def _edit_progress(
        self, message, broadcast_id: int, sent: int, failed: int, blocked: int,
        done: int, total: int, *, finished: bool = False, cancelled: bool = False,
    ) -> None:
        state = "cancelled" if cancelled else "complete" if finished else "sending"
        text = (
            f"Broadcast #{broadcast_id} {state}: "
            f"{done}/{total} processed · {sent} sent · "
            f"{failed} failed · {blocked} unreachable"
        )
        try:
            await message.edit_text(text)
        except TelegramError as error:
            logger.warning("Could not update broadcast progress: %s", error)

    async def broadcast_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        message = update.effective_message
        if not message:
            return
        if not await self._require_owner_private(update):
            return
        text, markup = self._broadcast_status_view()
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    def _broadcast_status_view(self) -> tuple[str, InlineKeyboardMarkup | None]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT b.id, b.status,
                    SUM(r.status='sent') AS sent,
                    SUM(r.status='failed') AS failed,
                    SUM(r.status='blocked') AS blocked,
                    SUM(r.status='pending') AS pending
                FROM broadcasts b
                LEFT JOIN broadcast_recipients r ON r.broadcast_id=b.id
                GROUP BY b.id ORDER BY b.id DESC LIMIT 10
                """
            ).fetchall()
        if not rows:
            return "<b>Broadcast status</b>\n\nNo broadcasts have been run.", None
        text_lines = ["<b>Recent Broadcasts</b>\n"]
        action_rows = []
        for row in rows:
            text_lines.append(
                f"Broadcast {row['id']} is {escape(row['status'])}, with "
                f"{row['sent'] or 0} sent, {row['failed'] or 0} failed, "
                f"{row['blocked'] or 0} unreachable and "
                f"{row['pending'] or 0} pending."
            )
            if row["status"] in ("running", "queued"):
                action_rows.append([
                    InlineKeyboardButton(
                        f"Stop #{row['id']}",
                        callback_data=f"owner:broadcast:cancel_prompt:{row['id']}",
                        style=KeyboardButtonStyle.DANGER,
                    )
                ])
            elif row["status"] in ("interrupted", "cancelled", "partial"):
                action_rows.append([
                    InlineKeyboardButton(
                        f"Resume #{row['id']}",
                        callback_data=f"owner:broadcast:resume_id:{row['id']}",
                        style=KeyboardButtonStyle.SUCCESS,
                    )
                ])
        action_rows.append([
            InlineKeyboardButton(
                "Compose broadcast",
                callback_data="owner:broadcast:start",
                style=KeyboardButtonStyle.PRIMARY,
            )
        ])
        return "\n".join(text_lines), InlineKeyboardMarkup(action_rows)

    async def broadcast_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user, message = update.effective_user, update.effective_message
        if not message:
            return
        if not await self._require_owner_private(update):
            return
        if not context.args or not context.args[0].isdigit():
            await message.reply_text("Usage: /broadcast_cancel <id>")
            return
        broadcast_id = int(context.args[0])
        task = self.broadcast_tasks.get(broadcast_id)
        if not task or task.done():
            await message.reply_text("That broadcast is not currently running.")
            return
        task.cancel()
        await message.reply_text(f"Cancellation requested for broadcast #{broadcast_id}.")

    async def broadcast_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user, message = update.effective_user, update.effective_message
        if not message:
            return
        if not await self._require_owner_private(update):
            return
        if not context.args or not context.args[0].isdigit():
            await message.reply_text("Usage: /broadcast_resume <id>")
            return
        broadcast_id = int(context.args[0])
        status_message = await message.reply_text(
            f"Resuming broadcast #{broadcast_id}…"
        )
        if not self._queue_broadcast_resume(
            broadcast_id, context.bot, status_message
        ):
            await status_message.edit_text(
                "That broadcast is already running or cannot be resumed."
            )

    def _queue_broadcast_resume(self, broadcast_id: int, bot, status_message) -> bool:
        if broadcast_id in self.broadcast_tasks:
            return False
        with self._connect() as db:
            db.execute(
                "UPDATE broadcast_recipients SET status='pending', error=NULL "
                "WHERE broadcast_id=? AND status='failed'",
                (broadcast_id,),
            )
            exists = db.execute(
                "SELECT 1 FROM broadcasts WHERE id=?", (broadcast_id,)
            ).fetchone()
            if exists:
                db.execute(
                    "UPDATE broadcasts SET status='running' WHERE id=?",
                    (broadcast_id,),
                )
        if not exists:
            return False
        if self._pending_count(broadcast_id) == 0:
            self._set_broadcast_status(broadcast_id, "completed")
            return False
        task = asyncio.create_task(
            self._deliver_broadcast(broadcast_id, bot, status_message)
        )
        self.broadcast_tasks[broadcast_id] = task
        task.add_done_callback(
            lambda completed, key=broadcast_id: self._broadcast_done(key, completed)
        )
        return True

    async def cancel_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user, message = update.effective_user, update.effective_message
        if not message or not self.is_owner(user.id if user else None):
            return
        if not update.effective_chat or update.effective_chat.type != "private":
            return
        if self.awaiting.pop(self.owner_id, None):
            await message.reply_text("Owner action cancelled.")
        else:
            await message.reply_text("No owner action is pending.")

    async def stop_broadcasts(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user, message, chat = (
            update.effective_user, update.effective_message, update.effective_chat
        )
        if not user or not message or not chat or chat.type != "private":
            return
        self.set_user_active(user.id, False)
        await message.reply_text(
            "You will no longer receive broadcasts. Send /start to opt back in."
        )

    async def start_broadcasts(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user, chat = update.effective_user, update.effective_chat
        if user and chat and chat.type == "private":
            self.set_user_active(user.id, True)

    async def close(self) -> None:
        tasks = list(self.broadcast_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
