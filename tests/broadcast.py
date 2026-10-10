import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ApplicationHandlerStop

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owner import OwnerConsole


class FakeBot:
    def __init__(self):
        self.calls = []

    async def do_api_request(self, method, *, api_kwargs):
        self.calls.append((method, api_kwargs))

    async def copy_message(self, **kwargs):
        self.calls.append(("copyMessage", kwargs))


class OwnerBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.console = OwnerConsole(
            owner_id=123,
            database_file=Path(self.temp_dir.name) / "bot.sqlite3",
        )
        self.console._initialize_database()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_non_owner_command_guard_is_silent(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=456),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
        )

        self.assertFalse(await self.console._require_owner_private(update))
        message.reply_text.assert_not_awaited()

    async def test_text_broadcast_persists_buttons_and_custom_emoji(self):
        button_rows = [[{
            "text": "Open",
            "url": "https://example.com",
            "style": "success",
        }]]
        with self.console._connect() as db:
            db.execute(
                "INSERT INTO users (user_id, first_seen, last_seen, active) "
                "VALUES (456, 'now', 'now', 1)"
            )
        broadcast_id = self.console._create_broadcast({
            "source_chat_id": 123,
            "source_message_id": 7,
            "silent": False,
            "protect_content": True,
            "audience": "all",
            "buttons": button_rows,
            "content_type": "text",
            "rich_text": "<b>Hello</b> {123456789}",
        })
        with self.console._connect() as db:
            row = db.execute(
                "SELECT * FROM broadcasts WHERE id=?", (broadcast_id,)
            ).fetchone()

        self.assertEqual(json.loads(row["buttons_json"]), button_rows)
        bot = FakeBot()
        await self.console._send_broadcast_message(bot, row, 456)

        method, payload = bot.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("<b>Hello</b>", payload["text"])
        self.assertIn('emoji-id="123456789"', payload["text"])
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            json.loads(payload["reply_markup"])["inline_keyboard"],
            button_rows,
        )

    async def test_media_broadcast_copies_caption_and_styled_buttons(self):
        bot = FakeBot()
        await self.console._send_broadcast_message(bot, {
            "content_type": "media",
            "source_chat_id": 123,
            "source_message_id": 8,
            "silent": True,
            "protect_content": False,
            "rich_text": "<i>Caption</i>",
            "buttons_json": '[ [{"text":"Visit","url":"https://example.com","style":"primary"}] ]',
        }, 456)

        method, payload = bot.calls[0]
        self.assertEqual(method, "copyMessage")
        self.assertEqual(payload["caption"], "<i>Caption</i>")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            json.loads(payload["reply_markup"])["inline_keyboard"][0][0]["style"],
            "primary",
        )

    async def test_button_command_extracts_custom_emoji_icon(self):
        self.console.awaiting[123] = {
            "kind": "broadcast_draft",
            "buttons": [],
        }
        message = SimpleNamespace(
            text=(
                "/broadcast_button Updates {123456789} | "
                "https://example.com | success"
            ),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(effective_message=message)

        await self.console._broadcast_button_command(update, None)

        self.assertEqual(
            self.console.awaiting[123]["buttons"],
            [[{
                "text": "Updates",
                "url": "https://example.com",
                "style": "success",
                "icon_custom_emoji_id": "123456789",
            }]],
        )

    async def test_button_builder_uses_inline_label_url_and_style_steps(self):
        draft = {
            "kind": "broadcast_draft",
            "buttons": [],
            "silent": False,
            "protect_content": False,
            "audience": "all",
        }
        self.console.awaiting[123] = draft
        query = SimpleNamespace(
            edit_message_text=AsyncMock(),
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(bot=FakeBot())

        await self.console._broadcast_callback(query, ["button_add"], context)
        self.assertEqual(draft["kind"], "broadcast_button_label")

        async def capture(text, message_id):
            message = SimpleNamespace(
                text=text,
                text_html=text,
                caption_html=None,
                message_id=message_id,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=123, type="private"),
                effective_message=message,
            )
            with self.assertRaises(ApplicationHandlerStop):
                await self.console.capture_owner_message(update, context)
            return message

        await capture("Updates {123456789}", 10)
        self.assertEqual(draft["kind"], "broadcast_button_url")
        await capture("https://example.com/updates", 11)
        self.assertEqual(draft["kind"], "broadcast_button_style")

        await self.console._broadcast_callback(query, ["style", "success"], context)

        self.assertEqual(draft["kind"], "broadcast_draft")
        self.assertEqual(draft["buttons"], [[{
            "text": "Updates",
            "url": "https://example.com/updates",
            "style": "success",
            "icon_custom_emoji_id": "123456789",
        }]])
        query.edit_message_text.assert_awaited()

    async def test_button_manager_removes_one_button_and_keeps_limits_visible(self):
        buttons = [
            [
                {"text": "A", "url": "https://a.example", "style": "primary"},
                {"text": "B", "url": "https://b.example", "style": "danger"},
            ],
            [{"text": "C", "url": "https://c.example", "style": "success"}],
        ]
        self.console.awaiting[123] = {
            "kind": "broadcast_draft",
            "buttons": buttons,
        }
        query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())

        await self.console._broadcast_callback(
            query,
            ["button_remove", "0", "1"],
            SimpleNamespace(bot=FakeBot()),
        )

        draft = self.console.awaiting[123]
        self.assertEqual(draft["buttons"], [
            [{"text": "A", "url": "https://a.example", "style": "primary"}],
            [{"text": "C", "url": "https://c.example", "style": "success"}],
        ])
        markup = self.console._button_manager_markup(draft)
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn("owner:broadcast:button_add", callbacks)
        self.assertIn("owner:broadcast:button_remove:0:0", callbacks)

    async def test_preview_sends_draft_with_buttons(self):
        bot = FakeBot()
        await self.console._send_broadcast_message(bot, {
            "content_type": "text",
            "rich_text": "<b>Notice</b>",
            "buttons": [[{
                "text": "Open",
                "url": "https://example.com",
                "style": "primary",
            }]],
            "silent": False,
            "protect_content": False,
        }, 123)

        method, payload = bot.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["text"], "<b>Notice</b>")
        self.assertEqual(
            json.loads(payload["reply_markup"])["inline_keyboard"][0][0]["text"],
            "Open",
        )

    def test_broadcast_preview_escapes_content_and_counts_html_entities(self):
        self.assertEqual(
            self.console._broadcast_plain_length("<b>Tea &amp; coffee</b>"),
            len("Tea & coffee"),
        )
        panel = self.console._broadcast_panel_text({
            "content_type": "text",
            "rich_text": "<b>Safe &lt;copy&gt;</b>",
            "buttons": [],
            "audience": "all",
            "silent": False,
            "protect_content": False,
        })
        self.assertIn("Safe &lt;copy&gt;", panel)


if __name__ == "__main__":
    unittest.main()
