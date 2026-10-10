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
    async def test_input_battery_returns_empty_for_non_searchable_inputs(self):
        cases = (
            "",
            "   ",
            "https://example.com/watch?v=abcdefghijk",
            "https://youtube.com/not-a-video",
            "x" * (bot.MAX_INLINE_QUERY_LENGTH + 1),
        )
        for query in cases:
            with self.subTest(query=query[:40]):
                update, inline_query = make_update(query)
                search = AsyncMock(return_value=[])
                with patch.object(bot, "resolve_candidates_async", search):
                    await bot.inline_search(
                        update, SimpleNamespace(application=object())
                    )

                inline_query.answer.assert_awaited_once()
                self.assertEqual(inline_query.answer.await_args.args[0], [])
                should_search = (
                    query
                    and not query.startswith("https://")
                    and len(query) <= bot.MAX_INLINE_QUERY_LENGTH
                )
                if should_search:
                    search.assert_awaited_once()
                else:
                    search.assert_not_awaited()

    async def test_youtube_url_and_id_return_playable_articles(self):
        cases = (
            (
                "https://youtu.be/abcdefghijk?t=4",
                "https://www.youtube.com/watch?v=abcdefghijk",
            ),
            (
                "https://music.youtube.com/watch?v=abcdefghijk&list=PL123",
                "https://music.youtube.com/watch?v=abcdefghijk",
            ),
            (
                "abcdefghijk",
                "https://www.youtube.com/watch?v=abcdefghijk",
            ),
        )
        for query, expected_url in cases:
            with self.subTest(query=query):
                update, inline_query = make_update(query)
                await bot.inline_search(update, SimpleNamespace(application=object()))

                results = inline_query.answer.await_args.args[0]
                self.assertEqual(len(results), 1)
                self.assertIsInstance(results[0], bot.InlineQueryResultArticle)
                self.assertEqual(
                    results[0].input_message_content.message_text,
                    f"/play {expected_url} --no-controls",
                )
                self.assertEqual(
                    inline_query.answer.await_args.kwargs["cache_time"], 0
                )

    async def test_search_results_filter_bad_ids_duplicates_and_cap_to_ten(self):
        ids = [
            "abcdefghijk",
            "abcdefghijk",
            "not-an-id",
            "lmnopqrstuv",
            "wxyz1234567",
            "12345678901",
            "23456789012",
            "34567890123",
            "45678901234",
            "56789012345",
            "67890123456",
            "78901234567",
            "89012345678",
        ]
        entries = [
            {
                "id": video_id,
                "title": f"Song {index}",
                "artist": "Artist",
                "duration": 187,
                "thumbnail": "https://i.ytimg.com/vi/example/default.jpg",
            }
            for index, video_id in enumerate(ids)
        ]
        update, inline_query = make_update("song")
        with patch.object(
            bot,
            "resolve_candidates_async",
            new=AsyncMock(return_value=entries),
        ) as search:
            await bot.inline_search(
                update, SimpleNamespace(application=object())
            )

        search.assert_awaited_once_with("song", ANY, limit=10)
        results = inline_query.answer.await_args.args[0]
        self.assertEqual(len(results), 10)
        self.assertEqual(len({result.id for result in results}), 10)
        self.assertEqual(results[0].description, "Artist · 3:07")
        self.assertIn("Artist", results[0].title)
        for result in results:
            self.assertTrue(
                result.input_message_content.message_text.startswith("/play ")
            )
            self.assertTrue(
                result.input_message_content.message_text.endswith(
                    " --no-controls"
                )
            )

    async def test_malformed_candidate_is_skipped_without_losing_valid_results(self):
        entries = [
            {"id": "abcdefghijk", "title": "Good result"},
            {"id": "lmnopqrstuv", "title": "Bad duration", "duration": object()},
            {"id": "wxyz1234567", "title": "Another good result"},
        ]
        update, inline_query = make_update("song")
        with patch.object(
            bot,
            "resolve_candidates_async",
            new=AsyncMock(return_value=entries),
        ):
            await bot.inline_search(
                update, SimpleNamespace(application=object())
            )

        results = inline_query.answer.await_args.args[0]
        self.assertEqual(
            [result.id for result in results],
            ["abcdefghijk", "lmnopqrstuv", "wxyz1234567"],
        )
        self.assertEqual(results[1].description, None)

    async def test_inline_play_command_reaches_song_without_recognition(self):
        music_url = "https://music.youtube.com/watch?v=abcdefghijk"
        update = SimpleNamespace(
            message=SimpleNamespace(),
            effective_message=SimpleNamespace(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=99),
        )
        context = SimpleNamespace(
            args=[music_url, "--no-controls"],
            bot=object(),
            application=object(),
        )
        with patch.object(bot, "song", new=AsyncMock()) as play_song:
            await bot.play_cmd(update, context)

        play_song.assert_awaited_once()
        delivery_context = play_song.await_args.args[1]
        self.assertEqual(delivery_context.args, [music_url])
        self.assertTrue(delivery_context.no_controls)

    async def test_search_failure_still_answers_inline_query(self):
        update, inline_query = make_update("song")
        with patch.object(
            bot,
            "resolve_candidates_async",
            new=AsyncMock(side_effect=RuntimeError("search unavailable")),
        ):
            await bot.inline_search(
                update, SimpleNamespace(application=object())
            )

        inline_query.answer.assert_awaited_once()
        self.assertEqual(inline_query.answer.await_args.args[0], [])


if __name__ == "__main__":
    unittest.main()
