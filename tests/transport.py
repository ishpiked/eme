import unittest
from unittest.mock import Mock

from transport import configure_local_api, local_api_urls


class TelegramTransportTests(unittest.TestCase):
    def test_empty_local_api_url_uses_default_transport(self):
        self.assertIsNone(local_api_urls(""))
        self.assertIsNone(local_api_urls(None))

    def test_local_api_urls_include_bot_and_file_routes(self):
        self.assertEqual(
            local_api_urls("http://127.0.0.1:8081/"),
            (
                "http://127.0.0.1:8081/bot",
                "http://127.0.0.1:8081/file/bot",
            ),
        )

    def test_local_api_url_rejects_non_origin_values(self):
        for value in (
            "ftp://127.0.0.1:8081",
            "http://user:pass@127.0.0.1:8081",
            "http://127.0.0.1:8081/bot",
            "http://127.0.0.1:8081?token=secret",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    local_api_urls(value)

    def test_configure_local_api_sets_both_urls_and_local_mode(self):
        builder = Mock()
        builder.base_url.return_value = builder
        builder.base_file_url.return_value = builder
        builder.local_mode.return_value = builder

        result = configure_local_api(builder, "http://127.0.0.1:8081")

        self.assertIs(result, builder)
        builder.base_url.assert_called_once_with("http://127.0.0.1:8081/bot")
        builder.base_file_url.assert_called_once_with(
            "http://127.0.0.1:8081/file/bot"
        )
        builder.local_mode.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
