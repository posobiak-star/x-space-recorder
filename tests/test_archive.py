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

    def test_missing_known_segments_only_returns_entries_present_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            session = archive.SpaceArchiveSession(
                url="https://x.com/i/spaces/abcdefghijklm",
                space_id="abcdefghijklm",
                output_dir=Path(directory),
                audio_format="source",
                browser=None,
                ytdlp_prefix=["yt-dlp"],
                ffmpeg_location="C:/ffmpeg",
                on_log=lambda _: None,
                on_done=lambda *_: None,
            )
            session._temp_dir = Path(directory)
            session._remember_segments([(10, "ten"), (11, "eleven"), (13, "thirteen")])
            session._segment_path(10).write_bytes(b"audio")
            session._segment_path(13).write_bytes(b"audio")

            self.assertEqual(session._missing_known_segments(), [(11, "eleven")])

    def test_repair_history_downloads_missing_segment_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = []
            session = archive.SpaceArchiveSession(
                url="https://x.com/i/spaces/abcdefghijklm",
                space_id="abcdefghijklm",
                output_dir=Path(directory),
                audio_format="source",
                browser=None,
                ytdlp_prefix=["yt-dlp"],
                ffmpeg_location="C:/ffmpeg",
                on_log=logs.append,
                on_done=lambda *_: None,
            )
            session._temp_dir = Path(directory)
            session._remember_segments([(41, "https://cdn.example/41.aac")])

            playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:41
#EXTINF:3.0,
41.aac
#EXTINF:3.0,
42.aac
"""

            def read_url(_url, *, binary=False):
                return b"audio" if binary else playlist

            with patch.object(session, "_read_url", side_effect=read_url):
                repaired = session._repair_history_once("https://cdn.example/replay.m3u8")

            self.assertEqual(repaired, 2)
            self.assertTrue(session._segment_path(41).is_file())
            self.assertTrue(session._segment_path(42).is_file())
            self.assertTrue(any("自動補完完了" in message for message in logs))

    def test_live_endlist_triggers_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            session = archive.SpaceArchiveSession(
                url="https://x.com/i/spaces/abcdefghijklm",
                space_id="abcdefghijklm",
                output_dir=Path(directory),
                audio_format="source",
                browser=None,
                ytdlp_prefix=["yt-dlp"],
                ffmpeg_location="C:/ffmpeg",
                on_log=lambda _: None,
                on_done=lambda *_: None,
            )
            session._temp_dir = Path(directory)
            with patch.object(session, "_read_url", return_value="#EXTM3U\n#EXT-X-ENDLIST\n"):
                session._poll_live("https://cdn.example/live.m3u8")

            self.assertTrue(session.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
