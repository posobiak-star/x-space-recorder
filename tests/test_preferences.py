import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import preferences


class PreferencesTests(unittest.TestCase):
    def test_settings_survive_restart_without_saving_space_or_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = preferences.Preferences("D:/音声", "mp3", "firefox", "dark")
            preferences.save_preferences(path, expected)
            self.assertEqual(preferences.load_preferences(path, Path("default")), expected)
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"output_dir", "audio_format", "browser", "theme"})

    def test_corrupted_settings_and_invalid_fields_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for body in ('{broken', '[]', '{"output_dir": 42, "audio_format": "unknown", "browser": "chrome"}'):
                path.write_text(body, encoding="utf-8")
                self.assertEqual(preferences.load_preferences(path, Path("default")), preferences.Preferences("default"))

    def test_failed_update_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            old = preferences.Preferences("old")
            preferences.save_preferences(path, old)
            with patch("preferences.Path.replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    preferences.save_preferences(path, preferences.Preferences("new"))
            self.assertEqual(preferences.load_preferences(path, Path("default")), old)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
