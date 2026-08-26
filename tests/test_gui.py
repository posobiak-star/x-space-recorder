import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
