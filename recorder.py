#!/usr/bin/env python3
"""Record a live X Space using yt-dlp."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SPACE_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/i/spaces/([A-Za-z0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


def normalize_space_url(value: str) -> tuple[str, str]:
    value = value.strip()
    match = SPACE_URL_RE.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            "X SpaceのURLを指定してください（例: https://x.com/i/spaces/1abcDEF）"
        )
    space_id = match.group(1)
    return f"https://x.com/i/spaces/{space_id}", space_id


def safe_prefix(space_id: str) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{timestamp}_x-space_{space_id}"


def find_ffmpeg() -> str | None:
    """Find FFmpeg on PATH or in WinGet's per-user package directory."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return str(Path(on_path).resolve().parent)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            matches = sorted(
                packages.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"),
                reverse=True,
            )
            if matches:
                return str(matches[0].parent)
    return None


def ytdlp_plugin_args() -> list[str]:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return ["--plugin-dirs", str(bundle_dir / "ytdlp_plugins")]


def yt_dlp_command(
    *,
    url: str,
    space_id: str,
    output_dir: Path,
    audio_format: str,
    browser: str | None,
    cookies_file: Path | None,
    ffmpeg_location: str,
    verbose: bool,
    ytdlp_prefix: list[str] | None = None,
) -> list[str]:
    output_template = str(output_dir / f"{safe_prefix(space_id)}_%(title).100B.%(ext)s")
    command = [
        *(ytdlp_prefix or [sys.executable, "-m", "yt_dlp", *ytdlp_plugin_args()]),
        "--ignore-config",
        "--no-playlist",
        "--newline",
        "--retries",
        "infinite",
        "--fragment-retries",
        "infinite",
        "--retry-sleep",
        "fragment:exp=1:20",
        "--concurrent-fragments",
        "1",
        "--part",
        "--write-info-json",
        "--no-write-playlist-metafiles",
        "--windows-filenames",
        "--ffmpeg-location",
        ffmpeg_location,
        "-f",
        "bestaudio/best",
        "-o",
        output_template,
    ]

    if browser:
        command.extend(["--cookies-from-browser", browser])
    elif cookies_file:
        command.extend(["--cookies", str(cookies_file)])

    if audio_format != "source":
        command.extend(["--extract-audio", "--audio-format", audio_format])
        if audio_format == "mp3":
            command.extend(["--audio-quality", "0"])

    if verbose:
        command.append("--verbose")
    command.extend(["--", url])
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="進行中のX Spaceを音声ファイルとして録音します。",
        epilog="停止するには Ctrl+C を押してください。利用・録音する権限があるスペースで使用してください。",
    )
    parser.add_argument("url", help="https://x.com/i/spaces/... のURL")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path("recordings"), help="保存先（既定: recordings）"
    )
    parser.add_argument(
        "--format",
        choices=("source", "m4a", "mp3", "wav", "opus"),
        default="source",
        help="保存形式。source以外はFFmpegが必要（既定: source）",
    )
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--browser",
        choices=("firefox",),
        help="ログイン済みブラウザのCookieを利用",
    )
    auth.add_argument("--cookies", type=Path, help="Netscape形式のcookies.txtを利用")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログを表示")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        url, space_id = normalize_space_url(args.url)
    except argparse.ArgumentTypeError as exc:
        build_parser().error(str(exc))

    if args.cookies and not args.cookies.is_file():
        build_parser().error(f"Cookieファイルが見つかりません: {args.cookies}")

    try:
        __import__("yt_dlp")
    except ImportError:
        print("yt-dlpが未インストールです。先に setup.ps1 を実行してください。", file=sys.stderr)
        return 2

    ffmpeg_location = find_ffmpeg()
    if ffmpeg_location is None:
        print(
            "X Spaceのライブ音声取得にはFFmpegが必要です。"
            "setup.ps1 -InstallFfmpeg を実行してください。",
            file=sys.stderr,
        )
        return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = yt_dlp_command(
        url=url,
        space_id=space_id,
        output_dir=output_dir,
        audio_format=args.format,
        browser=args.browser,
        cookies_file=args.cookies.resolve() if args.cookies else None,
        ffmpeg_location=ffmpeg_location,
        verbose=args.verbose,
    )

    print(f"録音開始: {url}")
    print(f"保存先: {output_dir}")
    if not args.browser and not args.cookies:
        print("認証エラーの場合は --browser firefox を追加してください。")
    print("停止: Ctrl+C")

    try:
        completed = subprocess.run(command, check=False)
    except KeyboardInterrupt:
        print("\n停止しました。取得済み部分は .part ファイルとして残る場合があります。")
        return 130
    except OSError as exc:
        print(f"起動できませんでした: {exc}", file=sys.stderr)
        return 1

    if completed.returncode:
        print(
            "録音に失敗しました。Spaceが進行中か確認し、認証エラーなら "
            "--browser firefox を付けて再試行してください。",
            file=sys.stderr,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
