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
