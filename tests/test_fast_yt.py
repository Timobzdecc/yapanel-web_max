from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from extmusic import fast_yt


class FastYTTest(unittest.TestCase):
    def test_empty_video_id_raises(self):
        with self.assertRaises(fast_yt.FastYTError):
            fast_yt.fast_resolve("")

    @patch("urllib.request.urlopen")
    def test_resolve_success(self, mock_urlopen):
        fake_response = MagicMock()
        fake_response.read.return_value = b"""{
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "title": "Test Title",
                "author": "Test Artist",
                "lengthSeconds": "180"
            },
            "streamingData": {
                "adaptiveFormats": [
                    {
                        "mimeType": "audio/webm; codecs=\\"opus\\"",
                        "bitrate": 160000,
                        "url": "https://rr1---sn-test.googlevideo.com/videoplayback?test=1"
                    }
                ]
            }
        }"""
        fake_response.__enter__.return_value = fake_response
        mock_urlopen.return_value = fake_response

        # Clear cache for isolated test
        fast_yt._CACHE.clear()

        res = fast_yt.fast_resolve("test_id_123")
        self.assertEqual(res["id"], "test_id_123")
        self.assertEqual(res["title"], "Test Title")
        self.assertEqual(res["artist"], "Test Artist")
        self.assertEqual(res["duration"], 180)
        self.assertEqual(res["acodec"], "opus")
        self.assertEqual(res["abr"], 160)
        self.assertTrue(res["url"].startswith("https://rr1---sn-test.googlevideo.com/"))

        # Verify caching
        res2 = fast_yt.fast_resolve("test_id_123")
        self.assertEqual(res2["url"], res["url"])
        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
