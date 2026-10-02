"""Windows GUI for X Space Recorder."""

from __future__ import annotations

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import archive
import recorder
import preferences


APP_NAME = "X Space Recorder"
APP_VERSION = "1.3.0"
FIREFOX_DOWNLOAD_URL = "https://www.mozilla.org/firefox/new/"
HELP_URL = "https://github.com/posobiak-star/x-space-recorder#readme"
MAX_LOG_LINES = 2000
PALETTES = {
    "light": {
        "page": "#f1f5f9", "card": "#ffffff", "input": "#f8fafc", "text": "#0f172a",
        "muted": "#64748b", "border": "#cbd5e1", "button": "#e2e8f0", "hover": "#cbd5e1",
        "accent": "#2563eb", "accent_hover": "#1d4ed8", "accent_text": "#ffffff",
        "info": "#2563eb", "success": "#15803d", "error": "#b91c1c", "warning": "#a16207",
    },
    "dark": {
        "page": "#0b1220", "card": "#182335", "input": "#111b2b", "text": "#e2e8f0",
        "muted": "#a1b1c7", "border": "#334155", "button": "#26354b", "hover": "#354865",
        "accent": "#60a5fa", "accent_hover": "#93c5fd", "accent_text": "#0b1220",
        "info": "#93c5fd", "success": "#86efac", "error": "#fca5a5", "warning": "#fcd34d",
    },
}


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


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def format_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 ** 2:.1f} MB"


PHASES = {
    "preparing": ("Spaceを確認中", "公開状態と音声データを確認しています。"),
    "history": ("過去の音声を取得中", "録音開始前の音声を取り込んでいます。停止すると取得済み分を保存します。"),
    "live": ("ライブ録音中", "新しい音声を追加しています。欠けた部分は配信中に自動補完します。"),
    "stopping": ("停止処理中", "新しい取得を止めています。通信中の処理が終わるまで少しお待ちください。"),
    "final_repair": ("最後の欠損を補完中", "Spaceの終了を確認しました。取得できる音声をもう一度確認します。"),
    "saving": ("音声ファイルを保存中", "取得済みの音声を結合しています。保存完了までお待ちください。"),
    "done": ("保存完了", "下のボタンから音声を再生したり、保存場所を開いたりできます。"),
    "cancelled": ("録音を中止しました", "音声を取得する前に停止したため、ファイルは作成していません。"),
    "error": ("取得・保存エラー", "詳細は実行ログで確認できます。"),
}
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
        self.root.geometry(f"900x{min(820, max(650, root.winfo_screenheight() - 100))}")
        self.root.minsize(760, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.configure(background="#f1f5f9")

        self.session: archive.SpaceArchiveSession | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.close_when_done = False
        self.installing_ffmpeg = False
        self.last_output: Path | None = None
        self.recovery_folder: Path | None = None
        self._closed = False
        self._last_phase: str | None = None
        self._bar_mode: str | None = None
        self._settings_warning_shown = False
        self._settings_expanded = True
        self._status_tone = "info"
        self.settings_path = preferences.preferences_path()
        saved = preferences.load_preferences(self.settings_path, DEFAULT_RECORDINGS)
        self.theme = tk.StringVar(value=saved.theme)

        self.url = tk.StringVar()
        self.output_dir = tk.StringVar(value=saved.output_dir)
        self.audio_format = tk.StringVar(value=next(label for label, (value, _help) in FORMAT_OPTIONS.items() if value == saved.audio_format))
        self.format_help = tk.StringVar(value=FORMAT_OPTIONS[self.audio_format.get()][1])
        self.browser = tk.StringVar(value="Firefox" if saved.browser else "認証なし")
        self.login_help = tk.StringVar(value=LOGIN_OPTIONS[self.browser.get()][1])
        self.status = tk.StringVar(value="録音の準備ができています")
        self.status_detail = tk.StringVar(value="SpaceのURLを貼り付けて、録音開始を押してください。")
        self.space_title = tk.StringVar(value="ライブと公開リプレイの音声を保存")
        self.audio_time = tk.StringVar(value="00:00")
        self.data_size = tk.StringVar(value="0.0 MB")
        self.elapsed = tk.StringVar(value="00:00")
        self.result_path = tk.StringVar()
        self.environment = tk.StringVar()
        self.settings_summary = tk.StringVar()

        self._configure_styles()
        self._build_ui()
        self.on_login_changed()
        self._update_environment()
        self.root.bind("<Map>", lambda event: self._update_titlebar() if event.widget is self.root else None, add="+")
        self.root.bind("<F1>", lambda _event: webbrowser.open(HELP_URL))
        self.root.after(100, self._drain_events)

    def _configure_styles(self) -> None:
        self.colors = PALETTES[self.theme.get()]
        colors = self.colors
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(background=colors["page"])
        style.configure("TFrame", background=colors["page"])
        style.configure("Card.TFrame", background=colors["card"])
        style.configure("TLabel", background=colors["page"], foreground=colors["text"], font=("Yu Gothic UI", 10))
        style.configure("Card.TLabel", background=colors["card"])
        style.configure("Muted.TLabel", background=colors["card"], foreground=colors["muted"], font=("Yu Gothic UI", 9))
        style.configure("PageMuted.TLabel", background=colors["page"], foreground=colors["muted"], font=("Yu Gothic UI", 9))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 22, "bold"))
        style.configure("Status.TLabel", background=colors["card"], foreground=colors[self._status_tone], font=("Yu Gothic UI", 12, "bold"))
        style.configure("Metric.TLabel", background=colors["card"], font=("Yu Gothic UI", 18, "bold"))
        style.configure("TButton", font=("Yu Gothic UI", 10), padding=(12, 6), background=colors["button"], foreground=colors["text"], bordercolor=colors["border"], lightcolor=colors["button"], darkcolor=colors["button"])
        style.map("TButton", background=[("active", colors["hover"])], foreground=[("disabled", colors["muted"])])
        style.configure("Accent.TButton", background=colors["accent"], foreground=colors["accent_text"], font=("Yu Gothic UI", 11, "bold"), padding=(24, 9))
        style.map("Accent.TButton", background=[("disabled", colors["button"]), ("active", colors["accent_hover"])], foreground=[("disabled", colors["muted"]), ("!disabled", colors["accent_text"])])
        for name in ("TEntry", "TCombobox"):
            style.configure(name, padding=6 if name == "TEntry" else 4, fieldbackground=colors["input"], foreground=colors["text"], insertcolor=colors["text"], bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"], background=colors["button"], arrowcolor=colors["text"])
            style.map(name, fieldbackground=[("disabled", colors["card"]), ("readonly", colors["input"])], foreground=[("disabled", colors["muted"]), ("readonly", colors["text"])], background=[("disabled", colors["card"]), ("active", colors["hover"]), ("readonly", colors["button"])], bordercolor=[("focus", colors["accent"])])
        style.configure("TCheckbutton", background=colors["page"], foreground=colors["text"], font=("Yu Gothic UI", 10), indicatorbackground=colors["input"], indicatorforeground=colors["accent"])
        style.map("TCheckbutton", background=[("active", colors["page"])], foreground=[("active", colors["text"])])
        style.configure("Vertical.TScrollbar", background=colors["button"], troughcolor=colors["page"], arrowcolor=colors["muted"], bordercolor=colors["page"], lightcolor=colors["button"], darkcolor=colors["button"])
        style.map("Vertical.TScrollbar", background=[("active", colors["hover"])])
        style.configure("Horizontal.TProgressbar", background=colors["accent"], troughcolor=colors["input"], bordercolor=colors["border"], lightcolor=colors["accent"], darkcolor=colors["accent"], borderwidth=0, thickness=5)
        self.root.option_add("*TCombobox*Listbox.background", colors["input"])
        self.root.option_add("*TCombobox*Listbox.foreground", colors["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", colors["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", colors["accent_text"])
        self.root.after_idle(self._update_titlebar)

    def _update_titlebar(self) -> None:
        if os.name != "nt" or self._closed:
            return
        try:
            get_parent = ctypes.windll.user32.GetParent
            get_parent.argtypes = [ctypes.c_void_p]
            get_parent.restype = ctypes.c_void_p
            hwnd = get_parent(self.root.winfo_id())
            attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            dark = ctypes.c_int(self.theme.get() == "dark")
            attribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        except (AttributeError, OSError, tk.TclError):
            pass

    def on_theme_changed(self) -> None:
        self._configure_styles()
        self.canvas.configure(background=self.colors["page"])
        self.log.configure(background=self.colors["card"], foreground=self.colors["text"], insertbackground=self.colors["text"], selectbackground=self.colors["accent"], selectforeground=self.colors["accent_text"])
        self.status_label.configure(foreground=self.colors[self._status_tone])
        self._configure_log_tags()
        self._save_preferences()

    def _configure_log_tags(self) -> None:
        for tag in ("error", "warning", "success"):
            self.log.tag_configure(tag, foreground=self.colors[tag])

    def _build_ui(self) -> None:
        viewport = ttk.Frame(self.root)
        viewport.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(viewport, background=self.colors["page"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        page_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        page_scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=page_scroll.set)
        outer = ttk.Frame(self.canvas, padding=(22, 16))
        self._page_window = self.canvas.create_window(0, 0, anchor="nw", window=outer)
        self.canvas.bind("<Configure>", lambda _event: self._resize_page(outer))
        outer.bind("<Configure>", lambda _event: self._resize_page(outer))
        self.root.bind("<MouseWheel>", self._scroll_page)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", style="PageMuted.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(header, text="ダークモード", variable=self.theme, onvalue="dark", offvalue="light", command=self.on_theme_changed).grid(row=0, column=1, padx=(0, 12))
        ttk.Button(header, text="使い方", command=lambda: webbrowser.open(HELP_URL)).grid(row=0, column=2)

        config = ttk.Frame(outer, style="Card.TFrame", padding=14)
        config.grid(row=1, column=0, sticky="ew")
        config.columnconfigure(0, weight=1)
        config_header = ttk.Frame(config, style="Card.TFrame")
        config_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(config_header, text="X SpaceのURL", style="Card.TLabel").pack(side="left")
        self.settings_toggle = ttk.Button(config_header, text="録音設定を閉じる", padding=(6, 1), command=lambda: self._toggle_settings(not self._settings_expanded))
        self.settings_toggle.pack(side="right")
        input_row = ttk.Frame(config, style="Card.TFrame")
        input_row.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        input_row.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(input_row, textvariable=self.url, font=("Yu Gothic UI", 11))
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _event: self.start_recording())
        self.url_entry.focus_set()
        self.paste_button = ttk.Button(input_row, text="貼り付け", command=self.paste_url)
        self.paste_button.grid(row=0, column=1, padx=(8, 0))

        options = ttk.Frame(config, style="Card.TFrame")
        self.options_panel = options
        options.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        options.columnconfigure(0, weight=1, uniform="option")
        options.columnconfigure(1, weight=1, uniform="option")

        format_panel = ttk.Frame(options, style="Card.TFrame")
        format_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        format_panel.columnconfigure(0, weight=1)
        ttk.Label(format_panel, text="保存形式", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.format_combo = ttk.Combobox(
            format_panel,
            textvariable=self.audio_format,
            values=tuple(FORMAT_OPTIONS),
            state="readonly",
        )
        self.format_combo.grid(row=1, column=0, sticky="ew")
        self.format_combo.bind("<<ComboboxSelected>>", self.on_format_changed)
        self.format_description = ttk.Label(
            format_panel,
            textvariable=self.format_help,
            style="Muted.TLabel",
            wraplength=330,
            justify="left",
        )
        self.format_description.grid(row=2, column=0, sticky="nw", pady=(6, 0))

        login_panel = ttk.Frame(options, style="Card.TFrame")
        login_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        login_panel.columnconfigure(0, weight=1)
        ttk.Label(login_panel, text="Xへのログイン", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.login_combo = ttk.Combobox(
            login_panel,
            textvariable=self.browser,
            values=tuple(LOGIN_OPTIONS),
            state="readonly",
        )
        self.login_combo.grid(row=1, column=0, sticky="ew")
        self.login_combo.bind("<<ComboboxSelected>>", self.on_login_changed)
        self.login_description = ttk.Label(
            login_panel,
            textvariable=self.login_help,
            style="Muted.TLabel",
            wraplength=330,
            justify="left",
        )
        self.login_description.grid(row=2, column=0, sticky="nw", pady=(6, 0))
        self.firefox_button = ttk.Button(
            login_panel,
            text="Firefoxをダウンロード",
            command=self.open_firefox_download,
        )
        self.firefox_button.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._update_firefox_button()
        options.bind("<Configure>", lambda event: self._resize_descriptions(event.width))

        self.folder_label = ttk.Label(config, text="保存先", style="Card.TLabel")
        self.folder_label.grid(row=3, column=0, sticky="w")
        folder = ttk.Frame(config, style="Card.TFrame")
        self.folder_panel = folder
        folder.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.output_dir, state="readonly").grid(
            row=0, column=0, sticky="ew"
        )
        self.folder_button = ttk.Button(folder, text="変更", command=self.choose_folder)
        self.folder_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(folder, text="開く", command=self.open_folder).grid(row=0, column=2, padx=(8, 0))
        self.summary_label = ttk.Label(config, textvariable=self.settings_summary, style="Muted.TLabel", wraplength=650)
        self.summary_label.grid(row=5, column=0, sticky="ew")
        self.summary_label.grid_remove()

        status_panel = ttk.Frame(outer, style="Card.TFrame", padding=14)
        status_panel.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        status_panel.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(status_panel, textvariable=self.status, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.title_label = ttk.Label(status_panel, textvariable=self.space_title, style="Card.TLabel", wraplength=780)
        self.title_label.grid(row=1, column=0, sticky="ew", pady=(3, 5))
        self.progress_bar = ttk.Progressbar(status_panel, mode="determinate", maximum=100)
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        metrics = ttk.Frame(status_panel, style="Card.TFrame")
        metrics.grid(row=3, column=0, sticky="ew")
        for column, (label, value) in enumerate((("保存済み音声", self.audio_time), ("取得データ", self.data_size), ("処理時間", self.elapsed))):
            metrics.columnconfigure(column, weight=1, uniform="metric")
            cell = ttk.Frame(metrics, style="Card.TFrame")
            cell.grid(row=0, column=column, sticky="ew")
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=value, style="Metric.TLabel").pack(anchor="w")
        self.detail_label = ttk.Label(status_panel, textvariable=self.status_detail, style="Muted.TLabel", wraplength=780)
        self.detail_label.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        status_panel.bind("<Configure>", lambda event: self._resize_status(event.width))

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=12)
        self.start_button = ttk.Button(actions, text="録音開始", style="Accent.TButton", command=self.start_recording)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止して保存", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.ffmpeg_button = ttk.Button(actions, text="FFmpegを導入", command=self.install_ffmpeg)
        self.ffmpeg_button.pack(side="right")
        ttk.Label(actions, textvariable=self.environment, style="PageMuted.TLabel").pack(side="right", padx=(0, 10))

        self.result_frame = ttk.Frame(outer, style="Card.TFrame", padding=10)
        self.result_frame.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        self.result_frame.columnconfigure(0, weight=1)
        ttk.Entry(self.result_frame, textvariable=self.result_path, state="readonly").grid(row=0, column=0, sticky="ew")
        self.play_button = ttk.Button(self.result_frame, text="再生", command=self.play_output)
        self.play_button.grid(row=0, column=1, padx=(8, 0))
        self.reveal_button = ttk.Button(self.result_frame, text="ファイルを表示", command=self.reveal_output)
        self.reveal_button.grid(row=0, column=2, padx=(6, 0))
        self.result_frame.grid_remove()

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_header = ttk.Frame(log_frame)
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(log_header, text="実行ログ", style="PageMuted.TLabel").pack(side="left")
        ttk.Button(log_header, text="ログをコピー", command=self.copy_log, padding=(8, 2)).pack(side="right")
        self.log = tk.Text(log_frame, height=5, wrap="word", state="disabled", font=("Yu Gothic UI", 9), background=self.colors["card"], foreground=self.colors["text"], insertbackground=self.colors["text"], selectbackground=self.colors["accent"], selectforeground=self.colors["accent_text"], relief="flat", padx=10, pady=8)
        self.log.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)
        self._configure_log_tags()

    def _resize_descriptions(self, width: int) -> None:
        for label in (self.format_description, self.login_description):
            label.configure(wraplength=max(180, width // 2 - 12))

    def _resize_page(self, outer: ttk.Frame) -> None:
        self.canvas.itemconfigure(self._page_window, width=self.canvas.winfo_width(), height=max(self.canvas.winfo_height(), outer.winfo_reqheight()))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _scroll_page(self, event: tk.Event) -> None:
        if event.widget is not self.log and event.widget not in (self.format_combo, self.login_combo):
            region = self.canvas.bbox("all")
            if region and region[3] > self.canvas.winfo_height():
                self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _toggle_settings(self, expanded: bool) -> None:
        self._settings_expanded = expanded
        for widget in (self.options_panel, self.folder_label, self.folder_panel):
            widget.grid() if expanded else widget.grid_remove()
        self.settings_toggle.configure(text="録音設定を閉じる" if expanded else "録音設定を表示")
        self.settings_summary.set(f"{self.audio_format.get()}  /  {self.browser.get()}\n保存先: {self.output_dir.get()}")
        self.summary_label.grid_remove() if expanded else self.summary_label.grid()
        self.canvas.yview_moveto(0)

    def _resize_status(self, width: int) -> None:
        self.title_label.configure(wraplength=max(200, width - 28))
        self.detail_label.configure(wraplength=max(200, width - 28))

    def append_log(self, line: str) -> None:
        follow = self.log.yview()[1] >= 0.98
        tag = "error" if "エラー:" in line else "warning" if any(word in line for word in ("警告:", "注意:", "待ち:")) else "success" if "保存完了:" in line else ""
        self.log.configure(state="normal")
        self.log.insert("end", line, tag)
        extra = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log.delete("1.0", f"{extra + 1}.0")
        if follow:
            self.log.see("end")
        self.log.configure(state="disabled")

    def copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))

    def paste_url(self) -> None:
        if self.session or self.installing_ffmpeg:
            return
        try:
            self.url.set(self.root.clipboard_get().strip())
            self.url_entry.focus_set()
        except tk.TclError:
            self._show_error("クリップボードにテキストがありません。")

    def _save_preferences(self) -> None:
        try:
            preferences.save_preferences(self.settings_path, preferences.Preferences(
                self.output_dir.get(), FORMAT_OPTIONS[self.audio_format.get()][0], LOGIN_OPTIONS[self.browser.get()][0], self.theme.get(),
            ))
        except OSError:
            if not self._settings_warning_shown:
                self.append_log("注意: 設定を保存できませんでした。録音処理はそのまま利用できます。\n")
                self._settings_warning_shown = True

    def _show_error(self, message: str) -> None:
        if not self.session:
            self.status.set("操作を確認してください")
            self._status_tone = "error"
            self.status_label.configure(foreground=self.colors["error"])
        self.status_detail.set(message)
        self.append_log(f"エラー: {message}\n")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.url_entry.configure(state=state)
        self.paste_button.configure(state=state)
        self.folder_button.configure(state=state)
        self.format_combo.configure(state="disabled" if busy else "readonly")
        self.login_combo.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if self.session else "disabled")
        self._update_environment()

    def _update_environment(self) -> None:
        available = bool(recorder.find_ffmpeg())
        self.environment.set("音声保存の準備OK" if available else "初回はFFmpegを導入")
        self.ffmpeg_button.configure(
            text="FFmpeg導入済み" if available else "FFmpegを導入",
            state="disabled" if available or self.session or self.installing_ffmpeg else "normal",
        )

    def _render_progress(self, progress: archive.ArchiveProgress) -> None:
        self.space_title.set(progress.title)
        self.audio_time.set(format_duration(progress.audio_seconds))
        self.data_size.set(format_size(progress.downloaded_bytes))
        self.elapsed.set(format_duration(progress.elapsed_seconds))
        if progress.phase != self._last_phase:
            self._last_phase = progress.phase
            status, detail = PHASES.get(progress.phase, PHASES["preparing"])
            self.status.set(status)
            self.status_detail.set(detail)
            self._status_tone = "success" if progress.phase == "done" else "error" if progress.phase == "error" else "info"
            self.status_label.configure(foreground=self.colors[self._status_tone])
        if progress.phase == "history" and progress.known_chunks:
            mode = "determinate"
            maximum = progress.known_chunks
            value = progress.downloaded_chunks
        elif progress.phase in ("done", "error", "cancelled"):
            mode, maximum, value = "determinate", 100, 100 if progress.phase == "done" else 0
        else:
            mode, maximum, value = "indeterminate", 100, 0
        if mode != self._bar_mode:
            self.progress_bar.stop()
            self.progress_bar.configure(mode=mode)
            self._bar_mode = mode
            if mode == "indeterminate":
                self.progress_bar.start(20)
        if mode == "determinate":
            self.progress_bar.configure(maximum=maximum, value=min(value, maximum))
        if self.session:
            self.stop_button.configure(state="disabled" if progress.phase in ("stopping", "saving", "done", "error", "cancelled") else "normal")

    def on_format_changed(self, _event: object | None = None) -> None:
        self.format_help.set(FORMAT_OPTIONS[self.audio_format.get()][1])
        if _event is not None:
            self._save_preferences()

    def on_login_changed(self, _event: object | None = None) -> None:
        description = LOGIN_OPTIONS[self.browser.get()][1]
        if self.browser.get() != "認証なし":
            description += " 読み取りエラー時はブラウザを完全に終了して再試行してください。"
        self.login_help.set(description)
        self._update_firefox_button()
        if _event is not None:
            self._save_preferences()

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
        if self.session or self.installing_ffmpeg:
            return
        selected = filedialog.askdirectory(initialdir=self.output_dir.get(), title="録音データ保存先")
        if selected:
            self.output_dir.set(selected)
            self._save_preferences()

    def open_folder(self) -> None:
        try:
            folder = Path(self.output_dir.get()).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError as exc:
            self._show_error(f"保存先を開けませんでした: {exc}")

    def play_output(self) -> None:
        try:
            if not self.last_output or not self.last_output.is_file():
                raise OSError("音声ファイルが見つかりません。保存先を確認してください。")
            os.startfile(self.last_output)
        except OSError as exc:
            self._show_error(str(exc))

    def reveal_output(self) -> None:
        try:
            if self.recovery_folder:
                os.startfile(self.recovery_folder)
            elif self.last_output and self.last_output.is_file():
                subprocess.Popen(["explorer.exe", f"/select,{self.last_output}"])
            else:
                self.open_folder()
        except OSError as exc:
            self._show_error(f"ファイルの保存場所を開けませんでした: {exc}")

    def install_ffmpeg(self) -> None:
        if self.session or self.installing_ffmpeg:
            return
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
        self.installing_ffmpeg = True
        self._set_busy(True)
        self.status.set("FFmpeg導入中")
        self.status_detail.set("インストールが完了するまでお待ちください。")
        self.append_log("FFmpegをインストールしています…\n")

        def worker() -> None:
            try:
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
            except OSError as exc:
                self.events.put(("ffmpeg_done", (1, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def start_recording(self) -> None:
        if self.session or self.installing_ffmpeg:
            return
        try:
            url, space_id = recorder.normalize_space_url(self.url.get())
        except Exception as exc:
            self._show_error(str(exc))
            self.url_entry.focus_set()
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

        try:
            output = Path(self.output_dir.get()).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=output):
                pass
        except (OSError, ValueError) as exc:
            self._show_error(f"保存先に書き込めません。別のフォルダを選択してください: {exc}")
            return
        self._save_preferences()
        self.url.set(url)
        self.last_output = None
        self.recovery_folder = None
        self.result_frame.grid_remove()
        self.close_when_done = False
        self._last_phase = None
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
        self._set_busy(True)
        self._toggle_settings(False)
        self._render_progress(self.session.progress)
        keep_system_awake(True)
        self.append_log(
            f"録音開始: {url}\n保存先: {output}\n"
            "録音中はWindowsの自動スリープを抑止します。\n"
        )
        try:
            self.session.start()
        except Exception as exc:
            keep_system_awake(False)
            self.session = None
            self._set_busy(False)
            self._show_error(f"録音処理を開始できませんでした: {exc}")

    def stop_recording(self) -> None:
        session = self.session
        if not session or session.progress.phase == "saving":
            return
        self.status.set("停止処理中")
        self.stop_button.configure(state="disabled")
        self.append_log("録音を強制停止します。取得済み分の保存へ進みます…\n")
        session.stop()
        self._render_progress(session.progress)

    def _drain_events(self) -> None:
        if self._closed:
            return
        try:
            for _ in range(100):
                event, value = self.events.get_nowait()
                if event == "log":
                    self.append_log(str(value))
                elif event == "done":
                    keep_system_awake(False)
                    success, path, error = value
                    if self.session:
                        self._render_progress(self.session.progress)
                        self.recovery_folder = self.session.recovery_dir if not success else None
                    self.session = None
                    self._set_busy(False)
                    if success and path:
                        self.last_output = Path(path)
                        self.result_path.set(str(path))
                        self.play_button.configure(state="normal")
                        self.reveal_button.configure(text="ファイルを表示")
                        self.result_frame.grid()
                    elif error:
                        self.status_detail.set(str(error))
                        if self.recovery_folder:
                            self.result_path.set(str(self.recovery_folder))
                            self.play_button.configure(state="disabled")
                            self.reveal_button.configure(text="取得済みデータを表示")
                            self.result_frame.grid()
                    if self.close_when_done:
                        self._shutdown()
                        return
                elif event == "ffmpeg_done":
                    returncode, error = value
                    self.installing_ffmpeg = False
                    self._set_busy(False)
                    if returncode == 0 and recorder.find_ffmpeg():
                        self.status.set("録音の準備ができています")
                        self.status_detail.set("FFmpegを導入しました。URLを貼り付けて録音を開始できます。")
                        self.append_log("FFmpegのインストールが完了しました。\n")
                    else:
                        detail = error or f"winget終了コード: {returncode}"
                        self._show_error(f"FFmpegをインストールできませんでした: {detail}")
        except queue.Empty:
            pass
        if self.session:
            self._render_progress(self.session.progress)
        self.root.after(200, self._drain_events)

    def on_close(self) -> None:
        if self.session:
            if self.close_when_done:
                return
            prompt = "音声を保存中です。保存完了後にアプリを閉じますか？" if self.session.progress.phase == "saving" else "録音を停止し、取得済み分を保存してから終了しますか？"
            if not messagebox.askyesno(APP_NAME, prompt):
                return
            self.close_when_done = True
            self.stop_recording()
        elif self.installing_ffmpeg:
            if messagebox.askyesno(APP_NAME, "FFmpegの導入はバックグラウンドで続きます。アプリを閉じますか？"):
                self._shutdown()
        else:
            self._shutdown()

    def _shutdown(self) -> None:
        self._save_preferences()
        keep_system_awake(False)
        self._closed = True
        self.progress_bar.stop()
        self.root.destroy()


def main() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
