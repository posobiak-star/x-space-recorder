import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import archive
import gui


class GuiTests(unittest.TestCase):
    def test_only_supported_login_options_are_shown(self):
        self.assertEqual(tuple(gui.LOGIN_OPTIONS), ("認証なし", "Firefox"))

    def test_firefox_download_uses_official_mozilla_url(self):
        self.assertEqual(gui.FIREFOX_DOWNLOAD_URL, "https://www.mozilla.org/firefox/new/")

    @patch("gui.shutil.which", return_value=None)
    def test_find_firefox_in_program_files(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Mozilla Firefox" / "firefox.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.dict(
                os.environ,
                {
                    "PROGRAMFILES": directory,
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                },
            ):
                self.assertEqual(gui.find_firefox(), executable)


class GuiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.settings_patch = patch("gui.preferences.preferences_path", return_value=Path(self.directory.name) / "settings.json")
        self.ffmpeg_patch = patch("gui.recorder.find_ffmpeg", return_value="C:/ffmpeg")
        self.settings_patch.start()
        self.ffmpeg_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.ffmpeg_patch.stop)
        self.app = gui.RecorderApp(self.root)
        self.app.output_dir.set(self.directory.name)

    def test_start_stop_and_save_keep_controls_and_result_in_sync(self):
        session = Mock()
        session.progress = archive.ArchiveProgress("history", "Space", 2, 10, 100, 6, 1)
        session.recovery_dir = None
        self.app.url.set("https://twitter.com/i/spaces/abcdefghijklm?s=20")
        with patch("gui.archive.SpaceArchiveSession", return_value=session):
            self.app.start_recording()
        self.assertEqual(self.app.url.get(), "https://x.com/i/spaces/abcdefghijklm")
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.folder_button.cget("state")), "disabled")
        self.assertFalse(self.app._settings_expanded)
        session.progress = archive.ArchiveProgress("live", "Space", 10, 10, 100, 30, 2)
        self.app.stop_recording()
        session.stop.assert_called_once()
        output = Path(self.directory.name) / "Space.m4a"
        session.progress = archive.ArchiveProgress("done", "Space", 10, 10, 100, 30, 3)
        self.app.events.put(("done", (True, output, None)))
        self.app._drain_events()
        self.assertIsNone(self.app.session)
        self.assertEqual(self.app.last_output, output)
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")
        self.assertEqual(self.app.status.get(), "保存完了")

    def test_idle_screen_hides_settings_progress_and_log(self):
        self.assertFalse(self.app._settings_expanded)
        self.assertEqual(self.app.options_panel.grid_info(), {})
        self.assertEqual(self.app.metrics_panel.grid_info(), {})
        self.assertEqual(self.app.progress_bar.grid_info(), {})
        self.assertEqual(self.app.log_body.grid_info(), {})
        self.assertNotEqual(self.app.status_label.grid_info(), {})
        self.assertIn("保存先:", self.app.settings_summary.get())

    def test_start_and_stop_buttons_swap_and_progress_appears(self):
        session = Mock()
        session.progress = archive.ArchiveProgress("live", "Space", 2, 10, 100, 6, 1)
        session.recovery_dir = None
        self.assertEqual(self.app.start_button.winfo_manager(), "pack")
        self.assertEqual(self.app.stop_button.winfo_manager(), "")
        self.app.url.set("https://x.com/i/spaces/abcdefghijklm")
        with patch("gui.archive.SpaceArchiveSession", return_value=session):
            self.app.start_recording()
        self.assertEqual(self.app.start_button.winfo_manager(), "")
        self.assertEqual(self.app.stop_button.winfo_manager(), "pack")
        self.assertNotEqual(self.app.metrics_panel.grid_info(), {})
        self.app.events.put(("done", (True, Path(self.directory.name) / "Space.m4a", None)))
        self.app._drain_events()
        self.assertEqual(self.app.start_button.winfo_manager(), "pack")
        self.assertEqual(self.app.stop_button.winfo_manager(), "")
        self.assertNotEqual(self.app.metrics_panel.grid_info(), {})

    def test_log_opens_only_for_errors_and_warnings(self):
        for line, expected in (
            ("音声チャンク取得済み: 100\n", False),
            ("注意: 手動停止のため省略します\n", False),
            ("履歴の自動補完待ち: timeout\n", False),
            ("警告: 1個の音声チャンクを補完できませんでした\n", True),
            ("エラー: 保存できませんでした\n", True),
        ):
            with self.subTest(line=line):
                self.app._toggle_log(False)
                self.app.events.put(("log", line))
                self.app._drain_events()
                self.assertEqual(self.app._log_expanded, expected)

    def test_invalid_url_shows_message_without_opening_log(self):
        self.app.url.set("https://example.com/")
        self.app.start_recording()
        self.assertFalse(self.app._log_expanded)
        self.assertIn("X SpaceのURL", self.app.status_detail.get())

    def test_failed_save_while_closing_keeps_window_open_and_shows_error(self):
        recovery = Path(self.directory.name) / ".recording-test"
        session = Mock()
        session.progress = archive.ArchiveProgress("error", "Space", 10, 10, 100, 30, 3)
        session.recovery_dir = recovery
        self.app.session = session
        self.app.close_when_done = True
        self.app.events.put(("done", (False, None, "disk full")))
        with (
            patch.object(self.app, "_shutdown") as shutdown,
            patch("gui.messagebox.showerror") as showerror,
        ):
            self.app._drain_events()
        shutdown.assert_not_called()
        self.assertFalse(self.app.close_when_done)
        message = showerror.call_args.args[1]
        self.assertIn("disk full", message)
        self.assertIn(str(recovery), message)

    def test_successful_save_while_closing_closes_the_app(self):
        session = Mock()
        session.progress = archive.ArchiveProgress("done", "Space", 10, 10, 100, 30, 3)
        session.recovery_dir = None
        self.app.session = session
        self.app.close_when_done = True
        self.app.events.put(("done", (True, Path(self.directory.name) / "Space.m4a", None)))
        with (
            patch.object(self.app, "_shutdown") as shutdown,
            patch("gui.messagebox.showerror") as showerror,
        ):
            self.app._drain_events()
        shutdown.assert_called_once()
        showerror.assert_not_called()

    def test_unwritable_folder_does_not_start_a_recording(self):
        self.app.url.set("https://x.com/i/spaces/abcdefghijklm")
        with (
            patch("gui.tempfile.TemporaryFile", side_effect=PermissionError("read only")),
            patch("gui.archive.SpaceArchiveSession") as session,
        ):
            self.app.start_recording()
        session.assert_not_called()
        self.assertIsNone(self.app.session)
        self.assertIn("保存先に書き込めません", self.app.status_detail.get())
        self.assertTrue(self.app._settings_expanded)

    def test_ffmpeg_installation_blocks_parallel_recording(self):
        self.app.installing_ffmpeg = True
        with patch("gui.archive.SpaceArchiveSession") as session:
            self.app.start_recording()
        session.assert_not_called()

    def test_settings_are_restored_on_next_launch(self):
        self.app.audio_format.set("MP3（互換性重視）")
        self.app.browser.set("Firefox")
        self.app.theme.set("dark")
        self.app.on_theme_changed()
        self.app._save_preferences()
        second_root = tk.Tk()
        second_root.withdraw()
        try:
            second = gui.RecorderApp(second_root)
            self.assertEqual(second.audio_format.get(), "MP3（互換性重視）")
            self.assertEqual(second.browser.get(), "Firefox")
            self.assertEqual(second.output_dir.get(), self.directory.name)
            self.assertEqual(second.url.get(), "")
            self.assertEqual(second.theme.get(), "dark")
            self.assertEqual(second.log.cget("background"), gui.PALETTES["dark"]["card"])
            self.assertEqual(str(second.root.cget("background")), gui.PALETTES["dark"]["page"])
        finally:
            second_root.destroy()


if __name__ == "__main__":
    unittest.main()
