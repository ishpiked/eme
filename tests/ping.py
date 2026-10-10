import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot


class OwnerPingTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_private_ping_reports_bot_api_round_trip(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="private"),
        )
        api_bot = SimpleNamespace(get_me=AsyncMock())
        context = SimpleNamespace(bot=api_bot)

        with patch.object(bot.OWNER_CONSOLE, "owner_id", 42):
            await bot.ping_command(update, context)

        api_bot.get_me.assert_awaited_once_with()
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertRegex(text, r"^Pong! <code>Latency: \d+\.\d{1} ms</code>$")
        self.assertEqual(
            message.reply_text.await_args.kwargs["parse_mode"],
            "HTML",
        )
        self.assertIn("ping", {command.command for command in bot.OWNER_COMMANDS})

    async def test_ping_ignores_non_owner_and_non_private_chats(self):
        api_bot = SimpleNamespace(get_me=AsyncMock())
        context = SimpleNamespace(bot=api_bot)
        message = SimpleNamespace(reply_text=AsyncMock())

        for user_id, chat_type in ((7, "private"), (42, "group")):
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(type=chat_type),
            )
            with patch.object(bot.OWNER_CONSOLE, "owner_id", 42):
                await bot.ping_command(update, context)

        api_bot.get_me.assert_not_awaited()
        message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
