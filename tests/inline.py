import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


def make_update(query):
    inline_query = SimpleNamespace(query=query, answer=AsyncMock())
    return SimpleNamespace(inline_query=inline_query), inline_query


class InlineSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_and_overlong_queries_return_empty(self):
        for query in ("", "  ", "x" * (bot.MAX_INLINE_QUERY_LENGTH + 1)):
            with self.subTest(query=query[:24]):
                update, inline_query = make_update(query)
                search = AsyncMock(return_value=[])
                with patch.object(bot, "resolve_candidates_async", search):
                    await bot.inline_search(
                        update, SimpleNamespace(application=object())
                    )

                inline_query.answer.assert_awaited_once()
                self.assertEqual(inline_query.answer.await_args.args[0], [])
                search.assert_not_awaited()

    async def test_unsupported_host_returns_empty_without_search(self):
        update, inline_query = make_update(
            "https://example.com/watch?v=abcdefghijk"
        )
        search = AsyncMock(return_value=[])
        with patch.object(bot, "resolve_candidates_async", search):
            await bot.inline_search(update, SimpleNamespace(application=object()))

        inline_query.answer.assert_awaited_once_with(
            [], cache_time=0, is_personal=True
        )
        search.assert_not_awaited()

    async def test_video_id_resolves_directly(self):
        update, inline_query = make_update("abcdefghijk")
        result = SimpleNamespace(id="abcdefghijk")
        with patch.object(
            bot,
            "_resolve_inline_audio",
            return_value=result,
        ) as resolver:
            await bot.inline_search(update, SimpleNamespace(application=object()))

        resolver.assert_called_once_with({
            "id": "abcdefghijk",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
        })
        inline_query.answer.assert_awaited_once_with(
            [result], cache_time=0, is_personal=True
        )

    async def test_youtube_url_preserves_music_host(self):
        for query in (
            "https://youtu.be/abcdefghijk?t=4",
            "https://www.youtube.com/shorts/abcdefghijk",
            "https://music.youtube.com/watch?v=abcdefghijk&list=PL123",
        ):
            with self.subTest(query=query):
                update, inline_query = make_update(query)
                result = SimpleNamespace(id="abcdefghijk")
                with patch.object(
                    bot,
                    "_resolve_inline_audio",
                    return_value=result,
                ) as resolver:
                    await bot.inline_search(
                        update, SimpleNamespace(application=object())
                    )

                candidate = resolver.call_args.args[0]
                self.assertEqual(candidate["id"], "abcdefghijk")
                self.assertEqual(candidate["url"], query)
                inline_query.answer.assert_awaited_once_with(
                    [result], cache_time=0, is_personal=True
                )

    async def test_search_filters_bad_ids_duplicates_and_limits_results(self):
        candidates = [
            {"id": "abcdefghijk", "title": "One"},
            {"id": "abcdefghijk", "title": "Duplicate"},
            {"id": "invalid", "title": "Bad ID"},
            {"id": "lmnopqrstuv", "title": "Two"},
            {"id": "wxyz1234567", "title": "Three"},
            {"id": "12345678901", "title": "Four"},
            {"id": "23456789012", "title": "Five"},
            {"id": "34567890123", "title": "Six"},
            None,
            "not a record",
        ]
        update, inline_query = make_update("favorite song")
        ids = [
            "abcdefghijk",
            "lmnopqrstuv",
            "wxyz1234567",
            "12345678901",
            "23456789012",
        ]
        results_by_id = {video_id: SimpleNamespace(id=video_id) for video_id in ids}
        with (
            patch.object(
                bot,
                "resolve_candidates_async",
                new=AsyncMock(return_value=candidates),
            ) as search,
            patch.object(
                bot,
                "_resolve_inline_audio",
                side_effect=lambda entry: results_by_id[entry["id"]],
            ),
        ):
            await bot.inline_search(
                update, SimpleNamespace(application=object())
            )

        search.assert_awaited_once_with("favorite song", ANY, limit=10)
        self.assertEqual(
            [result.id for result in inline_query.answer.await_args.args[0]],
            ids,
        )

    async def test_one_resolution_failure_keeps_other_results(self):
        candidates = [
            {"id": "abcdefghijk"},
            {"id": "lmnopqrstuv"},
            {"id": "wxyz1234567"},
        ]
        update, inline_query = make_update("song")

        def resolve(entry):
            if entry["id"] == "lmnopqrstuv":
                raise bot.youtube.YouTubeError("temporarily unavailable")
            return SimpleNamespace(id=entry["id"])

        with (
            patch.object(
                bot,
                "resolve_candidates_async",
                new=AsyncMock(return_value=candidates),
            ),
            patch.object(bot, "_resolve_inline_audio", side_effect=resolve),
        ):
            await bot.inline_search(
                update, SimpleNamespace(application=object())
            )

        self.assertEqual(
            [
                result.id
                for result in inline_query.answer.await_args.args[0]
            ],
            ["abcdefghijk", "wxyz1234567"],
        )

    def test_resolver_returns_audio_result_and_preserves_music_url(self):
        candidate = {
            "id": "abcdefghijk",
            "url": "https://music.youtube.com/watch?v=abcdefghijk",
            "title": "Fallback",
        }
        player_result = {
            "id": "abcdefghijk",
            "title": "Song - Official Audio",
            "author": "Artist",
            "duration": 183,
            "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/default.jpg",
            "formats": [{"url": "https://stream.example/audio"}],
        }
        audio_format = {"url": "https://stream.example/audio"}
        with (
            patch.object(
                bot.youtube, "resolve", return_value=player_result
            ) as resolve,
            patch.object(
                bot.youtube,
                "select_inline_audio_format",
                return_value=audio_format,
            ),
        ):
            result = bot._resolve_inline_audio(candidate)

        resolve.assert_called_once_with(candidate["url"])
        self.assertIsInstance(result, bot.InlineQueryResultAudio)
        self.assertEqual(result.audio_url, audio_format["url"])
        self.assertEqual(result.audio_duration, 183)
        self.assertEqual(
            result.thumb_url,
            player_result["thumbnail"],
        )


if __name__ == "__main__":
    unittest.main()
