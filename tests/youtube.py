import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import youtube


class _Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if size < 0:
            result, self.body = self.body, b""
            return result
        result, self.body = self.body[:size], self.body[size:]
        return result


class TestYouTube(unittest.TestCase):
    def test_search_parses_and_deduplicates_results(self):
        data = {
            "contents": [
                {"videoRenderer": {
                    "videoId": "abcdefghijk",
                    "title": {"runs": [{"text": "Song &amp; title"}]},
                    "ownerText": {"runs": [{"text": "Artist"}]},
                    "lengthText": {"simpleText": "3:21"},
                    "viewCountText": {"simpleText": "1,234 views"},
                    "thumbnail": {"thumbnails": [{"url": "thumb"}]},
                }},
                {"videoRenderer": {
                    "videoId": "abcdefghijk",
                    "title": {"runs": [{"text": "Duplicate"}]},
                }},
                {"videoRenderer": {
                    "videoId": "bad-id",
                    "title": {"runs": [{"text": "Invalid"}]},
                }},
            ]
        }
        page = f"var ytInitialData = {json.dumps(data)};</script>"
        with patch.object(
            youtube.urllib.request,
            "urlopen",
            return_value=_Response(page.encode()),
        ):
            results = youtube.search("song", limit=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "abcdefghijk")
        self.assertEqual(results[0]["title"], "Song & title")
        self.assertEqual(results[0]["duration"], 201)
        self.assertEqual(results[0]["artist"], "Artist")

    def test_search_rejects_consent_page(self):
        with patch.object(
            youtube.urllib.request,
            "urlopen",
            return_value=_Response(b"<html>consent</html>"),
        ), self.assertRaisesRegex(youtube.YouTubeError, "consent"):
            youtube.search("song")

    def test_get_video_id_accepts_supported_youtube_url_shapes(self):
        self.assertEqual(youtube.get_video_id("abcdefghijk"), "abcdefghijk")
        self.assertEqual(
            youtube.get_video_id("https://youtu.be/abcdefghijk?t=10"),
            "abcdefghijk",
        )
        self.assertEqual(
            youtube.get_video_id(
                "https://music.youtube.com/watch?v=abcdefghijk&list=abc"
            ),
            "abcdefghijk",
        )
        self.assertEqual(
            youtube.get_video_id("https://www.youtube.com/watch?v=abcdefghijk"),
            "abcdefghijk",
        )
        self.assertEqual(
            youtube.get_video_id("youtube.com/shorts/abcdefghijk"),
            "abcdefghijk",
        )

    def test_get_video_id_rejects_non_youtube_hosts_and_bad_ids(self):
        for value in (
            "https://youtube.example/abcdefghijk",
            "https://youtu.be/not-an-id",
            "https://user@youtube.com/watch?v=abcdefghijk",
        ):
            with self.subTest(value=value), self.assertRaises(youtube.YouTubeError):
                youtube.get_video_id(value)

    def test_resolve_maps_direct_formats_and_video_details(self):
        payload = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "title": "Song",
                "author": "Artist",
                "lengthSeconds": "180",
                "thumbnail": {
                    "thumbnails": [
                        {"url": "small", "width": 60, "height": 60},
                        {"url": "large", "width": 120, "height": 120},
                    ]
                },
            },
            "streamingData": {
                "formats": [],
                "adaptiveFormats": [{
                    "mimeType": 'audio/webm; codecs="opus"',
                    "bitrate": 128000,
                    "contentLength": "4000",
                    "url": "https://stream.example/audio",
                    "audioQuality": "AUDIO_QUALITY_MEDIUM",
                }],
            },
        }
        with patch.object(
            youtube.urllib.request,
            "urlopen",
            return_value=_Response(json.dumps(payload).encode()),
        ):
            track = youtube.resolve("abcdefghijk")

        self.assertEqual(track["id"], "abcdefghijk")
        self.assertEqual(track["title"], "Song")
        self.assertEqual(track["duration"], 180)
        self.assertEqual(track["thumbnail"], "large")
        self.assertEqual(track["formats"][0]["url"], "https://stream.example/audio")
        self.assertEqual(youtube.select_audio_format(track["formats"])["mimeType"],
                         'audio/webm; codecs="opus"')

    def test_music_link_retries_with_other_player_clients(self):
        unavailable = {
            "playabilityStatus": {
                "status": "LOGIN_REQUIRED",
                "reason": "Sign in required",
            }
        }
        available = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"title": "Song", "lengthSeconds": "180"},
            "streamingData": {
                "adaptiveFormats": [{
                    "mimeType": "audio/mp4",
                    "bitrate": 128000,
                    "url": "https://stream.example/audio",
                }]
            },
        }
        responses = [
            _Response(json.dumps(unavailable).encode()),
            _Response(json.dumps(available).encode()),
        ]
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            return responses.pop(0)

        with patch.object(
            youtube.urllib.request, "urlopen", side_effect=urlopen
        ):
            track = youtube.resolve(
                "https://music.youtube.com/watch?v=abcdefghijk"
            )

        self.assertEqual(track["id"], "abcdefghijk")
        self.assertEqual(len(requests), 2)
        self.assertIn("music.youtube.com/youtubei/v1/player", requests[0].full_url)
        self.assertIn("youtube.com/youtubei/v1/player", requests[1].full_url)

    def test_resolve_surfaces_player_unavailability_reason(self):
        payload = {
            "playabilityStatus": {
                "status": "ERROR",
                "reason": "This video is private",
            }
        }
        with patch.object(
            youtube.urllib.request,
            "urlopen",
            return_value=_Response(json.dumps(payload).encode()),
        ), self.assertRaisesRegex(
            youtube.YouTubeError, "This video is private"
        ):
            youtube.resolve("abcdefghijk")

    def test_inline_audio_selects_direct_mp4_audio_only_stream(self):
        selected = {
            "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
            "url": "https://stream.example/audio",
            "bitrate": 128000,
            "hasAudio": True,
            "hasVideo": False,
        }
        formats = [
            {
                "mimeType": 'audio/webm; codecs="opus"',
                "url": "https://stream.example/webm",
                "bitrate": 160000,
                "hasAudio": True,
                "hasVideo": False,
            },
            {
                "mimeType": 'video/mp4; codecs="avc1,mp4a"',
                "url": "https://stream.example/video",
                "bitrate": 800000,
                "hasAudio": True,
                "hasVideo": True,
            },
            selected,
        ]
        self.assertIs(youtube.select_inline_audio_format(formats), selected)

    def test_inline_video_selects_best_bounded_combined_mp4_stream(self):
        best = {
            "mimeType": 'video/mp4; codecs="avc1,mp4a"',
            "url": "https://stream.example/720p",
            "height": 720,
            "bitrate": 900000,
            "contentLength": 20_000_000,
            "hasAudio": True,
            "hasVideo": True,
        }
        too_large = {
            **best,
            "url": "https://stream.example/large",
            "height": 720,
            "contentLength": 60_000_000,
        }
        formats = [
            {
                **best,
                "url": "https://stream.example/360p",
                "height": 360,
                "bitrate": 400000,
            },
            too_large,
            best,
        ]

        self.assertIs(youtube.select_inline_video_format(formats), best)

    def test_inline_video_rejects_video_without_audio(self):
        with self.assertRaisesRegex(youtube.YouTubeError, "video-and-audio"):
            youtube.select_inline_video_format([{
                "mimeType": "video/mp4",
                "url": "https://stream.example/video-only",
                "height": 720,
                "hasAudio": False,
                "hasVideo": True,
            }])

    def test_download_format_uses_parallel_ranges(self):
        payload = b"abcdefgh"
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "audio.webm"

            def urlopen(request, timeout):
                self.assertEqual(timeout, 30)
                range_header = request.get_header("Range")
                start, end = (
                    int(value) for value in range_header.removeprefix("bytes=").split("-")
                )
                return _Response(
                    payload[start:end + 1],
                    status=206,
                    headers={"Content-Range": f"bytes {start}-{end}/{len(payload)}"},
                )

            with patch.object(youtube, "DOWNLOAD_CHUNK_SIZE", 4), patch.object(
                youtube, "DOWNLOAD_WORKERS", 2
            ), patch.object(youtube.urllib.request, "urlopen", side_effect=urlopen):
                size = youtube.download_format(
                    {"url": "https://stream.example/audio", "contentLength": 8},
                    destination,
                )

        self.assertEqual(size, len(payload))
        self.assertEqual(destination.read_bytes(), payload)

    def test_download_format_falls_back_when_ranges_are_not_supported(self):
        payload = b"abcdefgh"

        def urlopen(request, timeout):
            if request.get_header("Range"):
                return _Response(payload, status=200)
            return _Response(payload, status=200)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "audio.webm"
            with patch.object(youtube, "DOWNLOAD_CHUNK_SIZE", 4), patch.object(
                youtube.urllib.request, "urlopen", side_effect=urlopen
            ):
                size = youtube.download_format(
                    {"url": "https://stream.example/audio", "contentLength": 8},
                    destination,
                )

            self.assertEqual(size, len(payload))
            self.assertEqual(destination.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
