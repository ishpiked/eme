import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import owner
from telegram.ext import ApplicationHandlerStop


class OwnerConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.console = owner.OwnerConsole(
            owner_id=999,
            database_file=Path(self.temp_dir.name) / "owner.sqlite3",
        )

    async def asyncTearDown(self):
        await self.console.close()
        self.temp_dir.cleanup()

    async def _record_user(self, user_id, *, first_name="Test", username=None):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=user_id,
                is_bot=False,
                username=username,
                first_name=first_name,
                last_name=None,
            ),
            effective_chat=SimpleNamespace(type="private"),
        )
        await self.console.record_update(update, None)

    async def test_user_tracking_preserves_opt_out(self):
        await self._record_user(10)
        self.console.set_user_active(10, False)
        await self._record_user(10, first_name="Updated")

        with self.console._connect() as db:
            row = db.execute(
                "SELECT active, first_name FROM users WHERE user_id=10"
            ).fetchone()
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["first_name"], "Updated")

    async def test_broadcast_audience_selection_is_persisted(self):
        await self._record_user(10)
        await self._record_user(11)
        draft = {
            "source_chat_id": 999,
            "source_message_id": 4,
            "silent": True,
            "protect_content": False,
            "audience": "all",
        }

        broadcast_id = self.console._create_broadcast(draft)
        self.assertEqual(self.console._pending_count(broadcast_id), 2)
        with self.console._connect() as db:
            audience = db.execute(
                "SELECT audience FROM broadcasts WHERE id=?",
                (broadcast_id,),
            ).fetchone()["audience"]
        self.assertEqual(audience, "all")

    async def test_recent_audience_excludes_old_users(self):
        await self._record_user(10)
        await self._record_user(11)
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        with self.console._connect() as db:
            db.execute(
                "UPDATE users SET last_seen=? WHERE user_id=11", (old,)
            )
        draft = {
            "source_chat_id": 999,
            "source_message_id": 4,
            "silent": False,
            "protect_content": False,
            "audience": "recent",
        }

        broadcast_id = self.console._create_broadcast(draft)
        self.assertEqual(self.console._pending_count(broadcast_id), 1)

    async def test_playlists_persist_songs_and_are_private_to_the_owner(self):
        playlist_id = self.console.create_playlist(10, "Favorites")
        song = {
            "file_id": "telegram-file-id",
            "video_id": "video123",
            "title": "Song",
            "artist": "Artist",
            "duration": 180,
            "applied_effect": "off",
        }

        self.assertTrue(
            self.console.add_playlist_song(10, playlist_id, song)
        )
        self.assertFalse(
            self.console.add_playlist_song(10, playlist_id, song)
        )
        self.assertEqual(self.console.list_playlists(10)[0]["song_count"], 1)
        self.assertEqual(
            self.console.list_playlist_songs(10, playlist_id)[0]["file_id"],
            "telegram-file-id",
        )
        self.assertEqual(self.console.list_playlists(11), [])
        self.assertIsNone(self.console.get_playlist(11, playlist_id))

    async def test_playlist_variants_and_delete(self):
        playlist_id = self.console.create_playlist(10, "Favorites")
        base_song = {
            "file_id": "base-file",
            "video_id": "video123",
            "title": "Song",
            "artist": "Artist",
            "duration": 180,
            "applied_effect": "off",
        }
        effected_song = {
            **base_song,
            "file_id": "effect-file",
            "applied_effect": "360",
        }
        self.console.add_playlist_song(10, playlist_id, base_song)
        self.console.add_playlist_song(10, playlist_id, effected_song)
        saved_song_id = self.console.list_playlist_songs(
            10, playlist_id
        )[1]["id"]
        self.assertEqual(
            self.console.get_playlist_song(10, saved_song_id)["file_id"],
            "effect-file",
        )
        self.assertTrue(self.console.delete_playlist(10, playlist_id))
        self.assertEqual(self.console.list_playlists(10), [])

    async def test_stats_message_is_formatted_and_reports_empty_broadcasts(self):
        await self._record_user(10)

        text = await self.console._stats_text()

        self.assertIn('emoji-id="5258330865674494479"', text)
        self.assertIn("<b>Bot Statistics</b>", text)
        self.assertIn("<b>Known private users:</b> 1", text)
        self.assertIn("No recent broadcasts to display.", text)

    async def test_api_key_submission_deletes_prompt_and_key_message(self):
        prompt = SimpleNamespace(chat_id=999, message_id=25)
        prompt_owner_message = SimpleNamespace(
            reply_text=AsyncMock(return_value=prompt)
        )
        await self.console._prompt_for_api_key(prompt_owner_message)

        key_message = SimpleNamespace(
            text="private-test-key",
            message_id=26,
            delete=AsyncMock(),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_message=key_message,
            effective_chat=SimpleNamespace(type="private", id=999),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock())
        )
        fake_client = SimpleNamespace(
            usage=lambda: SimpleNamespace(success=True)
        )
        with (
            patch.dict(
                sys.modules,
                {"typetube": SimpleNamespace(
                    create_client=lambda **kwargs: fake_client
                )},
            ),
            patch.object(owner, "_save_api_key") as save_key,
        ):
            with self.assertRaises(ApplicationHandlerStop):
                await self.console.capture_owner_message(update, context)

        save_key.assert_called_once_with("private-test-key")
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=999, message_id=25
        )
        key_message.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
