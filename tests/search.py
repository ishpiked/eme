import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch

import ytmusic


def _raw_song(video_id, title="Song", artists=("Artist",), album="Album",
              duration_seconds=200, views="13K", year=2024):
    return {
        "category": "Songs",
        "resultType": "song",
        "title": title,
        "album": {"name": album, "id": "album-id"},
        "videoId": video_id,
        "duration_seconds": duration_seconds,
        "year": year,
        "artists": [{"name": name, "id": f"id-{name}"} for name in artists],
        "views": views,
        "thumbnails": [
            {"url": f"https://img/{video_id}-s", "width": 60, "height": 60},
            {"url": f"https://img/{video_id}-b", "width": 120, "height": 120},
        ],
    }


class YTMusicSearchTests(unittest.TestCase):
    def test_normalize_query_collapses_case_and_whitespace(self):
        self.assertEqual(ytmusic.normalize_query("  Tum HI   Ho "), "tum hi ho")

    def test_parse_views_handles_suffixes_and_commas(self):
        self.assertEqual(ytmusic.parse_views("1.2B"), 1200000000)
        self.assertEqual(ytmusic.parse_views("345M"), 345000000)
        self.assertEqual(ytmusic.parse_views("13K"), 13000)
        self.assertEqual(ytmusic.parse_views("1,234"), 1234)
        self.assertEqual(ytmusic.parse_views(None), 0)
        self.assertEqual(ytmusic.parse_views(""), 0)
        self.assertEqual(ytmusic.parse_views(42), 42)

    def test_parse_duration_prefers_seconds_then_clock_text(self):
        self.assertEqual(ytmusic.parse_duration(262, None), 262)
        self.assertEqual(ytmusic.parse_duration("4:22", None), 262)
        self.assertEqual(ytmusic.parse_duration(None, "1:01:01"), 3661)
        self.assertEqual(ytmusic.parse_duration(None, None), 0)
        self.assertEqual(ytmusic.parse_duration("", ""), 0)

    def test_best_thumbnail_picks_largest_url(self):
        thumbs = [
            {"url": "small", "width": 60, "height": 60},
            {"url": "big", "width": 120, "height": 120},
        ]
        self.assertEqual(ytmusic.best_thumbnail(thumbs), "big")
        self.assertIsNone(ytmusic.best_thumbnail([]))
        self.assertIsNone(ytmusic.best_thumbnail(None))

    def test_normalize_item_maps_bot_compatible_fields(self):
        entry = ytmusic.normalize_item(_raw_song("abc", "Tum Hi Ho", ("Arijit Singh",)))
        self.assertEqual(entry["id"], "abc")
        self.assertEqual(entry["title"], "Tum Hi Ho")
        self.assertEqual(entry["track"], "Tum Hi Ho")
        self.assertEqual(entry["artist"], "Arijit Singh")
        self.assertEqual(entry["artists"], ["Arijit Singh"])
        self.assertEqual(entry["album"], "Album")
        self.assertEqual(entry["duration"], 200)
        self.assertEqual(entry["thumbnail"], "https://img/abc-b")
        self.assertEqual(entry["channel"], "Arijit Singh")
        self.assertEqual(entry["uploader"], "Arijit Singh")
        self.assertEqual(entry["view_count"], 13000)

    def test_normalize_item_rejects_hits_without_video_id(self):
        self.assertIsNone(ytmusic.normalize_item({"title": "No id"}))
        self.assertIsNone(ytmusic.normalize_item(None))

    def test_search_prefers_songs_then_fills_with_videos(self):
        songs = [_raw_song("song-1"), _raw_song("song-2")]
        videos = [_raw_song("video-1")]
        videos[0]["resultType"] = "video"

        class FakeClient:
            def search(self, query, filter=None, limit=None):
                return songs if filter == "songs" else videos

        with patch("ytmusicapi.YTMusic", return_value=FakeClient()):
            results = ytmusic.search("query", limit=10)
        self.assertEqual(
            [entry["id"] for entry in results],
            ["song-1", "song-2", "video-1"],
        )

    def test_search_dedupes_and_respects_limit(self):
        songs = [_raw_song("dup"), _raw_song("song-2")]
        videos = [_raw_song("dup"), _raw_song("video-2")]
        videos[0]["resultType"] = "video"
        videos[1]["resultType"] = "video"

        class FakeClient:
            def search(self, query, filter=None, limit=None):
                return songs if filter == "songs" else videos

        with patch("ytmusicapi.YTMusic", return_value=FakeClient()):
            results = ytmusic.search("query", limit=2)
        self.assertEqual([entry["id"] for entry in results], ["dup", "song-2"])

    def test_search_skips_video_request_when_song_results_fill_limit(self):
        songs = [_raw_song("song-1"), _raw_song("song-2")]
        calls = []

        class FakeClient:
            def search(self, query, filter=None, limit=None):
                calls.append((filter, limit))
                return songs

        with patch("ytmusicapi.YTMusic", return_value=FakeClient()):
            results = ytmusic.search("query", limit=2)

        self.assertEqual([entry["id"] for entry in results], ["song-1", "song-2"])
        self.assertEqual(calls, [("songs", 2)])

    def test_search_returns_empty_on_blank_query_or_failure(self):
        self.assertEqual(ytmusic.search("   ", limit=10), [])

        class FailingClient:
            def search(self, *args, **kwargs):
                raise RuntimeError("offline")

        with patch("ytmusicapi.YTMusic", return_value=FailingClient()):
            self.assertEqual(ytmusic.search("query", limit=10), [])

    def test_search_one_returns_top_hit_or_none(self):
        songs = [_raw_song("top")]

        class FakeClient:
            def search(self, query, filter=None, limit=None):
                return songs if filter == "songs" else []

        with patch("ytmusicapi.YTMusic", return_value=FakeClient()):
            self.assertEqual(ytmusic.search_one("query")["id"], "top")
            self.assertIsNone(ytmusic.search_one("   "))


if __name__ == "__main__":
    unittest.main()
