import tempfile
import threading
import unittest
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

            def read_url(_url, *, binary=False, timeout=30):
                return b"audio" if binary else playlist

            with patch.object(session, "_read_url", side_effect=read_url):
                repaired = session._repair_history_once("https://cdn.example/replay.m3u8")

            self.assertEqual(repaired, 2)
            self.assertTrue(session._segment_path(41).is_file())
            self.assertTrue(session._segment_path(42).is_file())
            self.assertTrue(any("自動補完完了" in message for message in logs))

    def test_history_retry_failure_does_not_discard_downloaded_audio(self):
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
            playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:40
#EXTINF:3.0,
40.aac
#EXTINF:3.0,
41.aac
#EXTINF:3.0,
42.aac
#EXTINF:3.0,
43.aac
"""
            retried = []

            def initial_download(_segments):
                session._segment_path(40).write_bytes(b"audio")
                session._segment_path(43).write_bytes(b"audio")
                return 2

            def failed_retry(sequence, _url):
                retried.append(sequence)
                raise OSError("chunk unavailable")

            with (
                patch.object(session, "_read_url", return_value=playlist),
                patch.object(session, "_download_batch", side_effect=initial_download),
                patch.object(session, "_download_segment", side_effect=failed_retry),
            ):
                session._download_history("https://cdn.example/replay.m3u8")

            self.assertEqual(retried, [41, 42])
            self.assertTrue(session._segment_path(40).is_file())
            self.assertTrue(session._segment_path(43).is_file())
            self.assertTrue(any("再試行対象: 2チャンク" in message for message in logs))

    def test_final_repair_log_does_not_promise_another_retry(self):
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
            playlist = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:41\n#EXTINF:3.0,\n41.aac\n"
            with (
                patch.object(session, "_read_url", return_value=playlist),
                patch.object(session, "_download_batch", return_value=1),
            ):
                session._repair_history_once("https://cdn.example/replay.m3u8", final=True)

            self.assertTrue(any("最終補完で未取得: 1チャンク" in message for message in logs))
            self.assertFalse(any("配信中は履歴を再確認" in message for message in logs))

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

    def test_history_endlist_triggers_finalization(self):
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
            session._segment_path(50).write_bytes(b"audio")
            playlist = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:50\n#EXTINF:3.0,\n50.aac\n#EXT-X-ENDLIST\n"

            with patch.object(session, "_read_url", return_value=playlist):
                session._repair_history_once("https://cdn.example/replay.m3u8")

            self.assertTrue(session.stop_event.is_set())
            self.assertTrue(any("履歴からSpaceの終了を検知" in message for message in logs))

    def test_initial_history_endlist_triggers_finalization(self):
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
            playlist = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:70\n#EXTINF:3.0,\n70.aac\n#EXT-X-ENDLIST\n"

            with (
                patch.object(session, "_read_url", return_value=playlist),
                patch.object(session, "_download_batch", return_value=0),
            ):
                session._download_history("https://cdn.example/replay.m3u8", continuing_live=True)

            self.assertTrue(session.stop_event.is_set())
            self.assertTrue(any("履歴からSpaceの終了を検知" in message for message in logs))

    def test_status_check_keeps_live_space_running(self):
        logs = []
        session = archive.SpaceArchiveSession(
            url="https://x.com/i/spaces/abcdefghijklm",
            space_id="abcdefghijklm",
            output_dir=Path("out"),
            audio_format="source",
            browser=None,
            ytdlp_prefix=["yt-dlp"],
            ffmpeg_location="C:/ffmpeg",
            on_log=logs.append,
            on_done=lambda *_: None,
        )
        live_url = "https://cdn.example/dynamic_playlist.m3u8?type=live"
        with patch.object(
            session,
            "_extract_info",
            return_value={"live_status": "is_live", "url": live_url},
        ):
            result = session._check_space_status()
            repeated_result = session._check_space_status()

        self.assertEqual(result, "live")
        self.assertEqual(repeated_result, "live")
        self.assertFalse(session.stop_event.is_set())
        self.assertEqual(session._status_live_url, live_url)
        self.assertEqual(sum("Spaceは進行中" in message for message in logs), 1)

    def test_status_check_ends_with_public_replay(self):
        session = archive.SpaceArchiveSession(
            url="https://x.com/i/spaces/abcdefghijklm",
            space_id="abcdefghijklm",
            output_dir=Path("out"),
            audio_format="source",
            browser=None,
            ytdlp_prefix=["yt-dlp"],
            ffmpeg_location="C:/ffmpeg",
            on_log=lambda _: None,
            on_done=lambda *_: None,
        )
        replay_url = "https://cdn.example/replay.m3u8"
        with patch.object(
            session,
            "_extract_info",
            return_value={"live_status": "was_live", "url": replay_url},
        ):
            result = session._check_space_status()

        self.assertEqual(result, "ended_with_replay")
        self.assertTrue(session.stop_event.is_set())
        self.assertEqual(session._status_replay_url, replay_url)
        self.assertFalse(session._ended_without_replay)

    def test_status_check_ends_without_public_replay(self):
        session = archive.SpaceArchiveSession(
            url="https://x.com/i/spaces/abcdefghijklm",
            space_id="abcdefghijklm",
            output_dir=Path("out"),
            audio_format="source",
            browser=None,
            ytdlp_prefix=["yt-dlp"],
            ffmpeg_location="C:/ffmpeg",
            on_log=lambda _: None,
            on_done=lambda *_: None,
        )
        with patch.object(
            session,
            "_extract_info",
            return_value={"live_status": "was_live", "formats": []},
        ):
            result = session._check_space_status()

        self.assertEqual(result, "ended_without_replay")
        self.assertTrue(session.stop_event.is_set())
        self.assertTrue(session._ended_without_replay)

    def test_manual_stop_prevents_new_segment_downloads(self):
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
            session.stop()

            with patch.object(session, "_read_url") as read_url:
                downloaded = session._download_segment(100, "https://cdn.example/100.aac")

            self.assertFalse(downloaded)
            read_url.assert_not_called()

    def test_manual_stop_cancels_queued_batch_downloads(self):
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
            started = threading.Event()

            def read_url(_url, *, binary=False, timeout=30):
                started.set()
                session.manual_stop_event.wait(2)
                return b"audio"

            segments = [(sequence, f"https://cdn.example/{sequence}.aac") for sequence in range(50)]
            with patch.object(session, "_read_url", side_effect=read_url):
                worker = threading.Thread(target=session._download_batch, args=(segments,))
                worker.start()
                self.assertTrue(started.wait(1))
                session.stop()
                worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertLessEqual(len(list(Path(directory).glob("*.aac"))), 8)


if __name__ == "__main__":
    unittest.main()
