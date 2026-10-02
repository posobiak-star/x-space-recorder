"""Offline preview: python tests/preview_gui.py [idle|live|done] [--compact]."""

import sys
import tempfile
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive
import gui


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "idle"
    with tempfile.TemporaryDirectory() as directory:
        with patch("gui.preferences.preferences_path", return_value=Path(directory) / "settings.json"):
            root = tk.Tk()
            app = gui.RecorderApp(root)
        root.title(f"X Space Recorder UI preview ({phase})")
        if "--dark" in sys.argv:
            app.theme.set("dark")
            app.on_theme_changed()
        if "--compact" in sys.argv:
            root.geometry("760x650")
        if phase != "idle":
            app._toggle_settings(False)
            app._render_progress(archive.ArchiveProgress(phase, "テスト用Space — UIプレビュー", 1200, 1200, 28 * 1024 ** 2, 3600, 1234))
            app.append_log("ライブ開始前まで遡り、現在音声を並行取得します。\n自動補完完了: 3チャンクを回収しました\n")
        if phase == "done":
            app.result_path.set("D:\\X Space Recorder\\テスト用Space.m4a")
            app.result_frame.grid()
        root.mainloop()


if __name__ == "__main__":
    main()
