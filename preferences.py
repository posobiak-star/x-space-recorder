"""Local UI preferences. Space URLs, audio and cookies are never stored here."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Preferences:
    output_dir: str
    audio_format: str = "source"
    browser: str | None = None
    theme: str = "light"


def preferences_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "X Space Recorder" / "settings.json"


def load_preferences(path: Path, default_output_dir: Path) -> Preferences:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    output = data.get("output_dir")
    if not isinstance(output, str) or not output.strip() or "\x00" in output:
        output = str(default_output_dir)
    audio_format = data.get("audio_format")
    if audio_format not in ("source", "m4a", "mp3", "wav", "opus"):
        audio_format = "source"
    browser = "firefox" if data.get("browser") == "firefox" else None
    theme = "dark" if data.get("theme") == "dark" else "light"
    return Preferences(output, audio_format, browser, theme)


def save_preferences(path: Path, preferences: Preferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix="settings-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(preferences), stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
