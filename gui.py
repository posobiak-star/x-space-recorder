"""Windows GUI for X Space Recorder."""

from __future__ import annotations

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import archive
import recorder


APP_NAME = "X Space Recorder"
APP_VERSION = "1.2.1"
FIREFOX_DOWNLOAD_URL = "https://www.mozilla.org/firefox/new/"


def default_recordings_dir() -> Path:
    """Prefer the user's HDD when present, otherwise use their Music folder."""
    hdd = Path("D:/")
    if hdd.is_dir():
        return hdd / "X Space Recorder"
    return Path.home() / "Music" / "X Space Recorder"


DEFAULT_RECORDINGS = default_recordings_dir()
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_system_awake(enabled: bool) -> None:
    if os.name != "nt":
        return
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enabled else ES_CONTINUOUS
    ctypes.windll.kernel32.SetThreadExecutionState(flags)
FORMAT_OPTIONS = {
    "Source（推奨・M4A）": (
        "source",
        "Xの元音声を変換せずM4Aで保存。音質劣化なし・最速・容量も小さめです。",
    ),
    "M4A（Sourceと同じ）": (
        "m4a",
        "現在はSourceと同じ処理です。元のAAC音声を変換せずM4Aで保存します。",
    ),
    "MP3（互換性重視）": (
        "mp3",
        "ほとんどの機器や編集ソフトで再生可能。変換に時間がかかり、わずかに音質が落ちます。",
    ),
    "WAV（編集向け・大容量）": (
        "wav",
        "無圧縮で編集しやすい形式。音質は改善せず、ファイル容量が非常に大きくなります。",
    ),
    "Opus（小容量）": (
        "opus",
        "小さい容量で高音質。未対応の再生ソフトや編集ソフトがあります。",
    ),
}
LOGIN_OPTIONS = {
    "認証なし": (
        None,
        "まずはこちらを選択してください。公開SpaceはXへログインせず取得できる場合があります。",
    ),
    "Firefox": (
        "firefox",
        "FirefoxでXにログイン済みの場合、そのCookieをXへの取得リクエストに使用します。パスワード入力は不要です。",
    ),
}


def find_firefox() -> Path | None:
    executable = shutil.which("firefox")
    if executable:
        return Path(executable)

    candidates = []
    for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(Path(root) / "Mozilla Firefox" / "firefox.exe")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def bundled_ytdlp_prefix() -> list[str]:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = bundle_dir / "yt-dlp.exe"
    if bundled.is_file():
        return [str(bundled)]
    return [sys.executable, "-m", "yt_dlp"]


class RecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("760x640")
        self.root.minsize(680, 580)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.session: archive.SpaceArchiveSession | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.close_when_done = False

        self.url = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(DEFAULT_RECORDINGS))
        self.audio_format = tk.StringVar(value=next(iter(FORMAT_OPTIONS)))
        self.format_help = tk.StringVar(value=FORMAT_OPTIONS[self.audio_format.get()][1])
        self.browser = tk.StringVar(value="認証なし")
        self.login_help = tk.StringVar(value=LOGIN_OPTIONS[self.browser.get()][1])
        self.status = tk.StringVar(value="待機中")

        self._build_ui()
        self.root.after(100, self._drain_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, text=APP_NAME, font=("Yu Gothic UI", 19, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 16)
        )

        ttk.Label(outer, text="X SpaceのURL").grid(row=1, column=0, sticky="w")
        url_entry = ttk.Entry(outer, textvariable=self.url, font=("Yu Gothic UI", 11))
        url_entry.grid(row=2, column=0, sticky="ew", pady=(4, 12))
        url_entry.focus_set()

        options = ttk.Frame(outer)
        options.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        options.columnconfigure(0, weight=1, uniform="option")
        options.columnconfigure(1, weight=1, uniform="option")

        format_panel = ttk.LabelFrame(options, text="保存形式", padding=10)
        format_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        format_panel.columnconfigure(0, weight=1)
        format_combo = ttk.Combobox(
            format_panel,
            textvariable=self.audio_format,
            values=tuple(FORMAT_OPTIONS),
            state="readonly",
        )
        format_combo.grid(row=0, column=0, sticky="ew")
        format_combo.bind("<<ComboboxSelected>>", self.on_format_changed)
        ttk.Label(
            format_panel,
            textvariable=self.format_help,
            foreground="#4b5563",
            wraplength=310,
            justify="left",
        ).grid(row=1, column=0, sticky="nw", pady=(8, 0))

        login_panel = ttk.LabelFrame(options, text="Xへのログイン", padding=10)
        login_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        login_panel.columnconfigure(0, weight=1)
        login_combo = ttk.Combobox(
            login_panel,
            textvariable=self.browser,
            values=tuple(LOGIN_OPTIONS),
            state="readonly",
        )
        login_combo.grid(row=0, column=0, sticky="ew")
        login_combo.bind("<<ComboboxSelected>>", self.on_login_changed)
        ttk.Label(
            login_panel,
            textvariable=self.login_help,
            foreground="#4b5563",
            wraplength=310,
            justify="left",
        ).grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.firefox_button = ttk.Button(
            login_panel,
            text="Firefoxをダウンロード",
            command=self.open_firefox_download,
        )
        self.firefox_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._update_firefox_button()

        ttk.Label(outer, text="録音データ保存先").grid(row=4, column=0, sticky="w")
        folder = ttk.Frame(outer)
        folder.grid(row=5, column=0, sticky="ew", pady=(4, 16))
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.output_dir, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(folder, text="変更", command=self.choose_folder).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(folder, text="開く", command=self.open_folder).grid(row=0, column=2, padx=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="録音開始", command=self.start_recording)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="録音停止", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.ffmpeg_button = ttk.Button(actions, text="FFmpegを導入", command=self.install_ffmpeg)
        self.ffmpeg_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.status).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="実行ログ", padding=8)
        log_frame.grid(row=7, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line)
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_format_changed(self, _event: object | None = None) -> None:
        self.format_help.set(FORMAT_OPTIONS[self.audio_format.get()][1])

    def on_login_changed(self, _event: object | None = None) -> None:
        description = LOGIN_OPTIONS[self.browser.get()][1]
        if self.browser.get() != "認証なし":
            description += " 読み取りエラー時はブラウザを完全に終了して再試行してください。"
        self.login_help.set(description)
        self._update_firefox_button()

    def _update_firefox_button(self) -> None:
        if self.browser.get() == "Firefox" and not find_firefox():
            self.firefox_button.grid()
        else:
            self.firefox_button.grid_remove()

    def open_firefox_download(self) -> None:
        if not webbrowser.open(FIREFOX_DOWNLOAD_URL):
            messagebox.showerror(
                APP_NAME,
                f"ブラウザを開けませんでした。次のURLからFirefoxを入手してください。\n\n{FIREFOX_DOWNLOAD_URL}",
            )

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get(), title="録音データ保存先")
        if selected:
            self.output_dir.set(selected)

    def open_folder(self) -> None:
        folder = Path(self.output_dir.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def install_ffmpeg(self) -> None:
        if recorder.find_ffmpeg():
            messagebox.showinfo(APP_NAME, "FFmpegはすでに利用できます。")
            return
        if not shutil.which("winget"):
            messagebox.showerror(
                APP_NAME,
                "wingetが見つかりません。Microsoft Storeの「アプリ インストーラー」を更新するか、FFmpegを手動で導入してください。",
            )
            return
        if not messagebox.askyesno(
            APP_NAME,
            "Windowsのwingetを使ってFFmpegをインストールしますか？\n\n提供元: Gyan.FFmpeg",
        ):
            return
        self.ffmpeg_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.status.set("FFmpeg導入中")
        self.append_log("FFmpegをインストールしています…\n")

        def worker() -> None:
            result = subprocess.run(
                [
                    "winget",
                    "install",
                    "--id",
                    "Gyan.FFmpeg",
                    "--exact",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
            self.events.put(("ffmpeg_done", (result.returncode, result.stderr.strip())))

        threading.Thread(target=worker, daemon=True).start()

    def start_recording(self) -> None:
        if self.session and self.session.is_running:
            return
        try:
            url, space_id = recorder.normalize_space_url(self.url.get())
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        if self.browser.get() == "Firefox" and not find_firefox():
            if messagebox.askyesno(
                APP_NAME,
                "Firefoxが見つかりません。\n\n"
                "ログインが必要なSpaceを取得するには、FirefoxをインストールしてXへログインしてください。\n\n"
                "Firefox公式ダウンロードページを開きますか？",
            ):
                self.open_firefox_download()
            return

        ffmpeg_location = recorder.find_ffmpeg()
        if not ffmpeg_location:
            self.install_ffmpeg()
            return

        output = Path(self.output_dir.get()).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        self.session = archive.SpaceArchiveSession(
            url=url,
            space_id=space_id,
            output_dir=output,
            audio_format=FORMAT_OPTIONS[self.audio_format.get()][0],
            browser=LOGIN_OPTIONS[self.browser.get()][0],
            ytdlp_prefix=bundled_ytdlp_prefix(),
            ffmpeg_location=ffmpeg_location,
            on_log=lambda line: self.events.put(("log", line)),
            on_done=lambda success, path, error: self.events.put(
                ("done", (success, path, error))
            ),
        )
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("履歴＋ライブ取得中")
        keep_system_awake(True)
        self.append_log(
            f"録音開始: {url}\n保存先: {output}\n"
            "録音中はWindowsの自動スリープを抑止します。\n"
        )
        self.session.start()

    def stop_recording(self) -> None:
        session = self.session
        if not session or not session.is_running:
            return
        self.status.set("停止処理中")
        self.stop_button.configure(state="disabled")
        self.append_log("録音を強制停止します。取得済み分の保存へ進みます…\n")
        session.stop()

    def _drain_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "log":
                    self.append_log(str(value))
                elif event == "done":
                    keep_system_awake(False)
                    success, path, error = value
                    self.session = None
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status.set("録音完了" if success else "エラー")
                    if success:
                        self.append_log(f"録音処理が終了しました: {path}\n")
                    elif error:
                        messagebox.showerror(APP_NAME, str(error))
                    if self.close_when_done:
                        self.root.destroy()
                elif event == "error":
                    keep_system_awake(False)
                    self.session = None
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status.set("エラー")
                    messagebox.showerror(APP_NAME, str(value))
                elif event == "ffmpeg_done":
                    returncode, error = value
                    self.ffmpeg_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    if returncode == 0 and recorder.find_ffmpeg():
                        self.status.set("待機中")
                        self.append_log("FFmpegのインストールが完了しました。\n")
                        messagebox.showinfo(APP_NAME, "FFmpegのインストールが完了しました。録音を開始できます。")
                    else:
                        self.status.set("FFmpeg導入エラー")
                        detail = error or f"winget終了コード: {returncode}"
                        self.append_log(f"FFmpegのインストールに失敗しました: {detail}\n")
                        messagebox.showerror(APP_NAME, f"FFmpegをインストールできませんでした。\n{detail}")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def on_close(self) -> None:
        if self.session and self.session.is_running:
            if not messagebox.askyesno(APP_NAME, "録音中です。停止して終了しますか？"):
                return
            self.close_when_done = True
            self.stop_recording()
        else:
            self.root.destroy()


def main() -> None:
    DEFAULT_RECORDINGS.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
