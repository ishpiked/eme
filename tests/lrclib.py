import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lrclib


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self, **_):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, *, params, headers):
        self.requests.append((url, params, headers))
        return self.responses.pop(0)


class LRCLIBClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_track_lookup_returns_plain_lyrics(self):
        session = FakeSession([FakeResponse(200, {
            "trackName": "Tum Hi Ho",
            "artistName": "Arijit Singh",
            "plainLyrics": "Line one\nLine two",
            "syncedLyrics": None,
        })])

        result = await lrclib.find_lyrics(
            session, "Tum Hi Ho", "Arijit Singh", album="Aashiqui 2", duration=262
        )

        self.assertEqual(result, {
            "title": "Tum Hi Ho",
            "artist": "Arijit Singh",
            "lyrics": "Line one\nLine two",
        })
        self.assertEqual(session.requests[0][1], {
            "track_name": "Tum Hi Ho",
            "artist_name": "Arijit Singh",
            "album_name": "Aashiqui 2",
            "duration": "262",
        })

    async def test_search_fallback_rejects_unrelated_results(self):
        session = FakeSession([
            FakeResponse(404, {}),
            FakeResponse(200, [
                {
                    "trackName": "Different Song",
                    "artistName": "Arijit Singh",
                    "plainLyrics": "Wrong lyrics",
                },
                {
                    "trackName": "Tum Hi Ho",
                    "artistName": "Arijit Singh",
                    "plainLyrics": "Correct lyrics",
                },
            ]),
        ])

        result = await lrclib.find_lyrics(
            session, "Tum Hi Ho", "Arijit Singh"
        )

        self.assertEqual(result["lyrics"], "Correct lyrics")
        self.assertEqual(session.requests[1][1], {"q": "Tum Hi Ho"})

    async def test_title_only_lookup_uses_title_for_exact_and_search_requests(self):
        session = FakeSession([
            FakeResponse(404, {}),
            FakeResponse(200, [{
                "trackName": "Isq Risk",
                "artistName": "Sohail Sen",
                "plainLyrics": "Lyrics",
            }]),
        ])
        result = await lrclib.find_lyrics(session, "Isq Risk")

        self.assertEqual(session.requests[0][1], {"track_name": "Isq Risk"})
        self.assertEqual(session.requests[1][1], {"q": "Isq Risk"})
        self.assertEqual(result["lyrics"], "Lyrics")

    async def test_exact_lookup_error_falls_back_to_search(self):
        session = FakeSession([
            FakeResponse(400, {"message": "missing field `artist_name`"}),
            FakeResponse(200, [{
                "trackName": "Tum Hi Ho",
                "artistName": "Arijit Singh",
                "plainLyrics": "Fallback lyrics",
            }]),
        ])

        result = await lrclib.find_lyrics(session, "Tum Hi Ho")

        self.assertEqual(result["lyrics"], "Fallback lyrics")
        self.assertEqual(session.requests[1][1], {"q": "Tum Hi Ho"})

    async def test_missing_track_returns_none_and_lyrics_split_within_limit(self):
        session = FakeSession([FakeResponse(404, {}), FakeResponse(200, [])])
        self.assertIsNone(await lrclib.find_lyrics(session, "Missing", "Artist"))

        chunks = lrclib.split_lyrics(("line\n" * 2000).strip(), limit=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
