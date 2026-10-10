import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as emy_bot
import recognition


class SongRecognitionTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_supported_public_urls_and_rejects_other_hosts(self):
        self.assertTrue(recognition.validate_media_url(
            "https://www.youtube.com/watch?v=abc"
        ))
        self.assertFalse(recognition.validate_media_url(
            "https://www.tiktok.com/@user/video/123"
        ))
        self.assertFalse(recognition.validate_media_url(
            "https://example.com/video"
        ))
        self.assertFalse(recognition.validate_media_url(
            "https://user:password@youtube.com/watch?v=abc"
        ))
        self.assertFalse(recognition.validate_media_url(
            "file:///tmp/song.mp3"
        ))

    def test_music_links_point_straight_at_music(self):
        self.assertTrue(recognition.is_music_url(
            "https://music.youtube.com/watch?v=ODH_e0rOuyQ"
        ))
        self.assertFalse(recognition.is_music_url(
            "https://www.youtube.com/watch?v=abc"
        ))
        self.assertFalse(recognition.is_music_url(
            "https://youtu.be/abc"
        ))
        self.assertFalse(recognition.is_music_url(
            "https://example.com/video"
        ))

    def test_song_audio_is_transcoded_to_configured_mp3_bitrate(self):
        track = {
            "id": "abcdefghijk",
            "title": "Song",
            "author": "Artist",
            "duration": 180,
            "thumbnail": None,
            "formats": [],
        }
        source_format = {
            "mimeType": "audio/mp4",
            "url": "https://stream.example/audio",
        }

        def download(_stream, destination, max_bytes):
            destination.write_bytes(b"source audio" * 200)
            return destination.stat().st_size

        def transcode(command, **_kwargs):
            Path(command[-1]).write_bytes(b"mp3 output" * 200)
            return SimpleNamespace(returncode=0, stderr=b"")

        with (
            patch.object(emy_bot.youtube, "resolve", return_value=track),
            patch.object(
                emy_bot.youtube,
                "select_audio_format",
                return_value=source_format,
            ),
            patch.object(
                emy_bot.youtube, "format_extension", return_value=".m4a"
            ),
            patch.object(
                emy_bot.youtube,
                "download_format",
                side_effect=download,
            ),
            patch.object(
                emy_bot.subprocess, "run", side_effect=transcode
            ) as ffmpeg,
            patch.object(emy_bot, "fetch_cover_jpeg", return_value=None),
        ):
            result = emy_bot.download_song_to_ram(
                "https://www.youtube.com/watch?v=abcdefghijk"
            )

        self.assertEqual(result["audio_filename"], "abcdefghijk.mp3")
        self.assertEqual(
            ffmpeg.call_args.args[0][
                ffmpeg.call_args.args[0].index("-b:a") + 1
            ],
            f"{emy_bot.MP3_QUALITY}k",
        )

    def test_recognizes_only_audio_and_video_upload_types(self):
        self.assertTrue(recognition.is_audio_or_video("audio/mpeg", "track"))
        self.assertTrue(recognition.is_audio_or_video(None, "clip.MP4"))
        self.assertFalse(recognition.is_audio_or_video("image/jpeg", "cover.jpg"))

    def test_parses_only_complete_shazam_matches(self):
        self.assertEqual(
            recognition.parse_recognition_response({
                "matches": [{"id": "match"}],
                "track": {
                    "title": " Na Jaane ",
                    "subtitle": " Neeraj Shridhar ",
                },
            }),
            {"title": "Na Jaane", "artist": "Neeraj Shridhar"},
        )
        self.assertIsNone(recognition.parse_recognition_response({
            "matches": [], "track": {"title": "Na Jaane", "subtitle": "Artist"},
        }))
        self.assertIsNone(recognition.parse_recognition_response({
            "matches": [{"id": "match"}], "track": {"title": "Na Jaane"},
        }))
        with self.assertRaises(recognition.RecognitionError):
            recognition.parse_recognition_response([])

    def test_title_match_ratio_requires_shared_title_words(self):
        self.assertEqual(
            recognition.title_match_ratio("Na Jaane", "Na Jaane (Film Version)"),
            1.0,
        )
        self.assertEqual(recognition.title_match_ratio("Na Jaane", "Other Song"), 0.0)

    async def test_shazam_recognition_does_not_require_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sample = Path(temp_dir) / "sample.mp3"
            sample.write_bytes(b"sample-audio")
            with patch.object(
                recognition,
                "_run_shazam",
                new=AsyncMock(return_value={
                    "matches": [{"id": "match"}],
                    "track": {
                        "title": "Na Jaane",
                        "subtitle": "Neeraj Shridhar",
                    },
                }),
            ) as recognize:
                result = await recognition.recognize_sample(sample)

        recognize.assert_awaited_once_with(sample)
        self.assertEqual(result, {
            "title": "Na Jaane", "artist": "Neeraj Shridhar",
        })

    async def test_url_preview_downloader_enforces_720p_format_ceiling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            preview = work_dir / "preview.mp4"
            selected_format = {
                "url": "https://stream.example/video",
                "mimeType": 'video/mp4; codecs="avc1,mp4a"',
                "width": 1280,
                "height": 720,
                "hasAudio": True,
                "hasVideo": True,
                "contentLength": 1024,
            }

            def download(_format, destination, max_bytes):
                self.assertEqual(max_bytes, recognition.MAX_RECOGNITION_FILE_BYTES)
                destination.write_bytes(b"video-preview")
                return destination.stat().st_size

            with patch.object(
                recognition.youtube,
                "resolve",
                return_value={
                    "id": "abcdefghijk",
                    "title": "Test video",
                    "formats": [selected_format],
                },
            ), patch.object(
                recognition.youtube,
                "select_preview_format",
                return_value=selected_format,
            ), patch.object(
                recognition.youtube,
                "download_format",
                side_effect=download,
            ):
                result_path, info = await recognition.download_url_preview(
                    "https://www.youtube.com/watch?v=abcdefghijk", work_dir
                )

        self.assertEqual(result_path, preview)
        self.assertEqual(info["height"], 720)

    async def test_unavailable_shazam_service_returns_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sample = Path(temp_dir) / "sample.mp3"
            sample.write_bytes(b"sample-audio")
            with patch.object(
                recognition,
                "_run_shazam",
                new=AsyncMock(side_effect=RuntimeError("provider failure")),
            ):
                with self.assertRaisesRegex(
                    recognition.RecognitionError, "free recognition service"
                ):
                    await recognition.recognize_sample(sample)

    async def test_music_link_plays_directly_without_preview(self):
        url = "https://music.youtube.com/watch?v=ODH_e0rOuyQ"

        class FakeMessage:
            text = "https://music.youtube.com/watch?v=ODH_e0rOuyQ"
            caption = None
            audio = voice = video = video_note = document = None
            message_id = 41

            async def reply_video(self, **kwargs):
                raise AssertionError("music links must skip the video preview")

        emy_bot.RECOGNITION_REQUESTS.clear()
        emy_bot.PENDING.pop(777, None)
        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=777),
            effective_chat=SimpleNamespace(id=9),
        )
        with patch.object(
            emy_bot, "song", new=AsyncMock()
        ) as deliver_song:
            await emy_bot.receive_recognition_input(
                update, SimpleNamespace(bot=SimpleNamespace())
            )

        deliver_song.assert_awaited_once()
        delivery_update, delivery_context = deliver_song.await_args.args
        self.assertEqual(delivery_context.args, [url])
        self.assertEqual(delivery_update.effective_chat.id, 9)
        self.assertEqual(len(emy_bot.RECOGNITION_REQUESTS), 0)

    async def test_inline_query_resolves_results_without_result_cache(self):
        query = SimpleNamespace(
            query="song name",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=query)
        candidates = [{"id": "abcdefghijk"}]
        result = SimpleNamespace(id="abcdefghijk")
        application = SimpleNamespace()

        with (
            patch.object(
                emy_bot,
                "resolve_candidates_async",
                new=AsyncMock(return_value=candidates),
            ) as search,
            patch.object(
                emy_bot,
                "_resolve_inline_audio",
                return_value=result,
            ) as resolve,
        ):
            await emy_bot.inline_search(
                update, SimpleNamespace(application=application)
            )

        search.assert_awaited_once_with("song name", application, limit=5)
        resolve.assert_called_once_with(candidates[0])
        query.answer.assert_awaited_once_with(
            [result], cache_time=0, is_personal=True
        )

    async def test_supported_url_sends_720p_preview_with_recognition_button(self):
        class FakeStatus:
            def __init__(self):
                self.delete = AsyncMock()

        class FakeMessage:
            text = "https://www.youtube.com/watch?v=abc"
            caption = None
            audio = voice = video = video_note = document = None
            message_id = 31

            def __init__(self):
                self.statuses = []

            async def reply_text(self, text, **kwargs):
                status = FakeStatus()
                self.statuses.append((text, kwargs, status))
                return status

            async def reply_video(self, **kwargs):
                self.preview = kwargs
                return SimpleNamespace(
                    message_id=33,
                    video=SimpleNamespace(
                        file_id="preview-file-id",
                        file_size=1024,
                    ),
                )

        emy_bot.RECOGNITION_REQUESTS.clear()
        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=9),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            preview = Path(temp_dir) / "preview.mp4"
            preview.write_bytes(b"preview-data")

            async def make_preview(_url, _work_dir):
                return preview, {"id": "abc", "height": 720}

            with patch.object(
                recognition, "download_url_preview", side_effect=make_preview
            ):
                await emy_bot.receive_recognition_input(
                    update, SimpleNamespace(bot=SimpleNamespace())
                )

        self.assertEqual(len(emy_bot.RECOGNITION_REQUESTS), 1)
        request = next(iter(emy_bot.RECOGNITION_REQUESTS.values()))
        self.assertEqual(request["kind"], "file")
        self.assertEqual(request["file_id"], "preview-file-id")
        self.assertFalse(request["processing"])
        self.assertEqual(
            message.preview["video"].filename,
            "abc_720p.mp4",
        )
        button = message.preview["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Recognize Song")
        self.assertEqual(button["callback_data"], f"recognize:{next(iter(emy_bot.RECOGNITION_REQUESTS))}")
        self.assertEqual(len(message.statuses), 1)
        status_text, _, status = message.statuses[0]
        self.assertIn("Fetching Video", status_text)
        self.assertIn('emoji-id="5427181942934088912"', status_text)
        status.delete.assert_awaited_once()
        emy_bot.RECOGNITION_REQUESTS.clear()

    async def test_duplicate_recognition_click_is_not_processed_twice(self):
        token = "test-token"
        emy_bot.RECOGNITION_REQUESTS[token] = {
            "requester_id": 7,
            "created": 1,
            "expires": 9999999999,
            "processing": True,
        }
        query = SimpleNamespace(
            data=f"recognize:{token}",
            from_user=SimpleNamespace(id=7),
            answer=AsyncMock(),
        )
        await emy_bot._recognize_song_callback(
            query, SimpleNamespace()
        )

        query.answer.assert_awaited_once_with("Recognition is already in progress.")
        emy_bot.RECOGNITION_REQUESTS.pop(token, None)

    async def test_recognized_match_is_delivered_without_matched_interstitial(self):
        token = "deliver-token"
        request = {
            "kind": "url",
            "url": "https://www.youtube.com/watch?v=abc",
            "preview_video": True,
            "requester_id": 7,
            "created": 1,
            "expires": 9999999999,
            "chat_id": 9,
            "processing": False,
        }
        emy_bot.RECOGNITION_REQUESTS[token] = request

        class FakeStatus:
            def __init__(self):
                self.edited = []
                self.delete = AsyncMock()

            async def edit_text(self, text, **_kwargs):
                self.edited.append(text)

        status = FakeStatus()
        message = SimpleNamespace(
            message_id=33,
            chat=SimpleNamespace(id=9),
        )
        query = SimpleNamespace(
            data=f"recognize:{token}",
            message=message,
            from_user=SimpleNamespace(id=7),
            answer=AsyncMock(),
        )
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=status)
        )
        context = SimpleNamespace(
            bot=fake_bot,
            application=SimpleNamespace(),
        )
        candidate = {
            "id": "song-id",
            "track": "Song Name",
            "artist": "Artist Name",
        }
        with patch.object(
            recognition,
            "download_url_sample",
            new=AsyncMock(return_value=Path("sample.mp3")),
        ), patch.object(
            recognition,
            "recognize_sample",
            new=AsyncMock(return_value={
                "title": "Song Name",
                "artist": "Artist Name",
            }),
        ), patch.object(
            emy_bot,
            "resolve_official_async",
            new=AsyncMock(return_value=candidate),
        ), patch.object(
            emy_bot,
            "song",
            new=AsyncMock(),
        ) as deliver_song:
            await emy_bot._recognize_song_callback(query, context)

        self.assertEqual(status.edited, [])
        self.assertEqual(
            fake_bot.send_message.await_args.args[1],
            emy_bot.recognition_status_html(),
        )
        self.assertEqual(
            fake_bot.send_message.await_args.kwargs.get("parse_mode"), "HTML"
        )
        status.delete.assert_awaited_once()
        deliver_song.assert_awaited_once()
        self.assertEqual(
            deliver_song.await_args.args[1].recognition_candidate,
            candidate,
        )
        self.assertNotIn(token, emy_bot.RECOGNITION_REQUESTS)


if __name__ == "__main__":
    unittest.main()
