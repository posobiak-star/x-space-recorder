import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import archive


class ArchiveTests(unittest.TestCase):
    def make_session(self, directory, *, logs=None, done=None):
        return archive.SpaceArchiveSession(
            url="https://x.com/i/spaces/abcdefghijklm", space_id="abcdefghijklm",
            output_dir=Path(directory), audio_format="source", browser=None,
            ytdlp_prefix=["yt-dlp"], ffmpeg_location="C:/ffmpeg",
            on_log=logs.append if logs is not None else lambda _: None,
            on_done=done if done is not None else lambda *_: None,
        )

    def test_progress_counts_duration_and_redownload_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory)
            session._remember_playlist("https://cdn.example/history.m3u8", "#EXTM3U\n#EXTINF:2.5,title\nfirst.aac\n#EXTINF:1.25,\nsecond.aac\n")
            with patch.object(session, "_read_url", return_value=b"audio"):
                session._download_segment(0, "first")
                session._download_segment(1, "second")
                session._segment_path(0).unlink()
                session._missing_known_segments()
                session._download_segment(0, "first")
            self.assertEqual(session.progress.downloaded_chunks, 2)
            self.assertEqual(session.progress.downloaded_bytes, 10)
            self.assertAlmostEqual(session.progress.audio_seconds, 3.75)

    def test_derive_history_url_preserves_additional_query_parameters(self):
        session = self.make_session("out")
        with patch.object(session, "_read_url", return_value="#EXTM3U\nplaylist_123.m3u8\n") as read:
            url = session._derive_live_replay_url("https://cdn.example/audio/dynamic_playlist.m3u8?type=live&token=abc%2B123")
        self.assertEqual(url, "https://cdn.example/audio/playlist_123.m3u8?token=abc%2B123&type=replay")
        self.assertEqual(read.call_args_list[0].args[0], "https://cdn.example/audio/master_playlist.m3u8?token=abc%2B123")

    def test_failed_conversion_keeps_chunks_without_publishing_broken_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory) / ".recording-test"
            session._temp_dir.mkdir()
            session._segment_path(1).write_bytes(b"audio")

            def failed_conversion(command, **_kwargs):
                Path(command[-1]).write_bytes(b"partial")
                return SimpleNamespace(returncode=1, stderr="conversion failed")

            with patch("archive.subprocess.run", side_effect=failed_conversion):
                with self.assertRaises(archive.SpaceArchiveError):
                    session._finalize("Space")
            self.assertFalse((Path(directory) / "Space.m4a").exists())
            self.assertTrue(session._segment_path(1).is_file())

    def test_successful_conversion_preserves_existing_file_and_ignores_combined_aac(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory) / ".recording-test"
            session._temp_dir.mkdir()
            session._segment_path(1).write_bytes(b"audio")
            (session._temp_dir / "combined.aac").write_bytes(b"old combined")
            (Path(directory) / "Space.m4a").write_bytes(b"original")

            def successful_conversion(command, **_kwargs):
                Path(command[-1]).write_bytes(b"converted")
                return SimpleNamespace(returncode=0, stderr="")

            with patch("archive.subprocess.run", side_effect=successful_conversion):
                output = session._finalize("Space")
            self.assertEqual(output.name, "Space (2).m4a")
            self.assertEqual(output.read_bytes(), b"converted")
            self.assertEqual((Path(directory) / "Space.m4a").read_bytes(), b"original")
            self.assertFalse(session._temp_dir.exists())

    def test_error_joins_live_worker_before_done_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            stopped = threading.Event()
            outcomes = []

            def done(success, output, error):
                self.assertTrue(stopped.is_set())
                outcomes.append((success, output, error))

            session = self.make_session(directory, done=done)
            original_phase = session._set_phase

            def fail_after_start(phase):
                if phase == "live":
                    raise RuntimeError("forced failure")
                original_phase(phase)

            def poll(_url):
                session.cancel_event.wait(2)
                stopped.set()

            with (
                patch.object(session, "_extract_info", return_value={"live_status": "is_live", "url": "live"}),
                patch.object(session, "_derive_live_replay_url", return_value="history"),
                patch.object(session, "_poll_live", side_effect=poll),
                patch.object(session, "_download_history"),
                patch.object(session, "_set_phase", side_effect=fail_after_start),
            ):
                session._run()
            self.assertEqual(outcomes, [(False, None, "forced failure")])
            self.assertTrue(session.cancel_event.is_set())

    def test_initial_history_failure_keeps_live_recording_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            repaired = threading.Event()
            outcomes = []
            session = self.make_session(directory, done=lambda *args: outcomes.append(args))

            def repair(_history, _live):
                session._segment_path(0).write_bytes(b"audio")
                repaired.set()

            def poll(_url):
                repaired.wait(2)
                session.stop()

            with (
                patch.object(session, "_extract_info", return_value={"live_status": "is_live", "url": "live"}),
                patch.object(session, "_derive_live_replay_url", return_value="history"),
                patch.object(session, "_poll_live", side_effect=poll),
                patch.object(session, "_download_history", side_effect=OSError("temporary failure")),
                patch.object(session, "_poll_history_repairs", side_effect=repair),
                patch.object(session, "_finalize", return_value=Path(directory) / "Space.m4a"),
            ):
                session._run()
            self.assertTrue(repaired.is_set())
            self.assertTrue(outcomes[0][0])

    def test_failure_without_audio_leaves_no_temporary_folder(self):
        for info in (
            {"live_status": "was_live", "url": ""},
            {"live_status": "is_upcoming", "url": ""},
        ):
            with self.subTest(info=info), tempfile.TemporaryDirectory() as directory:
                outcomes = []
                session = self.make_session(directory, done=lambda *args: outcomes.append(args))
                with patch.object(session, "_extract_info", return_value=info):
                    session._run()
                self.assertFalse(outcomes[0][0])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failure_with_audio_keeps_chunks_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = []
            session = self.make_session(directory, done=lambda *args: outcomes.append(args))

            def partial_history(_url):
                session._segment_path(0).write_bytes(b"audio")
                raise archive.SpaceArchiveError("failed")

            with (
                patch.object(session, "_extract_info", return_value={"live_status": "was_live", "url": "replay"}),
                patch.object(session, "_download_history", side_effect=partial_history),
            ):
                session._run()
            self.assertFalse(outcomes[0][0])
            self.assertEqual(session.recovery_dir, session._temp_dir)
            self.assertTrue(session._segment_path(0).is_file())

    def test_final_repair_retries_temporary_failure(self):
        session = self.make_session("out")
        session._history_replay_url = "history"
        with (
            patch("archive.FINAL_REPAIR_RETRY_SECONDS", 0),
            patch.object(session, "_repair_history_once", side_effect=[OSError("temporary"), 1]) as repair,
        ):
            session._final_repair("live")
        self.assertEqual(repair.call_count, 2)

    def test_final_repair_gives_up_after_limit_and_keeps_going_to_save(self):
        logs = []
        session = self.make_session("out", logs=logs)
        session._history_replay_url = "history"
        with (
            patch("archive.FINAL_REPAIR_RETRY_SECONDS", 0),
            patch.object(session, "_repair_history_once", side_effect=OSError("unavailable")) as repair,
        ):
            session._final_repair("live")
        self.assertEqual(repair.call_count, archive.FINAL_REPAIR_ATTEMPTS)
        self.assertTrue(any("最終補完用の履歴を確認できませんでした: unavailable" in message for message in logs))

    def test_manual_stop_ends_final_repair_retries(self):
        session = self.make_session("out")
        session._history_replay_url = "history"

        def stop_then_fail(*_args, **_kwargs):
            session.stop()
            raise OSError("temporary")

        with patch.object(session, "_repair_history_once", side_effect=stop_then_fail) as repair:
            session._final_repair("live")
        self.assertEqual(repair.call_count, 1)

    def test_disk_failure_is_not_retried_by_final_repair(self):
        logs = []
        session = self.make_session("out", logs=logs)
        session._history_replay_url = "history"
        with patch.object(session, "_repair_history_once", side_effect=archive.SpaceArchiveStorageError("disk full")) as repair:
            session._final_repair("live")
        self.assertEqual(repair.call_count, 1)
        self.assertTrue(any("最終補完を中断しました: disk full" in message for message in logs))

    def test_stop_before_audio_is_a_normal_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = []
            session = self.make_session(directory, done=lambda *args: outcomes.append(args))
            session.stop()
            with patch.object(session, "_extract_info", side_effect=archive.SpaceArchiveCancelled("stopped")):
                session._run()
            self.assertEqual(outcomes, [(True, None, None)])
            self.assertEqual(session.progress.phase, "cancelled")

    def test_disk_failure_stops_batch_instead_of_retrying_it_as_a_network_error(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory)
            with (
                patch.object(session, "_read_url", return_value=b"audio") as read,
                patch("archive.Path.write_bytes", side_effect=OSError("disk full")),
            ):
                with self.assertRaises(archive.SpaceArchiveStorageError):
                    session._download_batch([(0, "audio")], workers=1)
            self.assertEqual(read.call_count, 1)
            self.assertTrue(session.cancel_event.is_set())
            self.assertTrue(session.stop_event.is_set())

    def test_unchanged_repair_failure_is_not_logged_every_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = []
            session = self.make_session(directory, logs=logs)
            session._temp_dir = Path(directory)
            session._remember_segments([(0, "missing")])
            body = "#EXTM3U\n#EXTINF:3.0,\nmissing.aac\n"
            with (
                patch.object(session, "_read_url", return_value=body),
                patch.object(session, "_download_batch", return_value=1),
            ):
                session._repair_history_once("https://cdn.example/history")
                session._repair_history_once("https://cdn.example/history")
            self.assertEqual(sum("自動補完待ち:" in message for message in logs), 1)
            self.assertEqual(sum("欠落を検知:" in message for message in logs), 1)

    def test_new_live_edge_chunk_is_downloaded_without_false_gap_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = []
            session = self.make_session(directory, logs=logs)
            session._temp_dir = Path(directory)
            session._remember_segments([(0, "saved")])
            session._segment_path(0).write_bytes(b"audio")
            body = "#EXTM3U\n#EXTINF:3.0,\nsaved.aac\n#EXTINF:3.0,\nnew.aac\n"
            with patch.object(session, "_read_url", side_effect=lambda _url, *, binary=False, timeout=30: b"audio" if binary else body):
                self.assertEqual(session._repair_history_once("https://cdn.example/history"), 1)
            self.assertFalse(any("欠落" in message for message in logs))

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

    def test_derive_history_url_follows_absolute_master_entry(self):
        session = self.make_session("out")
        master = "#EXTM3U\n/b/Transcoding/v1/hls/abc/playlist_123.m3u8\n"
        missing = urllib.error.HTTPError("https://cdn.example/x", 404, "Not Found", None, None)
        self.addCleanup(missing.close)
        with patch.object(session, "_read_url", side_effect=[master, missing]):
            url = session._derive_live_replay_url("https://cdn.example/x/audio-space/dynamic_playlist.m3u8?type=live")
        self.assertEqual(url, "https://cdn.example/b/Transcoding/v1/hls/abc/playlist_123.m3u8?type=replay")

    def test_derive_history_url_prefers_complete_replay_store(self):
        session = self.make_session("out")
        master = "#EXTM3U\n/b/Transcoding/v1/hls/tok/transcode/pdxa/replays/jwt/audio-space/playlist_123.m3u8\n"
        with patch.object(session, "_read_url", return_value=master):
            url = session._derive_live_replay_url("https://cdn.example/Transcoding/v1/hls/tok/non_transcode/atla/live/audio-space/dynamic_playlist.m3u8?type=live")
        self.assertEqual(url, "https://cdn.example/Transcoding/v1/hls/tok/non_transcode/pdxa/replays/audio-space/playlist_123.m3u8?type=replay")

    def test_derive_history_url_does_not_downgrade_on_temporary_error(self):
        session = self.make_session("out")
        master = "#EXTM3U\n/b/Transcoding/v1/hls/tok/transcode/pdxa/replays/jwt/audio-space/playlist_123.m3u8\n"
        unavailable = urllib.error.HTTPError("https://cdn.example/x", 503, "Unavailable", None, None)
        self.addCleanup(unavailable.close)
        with patch.object(session, "_read_url", side_effect=[master, unavailable]):
            with self.assertRaises(urllib.error.HTTPError):
                session._derive_live_replay_url("https://cdn.example/x/audio-space/dynamic_playlist.m3u8?type=live")

    def test_transcode_playlist_chunks_resolve_next_to_live_playlist(self):
        session = self.make_session("out")
        session._live_url = "https://cdn.example/x/audio-space/dynamic_playlist.m3u8?type=live"
        body = "#EXTM3U\n#EXTINF:3.0,\nchunk_100_5_b.aac\n"
        self.assertEqual(
            session._remember_playlist("https://cdn.example/b/Transcoding/transcode/playlist.m3u8?type=replay", body),
            [(5, "https://cdn.example/x/audio-space/chunk_100_5_b.aac")],
        )

    def test_history_repairs_always_read_the_newest_snapshot(self):
        session = self.make_session("out")
        session._live_url = "live"
        seen = []

        def repair(url):
            seen.append(url)
            if len(seen) == 2:
                session.stop_event.set()

        with (
            patch("archive.HISTORY_REPAIR_INTERVAL_SECONDS", 0),
            patch.object(session, "_derive_live_replay_url", side_effect=["snapshot-1", "snapshot-2"]),
            patch.object(session, "_repair_history_once", side_effect=repair),
        ):
            session._poll_history_repairs("snapshot-0", "live")
        self.assertEqual(seen, ["snapshot-1", "snapshot-2"])
        self.assertEqual(session._history_replay_url, "snapshot-2")

    def test_final_repair_prefers_newest_snapshot_and_falls_back_to_older_one(self):
        session = self.make_session("out")
        session._live_url = "live"
        session._history_replay_url = "old"
        with patch.object(session, "_derive_live_replay_url", return_value="new"), patch.object(session, "_repair_history_once") as repair:
            session._final_repair("live")
        repair.assert_called_once_with("new", final=True)
        session._history_replay_url = "old"
        with patch.object(session, "_derive_live_replay_url", side_effect=OSError("gone")), patch.object(session, "_repair_history_once") as repair:
            session._final_repair("live")
        repair.assert_called_once_with("old", final=True)

    def test_space_without_replay_still_repairs_from_readable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)

            def poll(_url):
                session._ended_without_replay = True
                session.stop_event.set()

            with (
                patch.object(session, "_extract_info", return_value={"live_status": "is_live", "url": "live"}),
                patch.object(session, "_derive_live_replay_url", return_value="history"),
                patch.object(session, "_poll_live", side_effect=poll),
                patch.object(session, "_download_history"),
                patch.object(session, "_poll_history_repairs"),
                patch.object(session, "_final_repair") as final_repair,
                patch.object(session, "_finalize", return_value=Path(directory) / "Space.m4a"),
            ):
                session._run()
            final_repair.assert_called_once_with("live")

    def test_final_repair_retries_while_chunks_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory)
            session._history_replay_url = "history"
            session._remember_segments([(1, "chunk")])

            def repair(*_args, **_kwargs):
                if repair_calls.call_count == 2:
                    session._segment_path(1).write_bytes(b"audio")
                return 0

            with (
                patch("archive.FINAL_REPAIR_RETRY_SECONDS", 0),
                patch.object(session, "_repair_history_once", side_effect=repair) as repair_calls,
            ):
                session._final_repair("live")
            self.assertEqual(repair_calls.call_count, 2)

    def test_public_replay_chunks_stay_next_to_their_own_playlist(self):
        session = self.make_session("out")
        session._live_url = "https://live.example/x/dynamic_playlist.m3u8?type=live"
        body = "#EXTM3U\n#EXTINF:3.0,\nchunk_100_5_b.aac\n"
        self.assertEqual(
            session._remember_playlist("https://replay.example/y/playlist.m3u8?type=replay", body),
            [(5, "https://replay.example/y/chunk_100_5_b.aac")],
        )

    def test_segment_number_is_read_from_any_chunk_suffix(self):
        for name in ("chunk_100_7_a.aac", "chunk_100_7_b.aac", "chunk_100_7_b.aac?type=live"):
            with self.subTest(name=name):
                body = f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:3.0,\n{name}\n"
                self.assertEqual(archive.SpaceArchiveSession.parse_playlist("https://cdn.example/p.m3u8", body)[0][0], 7)

    def test_missing_chunk_is_not_retried_with_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.make_session(directory)
            session._temp_dir = Path(directory)
            missing = urllib.error.HTTPError("https://cdn.example/1.aac", 404, "Not Found", None, None)
            self.addCleanup(missing.close)
            with patch.object(session, "_read_url", side_effect=missing) as read:
                with self.assertRaises(urllib.error.HTTPError):
                    session._download_segment(1, "https://cdn.example/1.aac")
            self.assertEqual(read.call_count, 1)

    def test_history_endlist_does_not_end_live_recording(self):
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

            self.assertFalse(session.stop_event.is_set())

    def test_initial_history_endlist_does_not_end_live_recording(self):
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

            self.assertFalse(session.stop_event.is_set())
            self.assertTrue(any("ライブ音声を継続取得中" in message for message in logs))

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
