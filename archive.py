"""Backfill and continuously archive X Space HLS audio segments."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable


SEGMENT_SEQUENCE_RE = re.compile(r"_(\d+)_a\.aac(?:\?|$)")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
HISTORY_REPAIR_INTERVAL_SECONDS = 5.0
LIVE_POLL_INTERVAL_SECONDS = 1.5
SEGMENT_REQUEST_TIMEOUT_SECONDS = 10.0
PLAYLIST_REQUEST_TIMEOUT_SECONDS = 10.0
LIVE_STATUS_CHECK_AFTER_SECONDS = 30.0
SPACE_INFO_TIMEOUT_SECONDS = 30.0


class SpaceArchiveError(RuntimeError):
    pass


class SpaceArchiveSession:
    def __init__(
        self,
        *,
        url: str,
        space_id: str,
        output_dir: Path,
        audio_format: str,
        browser: str | None,
        ytdlp_prefix: list[str],
        ffmpeg_location: str,
        on_log: Callable[[str], None],
        on_done: Callable[[bool, Path | None, str | None], None],
    ) -> None:
        self.url = url
        self.space_id = space_id
        self.output_dir = output_dir
        self.audio_format = audio_format
        self.browser = browser
        self.ytdlp_prefix = ytdlp_prefix
        self.ffmpeg_location = Path(ffmpeg_location)
        self.on_log = on_log
        self.on_done = on_done
        self.stop_event = threading.Event()
        self.manual_stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._claim_lock = threading.Lock()
        self._claimed: set[int] = set()
        self._known_segments: dict[int, str] = {}
        self._downloaded = 0
        self._temp_dir: Path | None = None
        self._status_replay_url: str | None = None
        self._status_live_url: str | None = None
        self._ended_without_replay = False
        self._last_status_result: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.manual_stop_event.set()
        self.stop_event.set()

    @property
    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _log(self, message: str) -> None:
        self.on_log(message.rstrip() + "\n")

    def _extract_info(self, *, allow_no_formats: bool = False) -> dict:
        command = [
            *self.ytdlp_prefix,
            "--ignore-config",
            "--no-download",
            "--dump-single-json",
        ]
        if allow_no_formats:
            command.append("--ignore-no-formats-error")
        if self.browser:
            command.extend(["--cookies-from-browser", self.browser])
        command.extend(["--", self.url])
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + SPACE_INFO_TIMEOUT_SECONDS
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if self.manual_stop_event.is_set():
                    process.kill()
                    process.communicate()
                    raise SpaceArchiveError("録音停止によりSpace情報の確認を中断しました")
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    raise SpaceArchiveError("Space情報の確認が30秒以内に完了しませんでした")
        if process.returncode:
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else "取得情報なし"
            raise SpaceArchiveError(f"Space情報を取得できませんでした: {detail}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SpaceArchiveError("Space情報の応答を解析できませんでした") from exc

    def _check_space_status(self) -> str:
        """Refresh Space metadata without assuming that a stalled playlist means ended."""
        info = self._extract_info(allow_no_formats=True)
        live_status = str(info.get("live_status") or "")
        replay_url = str(info.get("url") or "")
        if live_status == "is_live":
            self._status_live_url = replay_url or None
            if self._last_status_result != "live":
                self._log("状態確認: Spaceは進行中です。ライブ取得を再試行します。")
            self._last_status_result = "live"
            return "live"
        if live_status in ("was_live", "post_live"):
            self._status_replay_url = replay_url or None
            self._ended_without_replay = not bool(replay_url)
            if replay_url:
                self._log("状態確認: Space終了・公開リプレイあり。最終補完を開始します。")
                result = "ended_with_replay"
            else:
                self._log("状態確認: Space終了・公開リプレイなし。取得済み分を保存します。")
                result = "ended_without_replay"
            self._last_status_result = result
            self.stop_event.set()
            return result
        result = f"other:{live_status or 'unknown'}"
        if self._last_status_result != result:
            self._log(f"状態確認: Spaceは終了していません（{live_status or '状態不明'}）。")
        self._last_status_result = result
        return "other"

    @staticmethod
    def _read_url(url: str, *, binary: bool = False, timeout: float = 30) -> str | bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://x.com/",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
        return data if binary else data.decode("utf-8", errors="replace")

    @staticmethod
    def parse_playlist(playlist_url: str, body: str) -> list[tuple[int, str]]:
        media_sequence = 0
        entries: list[tuple[int, str]] = []
        fallback_index = 0
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(line.partition(":")[2])
                except ValueError:
                    media_sequence = 0
            elif line and not line.startswith("#") and ".m3u8" not in line:
                match = SEGMENT_SEQUENCE_RE.search(line)
                sequence = int(match.group(1)) if match else media_sequence + fallback_index
                entries.append((sequence, urllib.parse.urljoin(playlist_url, line)))
                fallback_index += 1
        return entries

    def _derive_live_replay_url(self, dynamic_url: str) -> str:
        if "dynamic_playlist.m3u8" not in dynamic_url:
            raise SpaceArchiveError("ライブ履歴プレイリストを特定できませんでした")
        master_url = re.sub(r"dynamic_playlist\.m3u8(?:\?type=live)?$", "master_playlist.m3u8", dynamic_url)
        master = str(self._read_url(master_url))
        playlist_paths = [
            line.strip()
            for line in master.splitlines()
            if line.strip() and not line.startswith("#") and ".m3u8" in line
        ]
        if not playlist_paths:
            raise SpaceArchiveError("ライブ履歴のマスタープレイリストが空です")
        playlist_name = Path(urllib.parse.urlparse(playlist_paths[0]).path).name
        return re.sub(
            r"dynamic_playlist\.m3u8(?:\?type=live)?$",
            f"{playlist_name}?type=replay",
            dynamic_url,
        )

    def _segment_path(self, sequence: int) -> Path:
        assert self._temp_dir is not None
        return self._temp_dir / f"{sequence:09d}.aac"

    def _remember_segments(self, segments: list[tuple[int, str]]) -> None:
        with self._claim_lock:
            for sequence, url in segments:
                self._known_segments[sequence] = url

    def _missing_known_segments(self) -> list[tuple[int, str]]:
        with self._claim_lock:
            known = sorted(self._known_segments.items())
            claimed = self._claimed.copy()
        return [
            (sequence, url)
            for sequence, url in known
            if sequence not in claimed and not self._segment_path(sequence).is_file()
        ]

    def _download_batch(self, segments: list[tuple[int, str]], *, workers: int = 8) -> int:
        failures = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="space-audio") as executor:
            futures = [executor.submit(self._download_segment, sequence, url) for sequence, url in segments]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    failures += 1
        return failures

    def _download_segment(self, sequence: int, url: str) -> bool:
        if self.manual_stop_event.is_set():
            return False
        target = self._segment_path(sequence)
        with self._claim_lock:
            if target.is_file() or sequence in self._claimed:
                return False
            self._claimed.add(sequence)
        try:
            for attempt in range(5):
                if self.manual_stop_event.is_set():
                    return False
                try:
                    data = self._read_url(
                        url,
                        binary=True,
                        timeout=SEGMENT_REQUEST_TIMEOUT_SECONDS,
                    )
                    if not data:
                        raise OSError("empty segment")
                    temporary = target.with_suffix(".tmp")
                    temporary.write_bytes(data)
                    temporary.replace(target)
                    with self._claim_lock:
                        self._downloaded += 1
                        count = self._downloaded
                    if count % 100 == 0:
                        self._log(f"音声チャンク取得済み: {count}")
                    return True
                except Exception:
                    if self.manual_stop_event.is_set():
                        return False
                    if attempt == 4:
                        raise
                    self.manual_stop_event.wait(min(2**attempt, 8))
        finally:
            with self._claim_lock:
                self._claimed.discard(sequence)
        return False

    def _download_history(self, playlist_url: str, *, continuing_live: bool = False) -> None:
        body = str(self._read_url(playlist_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
        segments = self.parse_playlist(playlist_url, body)
        if not segments:
            raise SpaceArchiveError("過去音声チャンクが見つかりませんでした")
        self._remember_segments(segments)
        if continuing_live and "#EXT-X-ENDLIST" in body and not self.stop_event.is_set():
            self._log("履歴からSpaceの終了を検知しました。最終補完を開始します。")
            self.stop_event.set()
        duration = sum(
            float(line.partition(":")[2].rstrip(","))
            for line in body.splitlines()
            if line.startswith("#EXTINF:")
        )
        self._log(f"過去音声を取得: {len(segments)}チャンク（約{duration / 60:.1f}分）")
        failures = self._download_batch(segments)
        if self.manual_stop_event.is_set():
            return
        if failures:
            self._log(f"過去音声の再試行対象: {failures}チャンク")
            retry_segments = [item for item in segments if not self._segment_path(item[0]).is_file()]
            for sequence, url in retry_segments:
                self._download_segment(sequence, url)
        if continuing_live and self.stop_event.is_set():
            self._log("過去音声の取り込みが完了しました。Space終了処理へ進みます。")
        elif continuing_live:
            self._log("過去音声の取り込みが完了しました。ライブ音声を継続取得中です。")
        else:
            self._log("公開リプレイの取り込みが完了しました。")

    def _repair_history_once(self, playlist_url: str, *, final: bool = False) -> int:
        if self.manual_stop_event.is_set():
            return 0
        body = str(self._read_url(playlist_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
        if self.manual_stop_event.is_set():
            return 0
        segments = self.parse_playlist(playlist_url, body)
        if not segments:
            raise SpaceArchiveError("補完用の履歴プレイリストが空です")
        self._remember_segments(segments)
        if "#EXT-X-ENDLIST" in body and not self.stop_event.is_set():
            self._log("履歴からSpaceの終了を検知しました。最終補完を開始します。")
            self.stop_event.set()
        missing = self._missing_known_segments()
        if not missing:
            return 0

        stage = "最終補完" if final else "自動補完"
        self._log(f"欠落を検知: {len(missing)}チャンクを{stage}します…")
        failures = self._download_batch(missing)
        remaining = self._missing_known_segments()
        repaired = len(missing) - len(remaining)
        if repaired:
            self._log(f"{stage}完了: {repaired}チャンクを回収しました")
        if failures or remaining:
            self._log(
                f"{stage}待ち: {len(remaining)}チャンク。"
                "配信中は履歴を再確認して再試行します"
            )
        return repaired

    def _poll_history_repairs(self, playlist_url: str) -> None:
        consecutive_errors = 0
        while not self.stop_event.wait(HISTORY_REPAIR_INTERVAL_SECONDS):
            try:
                self._repair_history_once(playlist_url)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors in (1, 5):
                    self._log(f"履歴の自動補完待ち: {exc}")

    def _poll_live(self, dynamic_url: str) -> None:
        consecutive_errors = 0
        latest_sequence: int | None = None
        last_progress_at = time.monotonic()
        last_status_check_at = 0.0
        status_check_errors = 0
        while not self.stop_event.is_set():
            try:
                body = str(self._read_url(dynamic_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
                segments = self.parse_playlist(dynamic_url, body)
                self._remember_segments(segments)
                for sequence, url in segments:
                    self._download_segment(sequence, url)
                consecutive_errors = 0
                current_sequence = max((sequence for sequence, _ in segments), default=None)
                if current_sequence is not None and (
                    latest_sequence is None or current_sequence > latest_sequence
                ):
                    latest_sequence = current_sequence
                    last_progress_at = time.monotonic()
                    status_check_errors = 0
                    self._last_status_result = None
                if "#EXT-X-ENDLIST" in body and not self.stop_event.is_set():
                    self._log("Spaceの終了を検知しました。最終補完を開始します。")
                    self.stop_event.set()
                    break
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors in (1, 5):
                    self._log(f"ライブ更新待ち: {exc}")
            now = time.monotonic()
            if (
                not self.stop_event.is_set()
                and now - last_progress_at >= LIVE_STATUS_CHECK_AFTER_SECONDS
                and now - last_status_check_at >= LIVE_STATUS_CHECK_AFTER_SECONDS
            ):
                last_status_check_at = now
                try:
                    status = self._check_space_status()
                    status_check_errors = 0
                    if status == "live" and self._status_live_url:
                        dynamic_url = self._status_live_url
                except Exception as exc:
                    status_check_errors += 1
                    if status_check_errors in (1, 5):
                        self._log(f"Space状態の再確認待ち: {exc}")
            self.stop_event.wait(LIVE_POLL_INTERVAL_SECONDS)
        if not self.manual_stop_event.is_set():
            try:
                body = str(self._read_url(dynamic_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
                segments = self.parse_playlist(dynamic_url, body)
                self._remember_segments(segments)
                for sequence, url in segments:
                    self._download_segment(sequence, url)
            except Exception:
                pass

    @staticmethod
    def _safe_title(value: str) -> str:
        cleaned = INVALID_FILENAME_RE.sub("_", value).strip(" .")
        return cleaned[:100] or "X Space"

    @staticmethod
    def _unique_output_path(directory: Path, title: str, extension: str) -> Path:
        candidate = directory / f"{title}.{extension}"
        counter = 2
        while candidate.exists():
            candidate = directory / f"{title} ({counter}).{extension}"
            counter += 1
        return candidate

    def _finalize(self, title: str) -> Path:
        assert self._temp_dir is not None
        segments = sorted(self._temp_dir.glob("*.aac"), key=lambda path: int(path.stem))
        if not segments:
            raise SpaceArchiveError("保存できる音声チャンクがありません")
        numbers = [int(path.stem) for path in segments]
        with self._claim_lock:
            known_numbers = set(self._known_segments)
        unresolved = len(known_numbers.difference(numbers))
        if unresolved:
            self._log(
                f"警告: 履歴上で確認できた{unresolved}個の音声チャンクを補完できませんでした。"
                "欠落部分は無音を追加せず、前後をつないで保存します"
            )
        if known_numbers:
            source_gaps = (max(known_numbers) - min(known_numbers) + 1) - len(known_numbers)
            if source_gaps:
                self._log(
                    f"注意: Xが公開した履歴自体に{source_gaps}個のチャンク番号の飛びがあります"
                )

        combined_aac = self._temp_dir / "combined.aac"
        with combined_aac.open("wb") as combined:
            for segment in segments:
                with segment.open("rb") as source:
                    shutil.copyfileobj(source, combined, length=1024 * 1024)
        extension = "m4a" if self.audio_format in ("source", "m4a") else self.audio_format
        output = self._unique_output_path(
            self.output_dir,
            self._safe_title(title),
            extension,
        )
        codec_args = ["-vn", "-c:a", "copy"]
        if extension == "mp3":
            codec_args = ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        elif extension == "wav":
            codec_args = ["-vn", "-c:a", "pcm_s16le"]
        elif extension == "opus":
            codec_args = ["-vn", "-c:a", "libopus"]
        self._log(f"{len(segments)}チャンクを音声ファイルへ結合しています…")
        result = subprocess.run(
            [
                str(self.ffmpeg_location / "ffmpeg.exe"),
                "-y",
                "-loglevel",
                "error",
                "-f",
                "aac",
                "-i",
                str(combined_aac),
                *codec_args,
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode or not output.is_file():
            raise SpaceArchiveError(f"音声ファイルの結合に失敗しました: {result.stderr.strip()}")
        shutil.rmtree(self._temp_dir)
        return output

    def _run(self) -> None:
        output: Path | None = None
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            info = self._extract_info(allow_no_formats=True)
            title = str(info.get("fulltitle") or info.get("title") or "X Space")
            live_status = str(info.get("live_status") or "")
            stream_url = str(info.get("url") or "")
            self._temp_dir = self.output_dir / f".recording-{self.space_id}-{int(time.time())}"
            self._temp_dir.mkdir(parents=True, exist_ok=False)
            self._log(f"Space: {title}")

            if live_status == "is_live":
                replay_url = self._derive_live_replay_url(stream_url)
                self._log("ライブ開始前まで遡り、現在音声を並行取得します。")
                poller = threading.Thread(target=self._poll_live, args=(stream_url,), daemon=True)
                poller.start()
                self._download_history(replay_url, continuing_live=True)
                repairer: threading.Thread | None = None
                if not self.stop_event.is_set():
                    repairer = threading.Thread(
                        target=self._poll_history_repairs,
                        args=(replay_url,),
                        daemon=True,
                    )
                    repairer.start()
                self.stop_event.wait()
                poller.join()
                if repairer:
                    repairer.join()
                if self.manual_stop_event.is_set():
                    self._log("録音を強制停止しました。最終補完を省略して保存します。")
                elif not self._ended_without_replay:
                    try:
                        self._repair_history_once(
                            self._status_replay_url or replay_url,
                            final=True,
                        )
                    except Exception as exc:
                        self._log(f"最終補完用の履歴を確認できませんでした: {exc}")
            elif live_status in ("was_live", "post_live"):
                if not stream_url:
                    raise SpaceArchiveError(
                        "終了済みSpaceですが、公開リプレイがありません。"
                        "ホストが録音を残していないか、リプレイが削除されています"
                    )
                self._log("終了済みSpaceの公開リプレイを取得します。")
                self._download_history(stream_url)
            else:
                raise SpaceArchiveError(f"現在のSpace状態は取得対象外です: {live_status or '不明'}")

            output = self._finalize(title)
            self._log(f"保存完了: {output}")
            self.on_done(True, output, None)
        except Exception as exc:
            self._log(f"エラー: {exc}")
            self.on_done(False, output, str(exc))
