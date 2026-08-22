import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import archive


class ArchiveTests(unittest.TestCase):
    def test_parse_replay_playlist_uses_segment_sequence(self):
        body = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:3.0,
chunk_100_0_a.aac
#EXTINF:3.0,
chunk_101_1_a.aac
"""
        self.assertEqual(
            archive.SpaceArchiveSession.parse_playlist("https://cdn.example/audio/playlist.m3u8", body),
            [
                (0, "https://cdn.example/audio/chunk_100_0_a.aac"),
                (1, "https://cdn.example/audio/chunk_101_1_a.aac"),
            ],
        )

    def test_parse_live_playlist_uses_media_sequence_fallback(self):
        body = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:42
#EXTINF:3.0,
segment.aac
"""
        self.assertEqual(
            archive.SpaceArchiveSession.parse_playlist("https://cdn.example/live.m3u8", body)[0][0],
            42,
        )

    def test_derive_live_replay_url_keeps_audio_directory(self):
        session = archive.SpaceArchiveSession(
            url="https://x.com/i/spaces/abcdefghijklm",
            space_id="abcdefghijklm",
            output_dir=None,
            audio_format="source",
            browser=None,
            ytdlp_prefix=["yt-dlp"],
            ffmpeg_location="C:/ffmpeg",
            on_log=lambda _: None,
            on_done=lambda *_: None,
        )
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1
/other/transcode/audio-space/playlist_123.m3u8
"""
        dynamic = "https://cdn.example/non_transcode/audio-space/dynamic_playlist.m3u8?type=live"
        with patch.object(session, "_read_url", return_value=master):
            self.assertEqual(
                session._derive_live_replay_url(dynamic),
                "https://cdn.example/non_transcode/audio-space/playlist_123.m3u8?type=replay",
            )

    def test_unique_output_path_uses_title_and_avoids_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Space名.m4a").touch()
            self.assertEqual(
                archive.SpaceArchiveSession._unique_output_path(root, "Space名", "m4a"),
                root / "Space名 (2).m4a",
            )


if __name__ == "__main__":
    unittest.main()
