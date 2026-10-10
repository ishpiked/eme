import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, TelegramError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as emy_bot
from features import (
    CUSTOM_EMOJI_IDS,
    CUSTOM_EMOJI_PACK_NAMES,
    custom_button,
    ephemeral_message_target,
    resolve_custom_emoji_packs,
    rich_custom_emoji,
    send_ephemeral_message,
    send_rich_message,
    stream_rich_response,
    stream_text_response,
)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def do_api_request(self, method, api_kwargs):
        self.calls.append((method, api_kwargs))
        if method == "getStickerSet":
            return {
                "stickers": [
                    {"emoji": "🎵", "custom_emoji_id": "123"},
                    {"emoji": "🎵", "custom_emoji_id": "456"},
                    {"emoji": "✨", "custom_emoji_id": "789"},
                ]
            }
        if method == "setMyCommands":
            self.commands = api_kwargs["commands"]
            return {"ok": True, "result": True}
        if method == "getMyCommands":
            return {"ok": True, "result": self.commands}
        return {"ok": True}

    async def get_sticker_set(self, name):
        self.calls.append(("getStickerSet", {"name": name}))
        return SimpleNamespace(stickers=[
            SimpleNamespace(emoji="🎵", custom_emoji_id="123"),
            SimpleNamespace(emoji="🎵", custom_emoji_id="456"),
            SimpleNamespace(emoji="✨", custom_emoji_id="789"),
        ])


class FakeProfileBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.profile_photo = None
        self.commands = []

    async def set_my_description(self, description):
        self.calls.append(("setMyDescription", {"description": description}))

    async def set_my_short_description(self, description):
        self.calls.append(("setMyShortDescription", {"short_description": description}))

    async def set_my_commands(self, commands, scope):
        self.commands = commands
        self.calls.append((
            "setMyCommands",
            {"commands": commands, "scope": scope.to_dict()},
        ))

    async def get_my_commands(self, scope):
        self.calls.append(("getMyCommands", {"scope": scope.to_dict()}))
        return self.commands

    async def do_api_request(self, method, api_kwargs):
        self.calls.append((method, api_kwargs))
        if method == "setMyProfilePhoto":
            self.profile_photo = api_kwargs
        return {"ok": True, "result": True}


class FakeSongMenuBot:
    def __init__(self):
        self.messages = []
        self.actions = []
        self.audio_messages = []
        self.edited_media = []
        self.deleted_messages = []
        self.next_message_id = 77

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return FakeStatusMessage()

    async def send_chat_action(self, *args, **kwargs):
        self.actions.append((args, kwargs))

    async def send_audio(self, *args, **kwargs):
        self.audio_messages.append((args, kwargs))
        return SimpleNamespace(
            message_id=self.next_message_id,
            audio=SimpleNamespace(file_id="replacement-file-id"),
        )

    async def edit_message_media(self, *args, **kwargs):
        self.edited_media.append((args, kwargs))
        return SimpleNamespace(
            message_id=kwargs["message_id"],
            audio=SimpleNamespace(file_id="replacement-file-id"),
        )

    async def edit_message_reply_markup(self, *args, **kwargs):
        return None

    async def delete_message(self, *args, **kwargs):
        self.deleted_messages.append((args, kwargs))


class FakeStatusMessage:
    async def delete(self):
        return None

    async def edit_text(self, *args, **kwargs):
        return None


class TelegramFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_local_api_endpoint_configuration_is_validated_and_applied(self):
        self.assertIsNone(emy_bot.local_api_urls(""))
        self.assertEqual(
            emy_bot.local_api_urls("http://127.0.0.1:8081/"),
            (
                "http://127.0.0.1:8081/bot",
                "http://127.0.0.1:8081/file/bot",
            ),
        )
        for invalid_url in (
            "ftp://127.0.0.1:8081",
            "http://user:pass@127.0.0.1:8081",
            "http://127.0.0.1:8081/bot",
            "http://127.0.0.1:8081?token=secret",
        ):
            with self.subTest(invalid_url=invalid_url):
                with self.assertRaises(ValueError):
                    emy_bot.local_api_urls(invalid_url)

        builder = Mock()
        builder.base_url.return_value = builder
        builder.base_file_url.return_value = builder
        builder.local_mode.return_value = builder
        self.assertIs(
            emy_bot.configure_local_api(builder, "http://127.0.0.1:8081"),
            builder,
        )
        builder.base_url.assert_called_once_with("http://127.0.0.1:8081/bot")
        builder.base_file_url.assert_called_once_with(
            "http://127.0.0.1:8081/file/bot"
        )
        builder.local_mode.assert_called_once_with(True)

    async def test_private_text_is_searched_directly_but_group_text_is_ignored(self):
        bot = FakeSongMenuBot()
        context = SimpleNamespace(bot=bot, application=SimpleNamespace())
        search = AsyncMock()

        with patch.object(emy_bot, "song", search):
            private_update = SimpleNamespace(
                effective_message=SimpleNamespace(text="Isq Risk"),
                effective_chat=SimpleNamespace(type="private", id=7),
                effective_user=SimpleNamespace(id=7),
            )
            await emy_bot.on_direct_private_text(private_update, context)

            group_update = SimpleNamespace(
                effective_message=SimpleNamespace(text="Isq Risk"),
                effective_chat=SimpleNamespace(type="group", id=-100),
                effective_user=SimpleNamespace(id=7),
            )
            await emy_bot.on_direct_private_text(group_update, context)
            private_url_update = SimpleNamespace(
                effective_message=SimpleNamespace(
                    text="https://www.youtube.com/watch?v=abc"
                ),
                effective_chat=SimpleNamespace(type="private", id=7),
                effective_user=SimpleNamespace(id=7),
            )
            await emy_bot.on_direct_private_text(private_url_update, context)
            own_bot_update = SimpleNamespace(
                effective_message=SimpleNamespace(text="Do not search this"),
                effective_chat=SimpleNamespace(type="private", id=7),
                effective_user=SimpleNamespace(id=99, is_bot=True),
            )
            await emy_bot.on_direct_private_text(own_bot_update, context)

        search.assert_awaited_once()
        args, _ = search.await_args
        self.assertEqual(args[0], private_update)
        self.assertEqual(args[1].args, ["Isq Risk"])

    async def test_group_pending_name_edit_updates_song(self):
        chat_id, msg_id = -100, 61
        session_key = (chat_id, msg_id)
        emy_bot.SESSIONS[session_key] = {
            "video_id": "group-edit-test",
            "file_id": "old-file-id",
            "original_audio_bytes": b"original-audio",
            "applied_effect": "off",
            "title": "Old title",
            "base_title": "Old title",
            "uploader": "Artist",
            "duration": 120,
        }
        emy_bot.PENDING[7] = {
            "op": "name", "chat_id": chat_id, "msg_id": msg_id,
            "expires": 9999999999,
        }
        bot = FakeSongMenuBot()
        update = SimpleNamespace(
            message=SimpleNamespace(text="New Group Title"),
            effective_message=SimpleNamespace(text="New Group Title"),
            effective_chat=SimpleNamespace(type="group", id=chat_id),
            effective_user=SimpleNamespace(id=7, is_bot=False),
        )
        try:
            await emy_bot.on_direct_private_text(
                update, SimpleNamespace(bot=bot, application=SimpleNamespace())
            )
            self.assertEqual(
                emy_bot.SESSIONS[session_key]["title"], "New Group Title"
            )
            self.assertNotIn(7, emy_bot.PENDING)
        finally:
            emy_bot.SESSIONS.pop(session_key, None)
            emy_bot.PENDING.pop(7, None)
            if session_key in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove(session_key)

    async def test_pending_thumbnail_updates_cover(self):
        chat_id, msg_id = -100, 62
        session_key = (chat_id, msg_id)
        emy_bot.SESSIONS[session_key] = {
            "video_id": "thumb-edit-test",
            "file_id": "old-file-id",
            "original_audio_bytes": b"original-audio",
            "applied_effect": "off",
            "title": "Song",
            "base_title": "Song",
            "uploader": "Artist",
            "duration": 120,
        }
        emy_bot.PENDING[7] = {
            "op": "thumb", "chat_id": chat_id, "msg_id": msg_id,
            "expires": 9999999999,
        }
        fake_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"img"))
        )
        bot = SimpleNamespace(
            get_file=AsyncMock(return_value=fake_file),
            edit_message_media=AsyncMock(return_value=SimpleNamespace(
                audio=SimpleNamespace(file_id="new-file-id")
            )),
        )
        message = SimpleNamespace(photo=[SimpleNamespace(file_id="photo-1")])
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=chat_id),
            effective_user=SimpleNamespace(id=7),
        )
        try:
            with patch.object(
                emy_bot, "_convert_thumbnail_jpeg", return_value=b"cover"
            ):
                await emy_bot.on_pending_photo(update, SimpleNamespace(bot=bot))
            self.assertEqual(
                emy_bot.SESSIONS[session_key]["cover_bytes"], b"cover"
            )
            self.assertNotIn(7, emy_bot.PENDING)
        finally:
            emy_bot.SESSIONS.pop(session_key, None)
            emy_bot.PENDING.pop(7, None)
            if session_key in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove(session_key)

    async def test_edit_updates_audio_metadata_on_the_same_message(self):
        chat_id, old_message_id = -100, 55
        session_key = (chat_id, old_message_id)
        emy_bot.SESSIONS[session_key] = {
            "video_id": "edit-base-test",
            "file_id": "original-file-id",
            "original_audio_bytes": b"untouched-audio",
            "applied_effect": "off",
            "title": "Old title",
            "base_title": "Old title",
            "uploader": "Artist",
            "duration": 120,
        }
        bot = FakeSongMenuBot()
        pending = {"chat_id": chat_id, "msg_id": old_message_id}

        try:
            await emy_bot._send_edited_song(
                SimpleNamespace(bot=bot),
                pending,
                emy_bot.SESSIONS[session_key],
                title="New title",
                artist="New artist",
            )
            self.assertFalse(bot.audio_messages)
            self.assertEqual(len(bot.edited_media), 1)
            args, kwargs = bot.edited_media[0]
            self.assertEqual(kwargs["chat_id"], chat_id)
            self.assertEqual(kwargs["message_id"], old_message_id)
            self.assertNotEqual(kwargs["media"].media, "original-file-id")
            self.assertEqual(kwargs["media"].title, "New title")
            self.assertEqual(kwargs["media"].performer, "New artist")
            self.assertFalse(bot.deleted_messages)
            self.assertEqual(emy_bot.SESSIONS[session_key]["title"], "New title")
            self.assertEqual(
                emy_bot.SESSIONS[session_key]["file_id"], "replacement-file-id"
            )
        finally:
            emy_bot.SESSIONS.pop(session_key, None)
            if session_key in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove(session_key)

    async def test_edited_media_links_uploads_with_attach_references(self):
        chat_id, old_message_id = -100, 63
        session_key = (chat_id, old_message_id)
        emy_bot.SESSIONS[session_key] = {
            "video_id": "attach-test",
            "file_id": "original-file-id",
            "original_audio_bytes": b"untouched-audio",
            "applied_effect": "off",
            "title": "Old title",
            "base_title": "Old title",
            "uploader": "Artist",
            "duration": 120,
        }
        bot = FakeSongMenuBot()
        pending = {"chat_id": chat_id, "msg_id": old_message_id}
        try:
            await emy_bot._send_edited_song(
                SimpleNamespace(bot=bot),
                pending,
                emy_bot.SESSIONS[session_key],
                title="New title",
                artist="New artist",
                cover_bytes=b"cover-bytes",
            )
            self.assertEqual(len(bot.edited_media), 1)
            media = bot.edited_media[0][1]["media"]
            self.assertTrue(
                media.media.attach_uri.startswith("attach://")
            )
            self.assertTrue(
                media.thumbnail.attach_uri.startswith("attach://")
            )
        finally:
            emy_bot.SESSIONS.pop(session_key, None)
            if session_key in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove(session_key)

    async def test_switching_effects_always_renders_from_original_and_edits_in_place(self):
        chat_id, message_id = -100, 56
        session_key = (chat_id, message_id)
        emy_bot.SESSIONS[session_key] = {
            "video_id": "effect-base-test",
            "title": "Song (8D)",
            "base_title": "Song",
            "applied_effect": "8d",
            "uploader": "Artist",
            "duration": 120,
            "file_id": "8d-file",
            "original_audio_bytes": b"original-audio",
            "cover_bytes": b"cover",
        }
        bot = FakeSongMenuBot()
        context = SimpleNamespace(bot=bot)
        rendered_inputs = []

        async def fake_apply_effect(audio, effect, cover):
            rendered_inputs.append((audio, effect, cover))
            return f"{effect}-audio".encode()

        try:
            with patch.object(emy_bot, "apply_effect", fake_apply_effect), patch.object(
                emy_bot, "cache_store"
            ):
                await emy_bot.run_effect(
                    None, context, chat_id, message_id,
                    emy_bot.SESSIONS[session_key], "concert",
                    emy_bot.EFFECTS["concert"],
                )
                self.assertEqual(
                    emy_bot.SESSIONS[session_key]["applied_effect"], "concert"
                )
                await emy_bot.run_effect(
                    None, context, chat_id, message_id,
                    emy_bot.SESSIONS[session_key], "vinyl",
                    emy_bot.EFFECTS["vinyl"],
                )
                vinyl_markup = bot.edited_media[-1][1]["reply_markup"]
                self.assertTrue(any(
                    button["text"] == "Original"
                    for row in vinyl_markup["inline_keyboard"]
                    for button in row
                ))
                await emy_bot.run_effect(
                    None, context, chat_id, message_id,
                    emy_bot.SESSIONS[session_key], "off",
                    {"label": "Original", "suffix": ""},
                )

            self.assertEqual(
                rendered_inputs,
                [
                    (b"original-audio", "concert", b"cover"),
                    (b"original-audio", "vinyl", b"cover"),
                ],
            )
            self.assertEqual(len(bot.edited_media), 3)
            self.assertEqual(
                [call[1]["message_id"] for call in bot.edited_media],
                [message_id, message_id, message_id],
            )
            self.assertFalse(bot.audio_messages)
            self.assertEqual(
                emy_bot.SESSIONS[session_key]["applied_effect"], "off"
            )
            self.assertEqual(emy_bot.SESSIONS[session_key]["title"], "Song")
            self.assertEqual(
                emy_bot.SESSIONS[session_key]["original_audio_bytes"],
                b"original-audio",
            )
        finally:
            emy_bot.SESSIONS.pop(session_key, None)
            if session_key in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove(session_key)

    async def test_search_results_follow_catalog_order_and_cache(self):
        query = "Catalog Order Probe Song"
        candidates = [
            {"id": f"song-{index}", "track": f"Song {index}", "artist": "Artist"}
            for index in range(10)
        ]
        application = SimpleNamespace(bot_data={})
        try:
            with patch.object(
                emy_bot.ytmusic, "search",
                return_value=[dict(entry) for entry in candidates],
            ) as catalog:
                first = await emy_bot.resolve_candidates_async(
                    query, application, limit=10
                )
                second = await emy_bot.resolve_candidates_async(
                    query, application, limit=10
                )
            self.assertEqual(
                [entry["id"] for entry in first],
                [f"song-{index}" for index in range(10)],
            )
            self.assertEqual(
                [entry["id"] for entry in second],
                [f"song-{index}" for index in range(10)],
            )
            catalog.assert_called_once_with(query, 10)
        finally:
            emy_bot.SEARCH_RESULT_CACHE.pop(
                emy_bot.ytmusic.normalize_query(query), None
            )

    async def test_small_cache_refetches_for_bigger_limits(self):
        query = "Small Cache Refetch Probe"
        small = [{"id": "only", "track": "Only", "artist": "Artist"}]
        big = [
            {"id": f"big-{index}", "track": f"Big {index}", "artist": "Artist"}
            for index in range(25)
        ]
        application = SimpleNamespace(bot_data={})
        try:
            with patch.object(
                emy_bot.ytmusic, "search", side_effect=[small, big]
            ) as catalog:
                first = await emy_bot.resolve_candidates_async(
                    query, application, limit=10
                )
                second = await emy_bot.resolve_candidates_async(
                    query, application, limit=10
                )
            self.assertEqual([entry["id"] for entry in first], ["only"])
            self.assertEqual(len(second), 10)
            self.assertEqual(catalog.call_count, 2)
        finally:
            emy_bot.SEARCH_RESULT_CACHE.pop(
                emy_bot.ytmusic.normalize_query(query), None
            )

    async def test_inline_video_link_offers_direct_download(self):
        url = "https://www.youtube.com/watch?v=abc"
        inline_query = SimpleNamespace(
            query=url,
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=inline_query)
        context = SimpleNamespace(
            bot=SimpleNamespace(id=999), application=SimpleNamespace()
        )
        with patch.object(
            emy_bot, "resolve_candidates_async", new=AsyncMock()
        ) as resolve:
            await emy_bot.inline_search(update, context)
        resolve.assert_not_awaited()
        inline_query.answer.assert_awaited_once()
        args, _ = inline_query.answer.await_args
        self.assertEqual(len(args[0]), 1)
        result = args[0][0]
        self.assertEqual(result.id, "link-preview")
        self.assertEqual(result.title, "Download audio")
        self.assertEqual(
            result.input_message_content.message_text,
            f"/play {url} --no-controls",
        )

    async def test_inline_music_link_offers_direct_play(self):
        url = "https://music.youtube.com/watch?v=ODH_e0rOuyQ"
        inline_query = SimpleNamespace(
            query=url,
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=inline_query)
        context = SimpleNamespace(
            bot=SimpleNamespace(id=999), application=SimpleNamespace()
        )
        with patch.object(
            emy_bot, "resolve_candidates_async", new=AsyncMock()
        ) as resolve:
            await emy_bot.inline_search(update, context)
        resolve.assert_not_awaited()
        inline_query.answer.assert_awaited_once()
        args, _ = inline_query.answer.await_args
        self.assertEqual(len(args[0]), 1)
        result = args[0][0]
        self.assertEqual(result.id, "link-preview")
        self.assertEqual(result.title, "Play this song")
        self.assertEqual(
            result.input_message_content.message_text,
            f"/play {url} --no-controls",
        )

    async def test_inline_unsupported_link_answers_empty(self):
        inline_query = SimpleNamespace(
            query="https://example.com/video",
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=inline_query)
        context = SimpleNamespace(
            bot=SimpleNamespace(id=999), application=SimpleNamespace()
        )
        with patch.object(
            emy_bot, "resolve_candidates_async", new=AsyncMock()
        ) as resolve:
            await emy_bot.inline_search(update, context)
        resolve.assert_not_awaited()
        inline_query.answer.assert_awaited_once_with(
            [], cache_time=15, is_personal=True
        )

    async def test_inline_text_query_resolves_songs(self):
        inline_query = SimpleNamespace(
            query="Kesariya",
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=inline_query)
        context = SimpleNamespace(
            bot=SimpleNamespace(id=999), application=SimpleNamespace()
        )
        candidates = [{
            "id": "vid-1", "track": "Kesariya", "artist": "Arijit Singh",
            "duration": 262, "thumbnail": None,
        }]
        with patch.object(
            emy_bot, "resolve_candidates_async",
            new=AsyncMock(return_value=candidates),
        ) as resolve:
            await emy_bot.inline_search(update, context)
        resolve.assert_awaited_once_with(
            "Kesariya", context.application, limit=10
        )
        inline_query.answer.assert_awaited_once()
        args, _ = inline_query.answer.await_args
        self.assertEqual(len(args[0]), 1)
        self.assertIn("Kesariya", args[0][0].title)
        self.assertEqual(
            args[0][0].input_message_content.message_text,
            "/play https://music.youtube.com/watch?v=vid-1 --no-controls",
        )

    async def test_search_results_show_ten_tracks_in_two_columns(self):
        selection = {
            "page": 0,
            "candidates": [
                {"id": f"id-{index}", "track": f"Song {index}", "artist": "Artist"}
                for index in range(12)
            ],
        }
        markup = emy_bot.search_results_keyboard("token", selection)
        rows = markup["inline_keyboard"]

        self.assertEqual(len(rows), 6)
        self.assertEqual(
            [button["callback_data"] for row in rows[:5] for button in row],
            [f"searchpick:token:{index}" for index in range(10)],
        )
        self.assertTrue(all(len(row) == 2 for row in rows[:5]))
        self.assertTrue(all(
            button["style"] == "primary"
            for row in rows[:5] for button in row
        ))
        self.assertEqual(
            [button["text"] for button in rows[-1]],
            ["Previous", "Close", "Next"],
        )
        self.assertEqual(rows[-1][0]["style"], "success")
        self.assertEqual(rows[-1][1]["style"], "danger")
        self.assertEqual(rows[-1][2]["style"], "success")
        self.assertEqual(
            rows[-1][0]["icon_custom_emoji_id"], "5258236805890710909"
        )
        self.assertEqual(
            rows[-1][2]["icon_custom_emoji_id"], "5260450573768990626"
        )
        self.assertEqual(
            emy_bot.search_results_text(selection),
            '<tg-emoji emoji-id="5258289810082111221">🎵</tg-emoji> '
            "<b>Results</b> (Page 1 of 2 · 12 found)",
        )
        second_rows = emy_bot.search_results_keyboard(
            "token", dict(selection, page=1)
        )["inline_keyboard"]
        self.assertEqual(len(second_rows), 2)
        self.assertEqual(
            [button["callback_data"] for button in second_rows[0]],
            ["searchpick:token:10", "searchpick:token:11"],
        )

    async def test_song_query_always_shows_picker_before_download(self):
        class SearchStatus:
            def __init__(self):
                self.edits = []

            async def edit_text(self, text, **kwargs):
                self.edits.append((text, kwargs))

        status = SearchStatus()
        message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=42),
        )
        candidates = [
            {
                "id": f"id-{index}",
                "track": f"Song {index}",
                "artist": "Artist",
                "_relevance_score": 100,
            }
            for index in range(7)
        ]
        context = SimpleNamespace(
            args=["Song"],
            application=SimpleNamespace(),
            bot=FakeSongMenuBot(),
        )
        existing_tokens = set(emy_bot.SEARCH_SELECTIONS)
        with patch.object(
            emy_bot,
            "resolve_candidates_async",
            new=AsyncMock(return_value=candidates),
        ) as resolve, patch.object(
            emy_bot,
            "download_song_to_ram",
        ) as download:
            await emy_bot.song(update, context)

        resolve.assert_awaited_once_with("Song", context.application, limit=50)
        download.assert_not_called()
        self.assertEqual(len(status.edits), 1)
        self.assertEqual(
            status.edits[0][0],
            '<tg-emoji emoji-id="5258289810082111221">🎵</tg-emoji> '
            '<b>Results for "Song"</b> (Page 1 of 1 · 7 found)',
        )
        self.assertEqual(status.edits[0][1].get("parse_mode"), "HTML")
        self.assertEqual(
            len(status.edits[0][1]["reply_markup"]["inline_keyboard"][:-1]),
            4,
        )
        new_tokens = set(emy_bot.SEARCH_SELECTIONS) - existing_tokens
        self.assertEqual(len(new_tokens), 1)
        emy_bot.SEARCH_SELECTIONS.pop(new_tokens.pop())

    async def test_search_navigation_changes_page_and_close_removes_selection(self):
        token = "page-token"
        selection = {
            "requester_id": 42,
            "expires": 9999999999,
            "processing": False,
            "page": 0,
            "query": "Song",
            "candidates": [
                {"id": f"id-{index}", "track": f"Song {index}", "artist": "Artist"}
                for index in range(12)
            ],
        }
        emy_bot.SEARCH_SELECTIONS[token] = selection
        query = SimpleNamespace(
            data=f"searchpage:{token}:1",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await emy_bot._search_navigation_callback(query)

        self.assertEqual(selection["page"], 1)
        query.edit_message_text.assert_awaited_once()
        self.assertEqual(
            query.edit_message_text.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            f"searchpick:{token}:10",
        )

        close_query = SimpleNamespace(
            data=f"searchclose:{token}",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            delete_message=AsyncMock(),
        )
        await emy_bot._search_navigation_callback(close_query)
        close_query.delete_message.assert_awaited_once()
        self.assertNotIn(token, emy_bot.SEARCH_SELECTIONS)

    async def test_ephemeral_message_uses_current_bot_api_parameters(self):
        bot = FakeBot()
        await send_ephemeral_message(
            bot, -1001, 42, "Private reply", parse_mode="HTML"
        )
        self.assertEqual(bot.calls, [(
            "sendMessage",
            {
                "chat_id": -1001,
                "text": "Private reply",
                "ephemeral_message_parameters": {"receiver_user_id": 42},
                "parse_mode": "HTML",
            },
        )])

    async def test_rich_message_payload_is_forwarded_without_conversion(self):
        bot = FakeBot()
        payload = {
            "html": (
                "<h1>Heading</h1><details><summary>More</summary>"
                "<tg-spoiler>Hidden</tg-spoiler></details>"
                "<tg-math-block>x^2</tg-math-block><pre><code>code</code></pre>"
            )
        }
        await send_rich_message(bot, 42, payload, disable_notification=True)
        self.assertEqual(bot.calls, [(
            "sendRichMessage",
            {
                "chat_id": 42,
                "rich_message": payload,
                "disable_notification": True,
            },
        )])

    async def test_custom_emoji_and_button_use_verified_bot_api_fields(self):
        emoji = rich_custom_emoji(CUSTOM_EMOJI_IDS["music"], "🎵")
        button = custom_button(
            "Play", "play", icon_custom_emoji_id=CUSTOM_EMOJI_IDS["music"],
            style="success",
        )
        self.assertEqual(emoji, {
            "type": "custom_emoji",
            "custom_emoji_id": "5258289810082111221",
            "alternative_text": "🎵",
        })
        self.assertEqual(button["icon_custom_emoji_id"], emoji["custom_emoji_id"])
        self.assertEqual(button["style"], "success")
        with self.assertRaises(ValueError):
            custom_button("Play", "play", style="purple")

    async def test_pack_resolver_reads_ids_from_telegram_response(self):
        bot = FakeBot()
        resolved = await resolve_custom_emoji_packs(bot)
        self.assertEqual(tuple(resolved), CUSTOM_EMOJI_PACK_NAMES)
        self.assertEqual(resolved["aicreate"]["🎵"], ["123", "456"])
        self.assertEqual(len(bot.calls), len(CUSTOM_EMOJI_PACK_NAMES))
        self.assertTrue(all(method == "getStickerSet" for method, _ in bot.calls))

    async def test_ephemeral_callback_target_uses_received_message_fields(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            api_kwargs={
                "receiver_user": {"id": 42},
                "ephemeral_message_id": 81,
            },
        )
        self.assertEqual(ephemeral_message_target(message), (-1001, 42, 81))

    async def test_group_start_uses_a_private_ephemeral_reply(self):
        bot = FakeBot()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1001, type="group"),
            effective_user=SimpleNamespace(id=42),
            message=None,
        )
        await emy_bot.start(update, SimpleNamespace(bot=bot))
        method, payload = bot.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(
            payload["ephemeral_message_parameters"], {"receiver_user_id": 42}
        )
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["reply_markup"], emy_bot.welcome_keyboard())

    async def test_welcome_keyboard_links_update_channel(self):
        rows = emy_bot.welcome_keyboard()["inline_keyboard"]
        self.assertEqual(len(rows), 1)
        button = rows[0][0]
        self.assertEqual(button["text"], "Update Channel")
        self.assertEqual(button["url"], "https://t.me/EndBots")
        self.assertEqual(button["style"], "success")

    async def test_help_sends_overview_with_channel_button(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_message=SimpleNamespace(text="/help"),
            effective_chat=SimpleNamespace(type="private", id=7),
            effective_user=SimpleNamespace(id=7),
        )
        await emy_bot.help_cmd(update, SimpleNamespace(bot=SimpleNamespace()))
        message.reply_text.assert_awaited_once()
        args, kwargs = message.reply_text.await_args
        self.assertEqual(args[0], emy_bot.HELP_HTML)
        self.assertIn("@Emeliyabot", args[0])
        self.assertNotIn("**", args[0])
        self.assertNotIn("—", args[0])
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertNotIn("reply_markup", kwargs)

    async def test_safe_answer_ignores_stale_queries(self):
        stale = AsyncMock(side_effect=BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        ))
        self.assertFalse(
            await emy_bot._safe_answer(SimpleNamespace(answer=stale))
        )
        fresh = AsyncMock()
        self.assertTrue(
            await emy_bot._safe_answer(
                SimpleNamespace(answer=fresh), "Hi", show_alert=True
            )
        )
        fresh.assert_awaited_once_with("Hi", show_alert=True)
        other = AsyncMock(side_effect=TelegramError("boom"))
        with self.assertRaises(TelegramError):
            await emy_bot._safe_answer(SimpleNamespace(answer=other))

    async def test_song_menu_buttons_are_arranged_in_three_rows(self):
        markup = emy_bot.main_menu(-1001, 12)
        rows = markup["inline_keyboard"]

        self.assertEqual(
            [[button["text"] for button in row] for row in rows],
            [["Edit", "Lyrics"], ["Effect", "Delete"], ["Playlist"]],
        )
        self.assertEqual(
            rows[0][1]["icon_custom_emoji_id"], "5258334469152054985"
        )
        self.assertEqual(
            rows[2][0]["icon_custom_emoji_id"], "5280867942056108177"
        )

    async def test_inline_song_menu_keeps_only_playlist_button(self):
        rows = emy_bot.song_menu(-1001, 12, playlist_only=True)["inline_keyboard"]
        self.assertEqual(
            [[button["text"] for button in row] for row in rows],
            [["Playlist"]],
        )
        self.assertEqual(
            rows[0][0]["callback_data"], "playlist:only"
        )
        self.assertEqual(
            rows[0][0]["icon_custom_emoji_id"], "5280867942056108177"
        )

    async def test_play_cmd_sends_video_link_to_preview_flow(self):
        url = "https://www.youtube.com/watch?v=abc"
        update = SimpleNamespace(
            message=SimpleNamespace(),
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        context = SimpleNamespace(
            args=[url], bot=SimpleNamespace(), application=SimpleNamespace()
        )
        with patch.object(
            emy_bot, "receive_recognition_input", new=AsyncMock()
        ) as preview:
            await emy_bot.play_cmd(update, context)
        preview.assert_awaited_once_with(update, context, media_url=url)

    async def test_play_cmd_routes_video_ids_to_preview_flow(self):
        update = SimpleNamespace(
            message=SimpleNamespace(),
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        context = SimpleNamespace(
            args=["dQw4w9WgXcQ"], bot=SimpleNamespace(),
            application=SimpleNamespace(),
        )
        with patch.object(
            emy_bot, "receive_recognition_input", new=AsyncMock()
        ) as preview:
            await emy_bot.play_cmd(update, context)
        preview.assert_awaited_once_with(
            update,
            context,
            media_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    async def test_play_cmd_sends_youtube_music_link_to_audio_flow(self):
        url = "https://music.youtube.com/watch?v=abcdefghijk"
        update = SimpleNamespace(
            message=SimpleNamespace(),
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        context = SimpleNamespace(
            args=[url], bot=SimpleNamespace(), application=SimpleNamespace()
        )
        with patch.object(emy_bot, "song", new=AsyncMock()) as deliver:
            await emy_bot.play_cmd(update, context)
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.args[1].args, [url])

    async def test_play_cmd_inline_option_suppresses_controls(self):
        url = "https://www.youtube.com/watch?v=abc"
        update = SimpleNamespace(
            message=SimpleNamespace(),
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        context = SimpleNamespace(
            args=[url, "--no-controls"],
            bot=SimpleNamespace(),
            application=SimpleNamespace(),
        )
        with patch.object(
            emy_bot, "song", new=AsyncMock()
        ) as deliver:
            await emy_bot.play_cmd(update, context)
        deliver.assert_awaited_once()
        delivery_context = deliver.await_args.args[1]
        self.assertEqual(delivery_context.args, [url])
        self.assertTrue(delivery_context.no_controls)

    async def test_play_cmd_rejects_missing_or_non_link_args(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        with patch.object(
            emy_bot, "song", new=AsyncMock()
        ) as deliver:
            await emy_bot.play_cmd(
                update, SimpleNamespace(args=[], bot=SimpleNamespace())
            )
            await emy_bot.play_cmd(
                update,
                SimpleNamespace(args=["just some words"], bot=SimpleNamespace()),
            )
        deliver.assert_not_awaited()
        self.assertEqual(message.reply_text.await_count, 2)

    async def test_song_applies_user_default_effect(self):
        emy_bot.USER_SETTINGS["42"] = {"effect": "8d"}
        meta = {
            "audio_bytes": b"plain",
            "cover_bytes": None,
            "size_mb": 1.0,
            "title": "Song",
            "uploader": "Artist",
            "duration": 120,
            "album": None,
            "thumbnail": None,
            "webpage_url": "https://www.youtube.com/watch?v=vid8d",
            "id": "vid8d",
        }
        status = SimpleNamespace(
            edit_text=AsyncMock(), delete=AsyncMock()
        )
        sent = SimpleNamespace(
            message_id=5, audio=SimpleNamespace(file_id="fid-8d")
        )
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            reply_audio=AsyncMock(return_value=sent),
            chat=SimpleNamespace(send_action=AsyncMock()),
        )
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=9),
        )
        context = SimpleNamespace(
            args=["https://www.youtube.com/watch?v=vid8d"],
            bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()),
            application=SimpleNamespace(),
        )
        memory_cache = dict(emy_bot.FILE_ID_CACHE)
        try:
            with patch.object(
                emy_bot, "download_song_to_ram", return_value=dict(meta)
            ), patch.object(
                emy_bot, "apply_effect", new=AsyncMock(return_value=b"fxbytes")
            ) as render, patch.object(
                emy_bot, "cache_store",
                side_effect=lambda key, entry: memory_cache.__setitem__(key, entry),
            ):
                saved, emy_bot.FILE_ID_CACHE = emy_bot.FILE_ID_CACHE, memory_cache
                try:
                    await emy_bot.song(update, context)
                finally:
                    emy_bot.FILE_ID_CACHE = saved
            render.assert_awaited_once()
            self.assertEqual(
                memory_cache["vid8d::8d"]["applied_effect"], "8d"
            )
            self.assertTrue(
                message.reply_audio.await_args.kwargs["title"].endswith(
                    emy_bot.EFFECTS["8d"]["suffix"]
                )
            )
        finally:
            emy_bot.USER_SETTINGS.pop("42", None)
            emy_bot.SESSIONS.pop((9, 5), None)
            if (9, 5) in emy_bot.SESSION_ORDER:
                emy_bot.SESSION_ORDER.remove((9, 5))

    async def test_active_effect_button_becomes_original(self):
        buttons = [
            button
            for row in emy_bot.fx_menu(-1001, 12, "8d")["inline_keyboard"]
            for button in row
        ]
        original = next(
            button for button in buttons
            if button["callback_data"] == emy_bot._cb(-1001, 12, "fx", "off")
        )

        self.assertEqual(original["text"], "Original")
        self.assertEqual(
            original["icon_custom_emoji_id"], CUSTOM_EMOJI_IDS["effect_off"]
        )
        self.assertNotIn(
            emy_bot._cb(-1001, 12, "fx", "8d"),
            [button["callback_data"] for button in buttons],
        )

    async def test_download_and_render_status_use_expected_text_and_emoji(self):
        downloading = emy_bot.downloading_status_html()
        titled_downloading = emy_bot.downloading_status_html("Ishq Risk")
        rendering = emy_bot.rendering_status_html("Concert")
        uploading = emy_bot.uploading_status_html("Ishq Risk", 12.3)

        self.assertNotIn("ishq risk", downloading)
        self.assertIn("<b>Downloading…</b>", downloading)
        self.assertIn("<b>Downloading…</b> <code>Ishq Risk</code>", titled_downloading)
        self.assertIn('emoji-id="5258289810082111221"', rendering)
        self.assertIn("<b>Rendering Concert…</b>", rendering)
        self.assertIn("<b>Uploading…</b> <code>Ishq Risk</code>", uploading)
        self.assertNotIn("MB", uploading)
        self.assertIn("⬆️", uploading)

    async def test_searching_title_cases_query_and_appends_a_fact(self):
        self.assertEqual(emy_bot.display_query("gehra hua"), "Gehra Hua")
        self.assertEqual(emy_bot.display_query("  tUM hi HO  "), "Tum Hi Ho")
        self.assertEqual(emy_bot.display_query("don't stop"), "Don't Stop")

        first = emy_bot.searching_status_html("gehra hua")
        self.assertIn('<b>Searching "Gehra Hua".</b>', first)
        self.assertNotIn("\n", first)
        second = emy_bot.searching_status_html("gehra hua")
        self.assertNotEqual(first, second)

    async def test_no_dashes_appear_in_rendered_messages(self):
        samples = [
            emy_bot.searching_status_html("well known hit"),
            emy_bot.downloading_status_html("Song — Note"),
            emy_bot.uploading_status_html("My — Song", 1.2),
            emy_bot.warn_html("Heads up", "well — known"),
            emy_bot.error_html("Failed", "time — out"),
            emy_bot.search_results_text({
                "candidates": [{"id": "a"}],
                "page": 0,
                "query": "half — fried",
            }),
        ]
        for text in samples:
            visible = re.sub(r"<[^>]+>", "", text)
            self.assertNotIn("-", visible)
            self.assertNotIn("—", visible)
            self.assertNotIn("–", visible)

    async def test_facts_rotate_without_repeating(self):
        self.assertGreaterEqual(len(emy_bot.MUSIC_FACTS), 120)
        seen = {emy_bot.next_fact() for _ in range(len(emy_bot.MUSIC_FACTS))}
        self.assertEqual(len(seen), len(emy_bot.MUSIC_FACTS))
        arijit = sum(
            "Arijit Singh" in fact for fact in emy_bot.MUSIC_FACTS
        )
        self.assertLessEqual(
            arijit, 10,
            "facts must stay spread across artists, not one name",
        )
        for fact in seen:
            self.assertNotIn("-", fact)
            self.assertNotIn("—", fact)

    async def test_lyrics_button_sends_lyrics_from_lrclib(self):
        chat_id, message_id = -1001, 55
        query = SimpleNamespace(
            data=emy_bot._cb(chat_id, message_id, "menu", "lyrics"),
            from_user=SimpleNamespace(id=42),
            answers=[],
        )

        async def answer(*args, **kwargs):
            query.answers.append((args, kwargs))

        query.answer = answer
        bot = FakeSongMenuBot()
        lyrics_session = object()
        lookup = AsyncMock(return_value={
            "title": "Na Jaane",
            "artist": "Neeraj Shridhar",
            "lyrics": "First line\nSecond line",
        })
        with patch.dict(emy_bot.SESSIONS, {
            (chat_id, message_id): {
                "title": "Na Jaane",
                "uploader": "Neeraj Shridhar",
                "album": "Album",
                "duration": 190,
            }
        }), patch.object(emy_bot.lrclib, "find_lyrics", lookup):
            await emy_bot.on_button(
                SimpleNamespace(callback_query=query),
                SimpleNamespace(
                    bot=bot,
                    application=SimpleNamespace(
                        bot_data={"lyrics_http_session": lyrics_session}
                    ),
                ),
            )

        lookup.assert_awaited_once_with(
            lyrics_session, "Na Jaane",
            artist="Neeraj Shridhar", album="Album", duration=190,
        )
        self.assertEqual(len(bot.messages), 1)
        args, kwargs = bot.messages[0]
        self.assertEqual(args, (
            chat_id, "<b>Na Jaane · Neeraj Shridhar</b>\n\nFirst line\nSecond line"
        ))
        self.assertEqual(kwargs["reply_to_message_id"], message_id)
        self.assertEqual(kwargs.get("parse_mode"), "HTML")

    async def test_group_settings_keyboard_uses_supported_button_fields(self):
        markup = emy_bot.settings_menu("concert")
        self.assertEqual(json.loads(json.dumps(markup)), markup)
        keyboard = markup["inline_keyboard"]
        buttons = [button for row in keyboard for button in row]
        selected = next(button for button in buttons if button["callback_data"] == "settings:fx:concert")
        self.assertEqual(selected["style"], "success")
        self.assertEqual(
            selected["icon_custom_emoji_id"], CUSTOM_EMOJI_IDS["audio_effect"]
        )
        self.assertTrue(all(
            button["style"] == ("success" if button is selected else "primary")
            for button in buttons if button["callback_data"].startswith("settings:fx:")
        ))
        three_d = next(
            button for button in buttons
            if button["callback_data"] == "settings:fx:3d"
        )
        self.assertEqual(three_d["text"], "3D")
        self.assertTrue(all(
            not button["text"].endswith(" - on")
            for button in buttons if button["callback_data"].startswith("settings:fx:")
        ))
        off_button = next(
            button for button in buttons
            if button["callback_data"] == "settings:fx:off"
        )
        self.assertEqual(off_button["text"], "Off")
        self.assertEqual(
            off_button["icon_custom_emoji_id"], "5258267368877989660"
        )
        back = next(button for button in buttons if button["callback_data"] == "settings:close:x")
        self.assertEqual((back["text"], back["style"]), ("Back", "danger"))

    async def test_settings_caption_describes_current_effect_and_choices(self):
        off_text, mode = emy_bot.settings_text("off")
        concert_text, _ = emy_bot.settings_text("concert")

        self.assertEqual(mode, "HTML")
        self.assertIn("<b>Settings</b>", off_text)
        self.assertIn("no effects applied", off_text)
        self.assertIn(
            "Tap an effect below: Slowed (slower), 8D (spatial), 360° (orbit), "
            "3D (gentle depth), Concert (live), Vinyl (classic), LoFi (mellow).",
            off_text,
        )
        self.assertIn("current effect:", concert_text)
        self.assertIn("Concert Hall", concert_text)

    async def test_edit_keyboard_uses_updated_icons_and_plain_back_label(self):
        buttons = [
            button
            for row in emy_bot.edit_menu(-1001, 12)["inline_keyboard"]
            for button in row
        ]
        artist = next(button for button in buttons if button["callback_data"].endswith(":artist"))
        thumbnail = next(button for button in buttons if button["callback_data"].endswith(":thumb"))
        back = next(button for button in buttons if button["callback_data"].endswith(":back:main"))

        self.assertEqual(artist["icon_custom_emoji_id"], "5370546867786523009")
        self.assertEqual(thumbnail["icon_custom_emoji_id"], "5258254475386167466")
        self.assertEqual(thumbnail["text"], "Edit thumbnail")
        self.assertEqual(back["text"], "Back")
        self.assertNotIn("icon_custom_emoji_id", back)
        self.assertEqual(
            emy_bot.CUSTOM_EMOJI_IDS["edit_artist"], "5370546867786523009"
        )
        self.assertEqual(
            emy_bot.CUSTOM_EMOJI_IDS["edit_thumbnail"], "5258254475386167466"
        )

    async def test_metadata_callback_fires_during_download_resolution(self):
        picked = {
            "id": "abc123",
            "title": "Na Jaane",
            "artist": "Neeraj Shridhar",
        }
        events = []

        def extract(*args, **kwargs):
            events.append("download")
            return {"audio_bytes": b"audio"}

        with patch.object(emy_bot, "resolve_official", return_value=picked), patch.object(
            emy_bot, "_extract_to_ram", side_effect=extract
        ):
            result = emy_bot.download_song_to_ram(
                "Na Jaane", lambda label: events.append(label)
            )

        self.assertEqual(events, ["Na Jaane · Neeraj Shridhar", "download"])
        self.assertEqual(result, {"audio_bytes": b"audio"})

    async def test_transient_public_download_retries_same_source_without_cookies(self):
        outcomes = [
            RuntimeError("The page needs to be reloaded."),
            {"audio_bytes": b"audio"},
        ]
        calls = []

        def extract(tmpdir, target, use_cookies, **kwargs):
            calls.append((target, use_cookies))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(emy_bot, "_extract_to_ram", side_effect=extract), patch.object(
            emy_bot.time, "sleep"
        ):
            result = emy_bot.download_song_to_ram(
                "https://www.youtube.com/watch?v=track123"
            )

        self.assertEqual(result, {"audio_bytes": b"audio"})
        self.assertEqual(
            calls,
            [
                ("https://www.youtube.com/watch?v=track123", False),
                ("https://www.youtube.com/watch?v=track123", False),
            ],
        )

    async def test_youtube_login_error_does_not_request_cookies(self):
        message = emy_bot.friendly_error(
            RuntimeError("Sign in to confirm you’re not a bot")
        )
        self.assertIn("YouTube requires sign-in for this upload", message)
        self.assertIn("Choose another result", message)
        self.assertNotIn("cookies", message.lower())

    async def test_direct_url_progress_hook_reports_track_metadata(self):
        labels = []
        opts = emy_bot.build_ydl_opts(
            "unused",
            timings={},
            metadata_callback=lambda info: labels.append(
                f'{emy_bot.derive_title(info)} - {emy_bot.derive_artist(info)}'
            ),
        )
        opts["progress_hooks"][0]({
            "status": "downloading",
            "info_dict": {
                "track": "Na Jaane",
                "artist": "Neeraj Shridhar",
            },
        })
        self.assertEqual(labels, ["Na Jaane - Neeraj Shridhar"])

    async def test_ephemeral_settings_callback_edits_only_its_private_response(self):
        bot = FakeBot()
        updates = []
        original_setter = emy_bot.set_default_effect
        emy_bot.set_default_effect = lambda user_id, effect: updates.append((user_id, effect))
        query = SimpleNamespace(
            data="settings:fx:concert",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001),
                api_kwargs={
                    "receiver_user": {"id": 42},
                    "ephemeral_message_id": 81,
                },
            ),
            from_user=SimpleNamespace(id=42),
            answers=[],
        )

        async def answer(*args, **kwargs):
            query.answers.append((args, kwargs))

        query.answer = answer
        try:
            await emy_bot.on_button(
                SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)
            )
        finally:
            emy_bot.set_default_effect = original_setter

        self.assertEqual(updates, [(42, "concert")])
        self.assertEqual(bot.calls[0][0], "editEphemeralMessageText")
        self.assertEqual(bot.calls[0][1]["receiver_user_id"], 42)
        self.assertEqual(bot.calls[0][1]["ephemeral_message_id"], 81)

    async def test_ephemeral_settings_callback_rejects_another_user(self):
        bot = FakeBot()
        query = SimpleNamespace(
            data="settings:fx:concert",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001),
                api_kwargs={
                    "receiver_user": {"id": 42},
                    "ephemeral_message_id": 81,
                },
            ),
            from_user=SimpleNamespace(id=43),
            answers=[],
        )

        async def answer(*args, **kwargs):
            query.answers.append((args, kwargs))

        query.answer = answer
        await emy_bot.on_button(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)
        )
        self.assertEqual(bot.calls, [])
        self.assertTrue(query.answers[0][1]["show_alert"])

    async def test_profile_configuration_sets_fields_and_optional_photo(self):
        original_photo = emy_bot.BOT_PROFILE_PHOTO
        original_photo_url = emy_bot.BOT_PROFILE_PHOTO_URL
        with tempfile.TemporaryDirectory() as temp_dir:
            photo_path = Path(temp_dir) / "profile.jpg"
            photo_path.write_bytes(b"test")
            emy_bot.BOT_PROFILE_PHOTO = str(photo_path)
            bot = FakeProfileBot()
            app = SimpleNamespace(bot=bot, bot_data={})
            try:
                await emy_bot.post_init(app)
            finally:
                emy_bot.BOT_PROFILE_PHOTO = original_photo
                emy_bot.BOT_PROFILE_PHOTO_URL = original_photo_url

        self.assertIsNotNone(bot.profile_photo)
        profile_calls = {method for method, _ in bot.calls}
        self.assertTrue({
            "setMyDescription", "setMyShortDescription",
            "setMyCommands", "getMyCommands",
        }.issubset(profile_calls))
        self.assertNotIn("setMyName", profile_calls)
        self.assertNotIn("getMyName", profile_calls)
        set_call = next(args for method, args in bot.calls if method == "setMyCommands")
        commands = set_call["commands"]
        self.assertEqual(
            [(command.command, command.description) for command in commands],
            [
                ("start", "Welcome to Emy"),
                ("search", "Find and play a song"),
                ("play", "Play a link or video id directly"),
                ("playlist", "Create and manage your playlists"),
                ("settings", "Choose an audio effect"),
                ("cancel", "Cancel the current input"),
                ("help", "Show available commands"),
            ],
        )
        self.assertEqual(set_call["scope"], {"type": "default"})
        get_call = next(args for method, args in bot.calls if method == "getMyCommands")
        self.assertEqual(get_call["scope"], {"type": "default"})
        command_scopes = {
            json.dumps(args["scope"], sort_keys=True)
            for method, args in bot.calls
            if method == "setMyCommands"
        }
        self.assertEqual(command_scopes, {
            '{"type": "all_chat_administrators"}',
            '{"type": "all_group_chats"}',
            '{"type": "all_private_chats"}',
            '{"type": "default"}',
        })
        self.assertEqual(
            set(app.bot_data["resolved_custom_emoji_packs"]), set(CUSTOM_EMOJI_PACK_NAMES)
        )

    async def test_profile_photo_url_is_loaded_and_uploaded_as_jpeg(self):
        original_photo = emy_bot.BOT_PROFILE_PHOTO
        original_photo_url = emy_bot.BOT_PROFILE_PHOTO_URL
        emy_bot.BOT_PROFILE_PHOTO = ""
        emy_bot.BOT_PROFILE_PHOTO_URL = "https://example.test/profile.png"
        bot = FakeProfileBot()
        app = SimpleNamespace(bot=bot, bot_data={})
        try:
            with patch.object(
                emy_bot, "download_profile_photo_jpeg", return_value=b"\xff\xd8\xffimage"
            ) as load_photo:
                await emy_bot.post_init(app)
        finally:
            emy_bot.BOT_PROFILE_PHOTO = original_photo
            emy_bot.BOT_PROFILE_PHOTO_URL = original_photo_url

        load_photo.assert_called_once_with("https://example.test/profile.png")
        self.assertIsInstance(bot.profile_photo["photo"], str)
        self.assertEqual(
            json.loads(bot.profile_photo["photo"]),
            {"type": "static", "photo": "attach://profile_photo"},
        )
        self.assertIsInstance(bot.profile_photo["profile_photo"], emy_bot.InputFile)

    async def test_text_draft_is_finalized_as_a_persistent_message(self):
        bot = FakeBot()

        async def chunks():
            yield "Hello"
            yield " world"

        await stream_text_response(bot, 42, chunks(), chat_type="private")
        self.assertEqual([method for method, _ in bot.calls], [
            "sendMessageDraft", "sendMessage"
        ])
        draft_id = bot.calls[0][1]["draft_id"]
        self.assertEqual(bot.calls[1][1], {
            "chat_id": 42, "text": "Hello world"
        })
        self.assertGreater(draft_id, 0)

    async def test_rich_draft_finalization_uses_separate_send(self):
        bot = FakeBot()

        async def drafts():
            yield {"html": "<h2>Preview</h2>"}

        final = {"html": "<h2>Complete</h2>"}
        await stream_rich_response(
            bot, 42, drafts(), final, chat_type="private"
        )
        self.assertEqual([method for method, _ in bot.calls], [
            "sendRichMessageDraft", "sendRichMessage"
        ])
        self.assertNotIn("draft_id", bot.calls[1][1])
        self.assertEqual(json.loads(json.dumps(bot.calls[0][1]["rich_message"])),
                         {"html": "<h2>Preview</h2>"})

    async def test_drafts_reject_group_chats(self):
        bot = FakeBot()

        async def chunks():
            yield "private only"

        with self.assertRaises(ValueError):
            await stream_text_response(bot, -1001, chunks(), chat_type="group")
        with self.assertRaises(ValueError):
            await stream_rich_response(
                bot, -1001, chunks(), {}, chat_type="supergroup"
            )
        self.assertEqual(bot.calls, [])


@unittest.skip("TypeTube was replaced by native YouTube stream resolution.")
class TypeTubeResolutionTests(unittest.TestCase):
    def test_m4a_source_skips_transcoding_and_preserves_provider_audio(self):
        track = SimpleNamespace(
            success=True,
            id="m4a-track",
            title="Native audio",
            author="Artist",
            duration_seconds=180,
            latency_ms=100,
            thumbnail="",
            best_audio=SimpleNamespace(
                mime_type="audio/mp4",
                url="https://stream.example/audio",
            ),
        )
        source_bytes = b"native-m4a-audio" * 256

        def write_source(_client, _track, _target, source_path):
            source_path.write_bytes(source_bytes)
            return _client, _track, len(source_bytes), 10.0

        with (
            patch.object(
                emy_bot,
                "_resolve_typetube_track",
                return_value=(object(), track),
            ),
            patch.object(
                emy_bot,
                "_download_typetube_source",
                side_effect=write_source,
            ),
            patch.object(emy_bot, "fetch_cover_jpeg", return_value=None),
            patch.object(emy_bot.subprocess, "run") as ffmpeg,
        ):
            result = emy_bot.download_song_to_ram(
                "https://www.youtube.com/watch?v=m4a-track"
            )

        self.assertEqual(result["audio_bytes"], source_bytes)
        self.assertEqual(result["audio_filename"], "m4a-track.m4a")
        ffmpeg.assert_not_called()

    def test_forbidden_range_stream_refreshes_and_falls_back_to_linear(self):
        audio = SimpleNamespace(url="https://stream.example/old")
        refreshed_track = SimpleNamespace(
            success=True,
            best_audio=SimpleNamespace(url="https://stream.example/new"),
        )

        class FakeClient:
            def __init__(self):
                self.attempts = []

            def download_audio(self, track, **kwargs):
                self.attempts.append((track, kwargs))
                raise urllib.error.HTTPError(
                    audio.url, 403, "Forbidden", {}, None
                )

        class FakeResponse:
            headers = {}

            def __init__(self):
                self.content = b"x" * 2048

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size):
                chunk, self.content = self.content[:size], self.content[size:]
                return chunk

        client = FakeClient()
        refreshed_client = FakeClient()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(emy_bot.os, "pwrite", lambda *_args: 0, create=True),
            patch.object(
                emy_bot,
                "_resolve_typetube_track",
                return_value=(refreshed_client, refreshed_track),
            ) as resolve,
            patch.object(
                emy_bot.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ) as urlopen,
        ):
            path = Path(tmpdir) / "track.m4a"
            selected_client, track, written, speed = (
                emy_bot._download_typetube_source(
                    client, SimpleNamespace(best_audio=audio), "track-id", path
                )
            )

        resolve.assert_called_once_with("track-id")
        self.assertIs(selected_client, refreshed_client)
        self.assertIs(track, refreshed_track)
        self.assertEqual(written, 2048)
        self.assertIsNone(speed)
        self.assertEqual(client.attempts[0][1]["workers"], 8)
        self.assertEqual(refreshed_client.attempts, [])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, refreshed_track.best_audio.url)
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(path.stat().st_size, 2048)

    def test_forbidden_download_error_is_user_friendly(self):
        error = urllib.error.HTTPError(
            "https://stream.example/audio", 403, "Forbidden", {}, None
        )
        self.assertEqual(
            emy_bot.friendly_error(error),
            "The audio source temporarily refused the download. Please try again.",
        )

    def test_public_tier_succeeds_without_reading_owner_key(self):
        class FakeTypeTubeError(Exception):
            pass

        track = object()
        client = SimpleNamespace(resolve=lambda _target: track)

        def create_client(**kwargs):
            self.assertEqual(kwargs["api_key"], "public")
            return client

        with (
            patch.dict(
                "sys.modules",
                {"typetube": SimpleNamespace(
                    TypeTubeError=FakeTypeTubeError,
                    create_client=create_client,
                )},
            ),
            patch.object(
                emy_bot.owner, "get_typetube_api_key",
                side_effect=AssertionError("owner key must not be read"),
            ),
        ):
            selected_client, resolved = emy_bot._resolve_typetube_track("track-id")

        self.assertIs(selected_client, client)
        self.assertIs(resolved, track)

    def test_public_tier_is_tried_before_owner_key_fallback(self):
        class FakeTypeTubeError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        clients = []

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key
                clients.append(self)

            def resolve(self, target):
                if self.api_key == "public":
                    raise FakeTypeTubeError("daily limit reached", 429)
                return {"target": target}

        def create_client(**kwargs):
            return FakeClient(kwargs.get("api_key") or "public")

        with (
            patch.dict(
                "sys.modules",
                {"typetube": SimpleNamespace(
                    TypeTubeError=FakeTypeTubeError,
                    create_client=create_client,
                )},
            ),
            patch.object(emy_bot.owner, "get_typetube_api_key", return_value="owner-secret"),
        ):
            client, track = emy_bot._resolve_typetube_track("track-id")

        self.assertEqual([item.api_key for item in clients], ["public", "owner-secret"])
        self.assertIs(client, clients[-1])
        self.assertEqual(track, {"target": "track-id"})

    def test_exhausted_public_tier_needs_no_user_key_or_user_detail(self):
        class FakeTypeTubeError(Exception):
            status_code = 429

        def create_client(**kwargs):
            self.assertEqual(kwargs["api_key"], "public")
            return SimpleNamespace(
                resolve=lambda _target: (_ for _ in ()).throw(
                    FakeTypeTubeError("private provider quota details")
                )
            )

        with (
            patch.dict(
                "sys.modules",
                {"typetube": SimpleNamespace(
                    TypeTubeError=FakeTypeTubeError,
                    create_client=create_client,
                )},
            ),
            patch.object(emy_bot.owner, "get_typetube_api_key", return_value=None),
        ):
            with self.assertRaises(emy_bot.TypeTubeFreeQuotaExhausted):
                emy_bot._resolve_typetube_track("track-id")

        self.assertNotIn(
            "private provider quota details",
            emy_bot.friendly_error(FakeTypeTubeError("private provider quota details")),
        )
        self.assertIn("/playlist", emy_bot.HELP_HTML)


class SearchResultCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_capped_partial_catalog_results_are_reused(self):
        previous_cache = dict(emy_bot.SEARCH_RESULT_CACHE)
        emy_bot.SEARCH_RESULT_CACHE.clear()
        entries = [{"id": "track-1", "title": "Track"}]
        try:
            with patch.object(emy_bot.youtube, "search", return_value=entries) as search:
                first = await emy_bot.resolve_candidates_async(
                    "Track", None, limit=50
                )
                second = await emy_bot.resolve_candidates_async(
                    "track", None, limit=50
                )
            self.assertEqual(first, entries)
            self.assertEqual(second, entries)
            search.assert_called_once_with("Track", 25)
        finally:
            emy_bot.SEARCH_RESULT_CACHE.clear()
            emy_bot.SEARCH_RESULT_CACHE.update(previous_cache)

    def test_search_query_normalization(self):
        self.assertEqual(
            emy_bot._normalize_search_query("  Song \t TITLE  "),
            "song title",
        )


if __name__ == "__main__":
    unittest.main()
