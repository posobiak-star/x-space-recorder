import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recorder


class RecorderTests(unittest.TestCase):
    def test_normalize_x_url(self):
        self.assertEqual(
            recorder.normalize_space_url("https://x.com/i/spaces/1AbC23/peek?x=1"),
            ("https://x.com/i/spaces/1AbC23", "1AbC23"),
        )

    def test_normalize_twitter_url(self):
        self.assertEqual(
            recorder.normalize_space_url("https://twitter.com/i/spaces/xyz"),
            ("https://x.com/i/spaces/xyz", "xyz"),
        )

    def test_rejects_non_space_url(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            recorder.normalize_space_url("https://x.com/user/status/123")

    @patch("recorder.find_ffmpeg", return_value=None)
    def test_main_requires_ffmpeg_for_source_too(self, _find):
        with patch.dict(sys.modules, {"yt_dlp": object()}):
            self.assertEqual(
                recorder.main(["https://x.com/i/spaces/abc", "--format", "source"]),
                2,
            )

    @patch("recorder.shutil.which", return_value=None)
    def test_find_ffmpeg_in_winget_packages(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory) / "Microsoft/WinGet/Packages/Gyan.FFmpeg_test/ffmpeg/bin/ffmpeg.exe"
            ffmpeg.parent.mkdir(parents=True)
            ffmpeg.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}), patch("recorder.os.name", "nt"):
                self.assertEqual(recorder.find_ffmpeg(), str(ffmpeg.parent))

    @patch("recorder.safe_prefix", return_value="prefix")
    def test_command_uses_browser_and_source_format(self, _):
        command = recorder.yt_dlp_command(
            url="https://x.com/i/spaces/abc",
            space_id="abc",
            output_dir=Path("out"),
            audio_format="source",
            browser="firefox",
            cookies_file=None,
            ffmpeg_location="C:/ffmpeg/bin",
            verbose=False,
        )
        self.assertIn("--cookies-from-browser", command)
        self.assertNotIn("--extract-audio", command)
        self.assertEqual(command[-1], "https://x.com/i/spaces/abc")

    @patch("recorder.safe_prefix", return_value="prefix")
    def test_command_enables_mp3_conversion(self, _):
        with tempfile.TemporaryDirectory() as directory:
            command = recorder.yt_dlp_command(
                url="https://x.com/i/spaces/abc",
                space_id="abc",
                output_dir=Path(directory),
                audio_format="mp3",
                browser=None,
                cookies_file=Path("cookies.txt"),
                ffmpeg_location="C:/ffmpeg/bin",
                verbose=True,
            )
        self.assertIn("--extract-audio", command)
        self.assertIn("mp3", command)
        self.assertIn("--cookies", command)
        self.assertIn("--verbose", command)


if __name__ == "__main__":
    unittest.main()
