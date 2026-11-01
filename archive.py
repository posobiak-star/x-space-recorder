"""Backfill and continuously archive X Space HLS audio segments."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SEGMENT_SEQUENCE_RE = re.compile(r"_(\d+)_[a-z]+\.aac(?:\?|$)")
PUBLIC_HISTORY_PATH_RE = re.compile(r"(/Transcoding/v1/hls/[^/]+)/transcode/([^/]+/[^/]+)/[^/]+/(audio-space/[^/]+)$")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
HISTORY_REPAIR_INTERVAL_SECONDS = 5.0
LIVE_POLL_INTERVAL_SECONDS = 1.5
SEGMENT_REQUEST_TIMEOUT_SECONDS = 10.0
PLAYLIST_REQUEST_TIMEOUT_SECONDS = 10.0
LIVE_STATUS_CHECK_AFTER_SECONDS = 30.0
SPACE_INFO_TIMEOUT_SECONDS = 30.0
FINAL_REPAIR_ATTEMPTS = 4
FINAL_REPAIR_RETRY_SECONDS = 5.0


class SpaceArchiveError(RuntimeError):
    pass


class SpaceArchiveCancelled(SpaceArchiveError):
    pass


class SpaceArchiveStorageError(SpaceArchiveError):
    pass


@dataclass(frozen=True)
class ArchiveProgress:
    phase: str
    title: str
    downloaded_chunks: int
    known_chunks: int
    downloaded_bytes: int
    audio_seconds: float
    elapsed_seconds: float


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
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._claim_lock = threading.Lock()
        self._claimed: set[int] = set()
        self._known_segments: dict[int, str] = {}
        self._downloaded = 0
        self._saved_sizes: dict[int, int] = {}
        self._segment_durations: dict[int, float] = {}
        self._phase = "preparing"
        self._title = "Space情報を確認しています"
        self._started_at = time.monotonic()
        self._finished_at: float | None = None
        self._temp_dir: Path | None = None
        self._live_url: str | None = None
        self._history_replay_url: str | None = None
        self._status_replay_url: str | None = None
        self._status_live_url: str | None = None
        self._ended_without_replay = False
        self._last_status_result: str | None = None
        self._last_repair_pending: set[int] = set()
        self._worker_error: SpaceArchiveStorageError | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.manual_stop_event.set()
        self.cancel_event.set()
        self.stop_event.set()
        self._set_phase("stopping")

    @property
    def progress(self) -> ArchiveProgress:
        with self._claim_lock:
            return ArchiveProgress(
                phase=self._phase,
                title=self._title,
                downloaded_chunks=len(self._saved_sizes),
                known_chunks=len(self._known_segments),
                downloaded_bytes=sum(self._saved_sizes.values()),
                audio_seconds=sum(self._segment_durations.get(number, 0) for number in self._saved_sizes),
                elapsed_seconds=(self._finished_at or time.monotonic()) - self._started_at,
            )

    @property
    def recovery_dir(self) -> Path | None:
        if self._temp_dir and any(path.stem.isdigit() for path in self._temp_dir.glob("*.aac")):
            return self._temp_dir
        return None

    def _set_phase(self, phase: str) -> None:
        with self._claim_lock:
            if self.manual_stop_event.is_set() and phase not in ("stopping", "saving", "done", "error", "cancelled"):
                return
            self._phase = phase

    def _cancel_requested(self) -> bool:
        return self.cancel_event.is_set() or self.manual_stop_event.is_set()

    def _storage_failed(self, error: SpaceArchiveStorageError) -> None:
        with self._claim_lock:
            self._worker_error = error
        self.cancel_event.set()
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
                if self._cancel_requested():
                    process.kill()
                    process.communicate()
                    raise SpaceArchiveCancelled("録音停止によりSpace情報の確認を中断しました")
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
        parts = urllib.parse.urlsplit(dynamic_url)
        if not parts.path.endswith("/dynamic_playlist.m3u8"):
            raise SpaceArchiveError("ライブ履歴プレイリストを特定できませんでした")
        directory = parts.path.rsplit("/", 1)[0]
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if key != "type"]
        master_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"{directory}/master_playlist.m3u8", urllib.parse.urlencode(query), ""))
        master = str(self._read_url(master_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
        playlist_paths = [
            line.strip()
            for line in master.splitlines()
            if line.strip() and not line.startswith("#") and ".m3u8" in line
        ]
        if not playlist_paths:
            raise SpaceArchiveError("ライブ履歴のマスタープレイリストが空です")
        target = urllib.parse.urlsplit(urllib.parse.urljoin(master_url, playlist_paths[0]))
        history_query = urllib.parse.urlencode([*query, ("type", "replay")])
        candidates = [urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"{directory}/{Path(target.path).name}", history_query, ""))]
        public = PUBLIC_HISTORY_PATH_RE.search(target.path)
        if public:
            # The replay store holds every chunk, unlike the live store, and serves them next to this playlist.
            public_path = f"{public[1]}/non_transcode/{public[2]}/{public[3]}"
            candidates.insert(0, urllib.parse.urlunsplit((target.scheme, target.netloc, public_path, history_query, "")))
        for candidate in candidates:
            try:
                self._read_url(candidate, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS)
                return candidate
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
        return urllib.parse.urlunsplit((target.scheme, target.netloc, target.path, history_query, ""))

    def _segment_path(self, sequence: int) -> Path:
        assert self._temp_dir is not None
        return self._temp_dir / f"{sequence:09d}.aac"

    def _remember_segments(self, segments: list[tuple[int, str]]) -> None:
        with self._claim_lock:
            for sequence, url in segments:
                self._known_segments[sequence] = url

    def _remember_playlist(self, playlist_url: str, body: str) -> list[tuple[int, str]]:
        # A transcode playlist cannot serve its own chunks, so fall back to the live store.
        uses_live_store = self._live_url and "/transcode/" in urllib.parse.urlsplit(playlist_url).path
        base_url = self._live_url if uses_live_store else playlist_url
        segments = self.parse_playlist(base_url, body)
        durations = []
        duration = 0.0
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("#EXTINF:"):
                try:
                    duration = max(0.0, float(line.partition(":")[2].split(",", 1)[0]))
                    if not math.isfinite(duration):
                        duration = 0.0
                except ValueError:
                    duration = 0.0
            elif line and not line.startswith("#") and ".m3u8" not in line:
                durations.append(duration)
                duration = 0.0
        with self._claim_lock:
            for (sequence, url), seconds in zip(segments, durations):
                self._known_segments[sequence] = url
                self._segment_durations[sequence] = seconds
        return segments

    def _missing_known_segments(self) -> list[tuple[int, str]]:
        with self._claim_lock:
            known = sorted(self._known_segments.items())
            claimed = self._claimed.copy()
        missing = [
            (sequence, url)
            for sequence, url in known
            if sequence not in claimed and not self._segment_path(sequence).is_file()
        ]
        with self._claim_lock:
            for sequence, _url in missing:
                self._saved_sizes.pop(sequence, None)
        return missing

    def _download_batch(self, segments: list[tuple[int, str]], *, workers: int = 8, attempts: int = 5) -> int:
        failures = 0
        entries = iter(segments)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="space-audio") as executor:
            pending = set()

            def submit_next() -> None:
                if not self._cancel_requested():
                    item = next(entries, None)
                    if item is not None:
                        pending.add(executor.submit(self._download_segment, *item, attempts=attempts))

            for _ in range(workers):
                submit_next()
            while pending:
                if self._cancel_requested():
                    for future in pending:
                        future.cancel()
                    break
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    try:
                        future.result()
                    except SpaceArchiveStorageError as exc:
                        self._storage_failed(exc)
                        raise
                    except Exception:
                        failures += 1
                    submit_next()
        return failures

    def _download_segment(self, sequence: int, url: str, *, attempts: int = 5) -> bool:
        if self._cancel_requested():
            return False
        target = self._segment_path(sequence)
        with self._claim_lock:
            if target.is_file() or sequence in self._claimed:
                return False
            self._claimed.add(sequence)
        try:
            for attempt in range(attempts):
                if self._cancel_requested():
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
                    try:
                        temporary.write_bytes(data)
                        temporary.replace(target)
                    except OSError as exc:
                        raise SpaceArchiveStorageError(
                            f"音声データを保存先に書き込めません。空き容量とアクセス権を確認してください: {exc}"
                        ) from exc
                    with self._claim_lock:
                        self._downloaded += 1
                        self._saved_sizes[sequence] = len(data)
                        count = self._downloaded
                    if count % 100 == 0:
                        self._log(f"音声チャンク取得済み: {count}")
                    return True
                except SpaceArchiveStorageError:
                    raise
                except Exception as exc:
                    if self._cancel_requested():
                        return False
                    # Some listed chunks never exist; the history repair retries them without backing off here.
                    if attempt == attempts - 1 or (isinstance(exc, urllib.error.HTTPError) and exc.code == 404):
                        raise
                    self.cancel_event.wait(min(2**attempt, 8))
        finally:
            with self._claim_lock:
                self._claimed.discard(sequence)
        return False

    def _download_history(self, playlist_url: str, *, continuing_live: bool = False) -> None:
        body = str(self._read_url(playlist_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
        if self._cancel_requested():
            return
        segments = self._remember_playlist(playlist_url, body)
        if not segments:
            raise SpaceArchiveError("過去音声チャンクが見つかりませんでした")
        with self._claim_lock:
            duration = sum(self._segment_durations.get(sequence, 0) for sequence, _url in segments)
        self._log(f"過去音声を取得: {len(segments)}チャンク（約{duration / 60:.1f}分）")
        failures = self._download_batch(segments)
        if self._cancel_requested():
            return
        if failures:
            self._log(f"過去音声の再試行対象: {failures}チャンク")
            retry_segments = [item for item in segments if not self._segment_path(item[0]).is_file()]
            for sequence, url in retry_segments:
                if self._cancel_requested():
                    return
                try:
                    self._download_segment(sequence, url)
                except SpaceArchiveStorageError:
                    raise
                except Exception:
                    # A permanently unavailable chunk must not discard the audio already saved.
                    pass
        if continuing_live and self.stop_event.is_set():
            self._log("過去音声の取り込みが完了しました。Space終了処理へ進みます。")
        elif continuing_live:
            self._log("過去音声の取り込みが完了しました。ライブ音声を継続取得中です。")
        else:
            self._log("公開リプレイの取り込みが完了しました。")

    def _repair_history_once(self, playlist_url: str, *, final: bool = False) -> int:
        if self._cancel_requested():
            return 0
        body = str(self._read_url(playlist_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
        if self._cancel_requested():
            return 0
        with self._claim_lock:
            previous_known = set(self._known_segments)
            previous_latest = max(previous_known, default=-1)
        segments = self._remember_playlist(playlist_url, body)
        if not segments:
            raise SpaceArchiveError("補完用の履歴プレイリストが空です")
        # X marks the history as ended even during a live Space, so only the live playlist can signal the end.
        missing = self._missing_known_segments()
        if not missing:
            self._last_repair_pending = set()
            return 0

        stage = "最終補完" if final else "自動補完"
        # New chunks at the live edge are normal arrivals, not recording gaps.
        reported = {sequence for sequence, _url in missing if final or sequence in previous_known or sequence < previous_latest}
        previous_pending = self._last_repair_pending
        if reported and reported != previous_pending:
            self._log(f"欠落を検知: {len(reported)}チャンクを{stage}します…")
        self._download_batch(missing)
        remaining = self._missing_known_segments()
        repaired = len(missing) - len(remaining)
        reported_remaining = reported.intersection(sequence for sequence, _url in remaining)
        reported_repaired = len(reported) - len(reported_remaining)
        if reported_repaired:
            self._log(f"{stage}完了: {reported_repaired}チャンクを回収しました")
        if reported_remaining and reported_remaining != previous_pending:
            if final:
                self._log(f"最終補完で未取得: {len(reported_remaining)}チャンク")
            else:
                self._log(
                    f"自動補完待ち: {len(reported_remaining)}チャンク。"
                    "配信中は履歴を再確認して再試行します"
                )
        self._last_repair_pending = reported_remaining
        return repaired

    def _final_repair(self, stream_url: str) -> None:
        # The replay can lag right after a Space ends, and this is the last chance to fill gaps.
        self._last_repair_pending = set()
        for attempt in range(FINAL_REPAIR_ATTEMPTS):
            failure: Exception | None = None
            try:
                if not self._status_replay_url:
                    try:
                        self._history_replay_url = self._derive_live_replay_url(self._live_url or stream_url)
                    except Exception:
                        # The live master can vanish once a Space ends; an older snapshot may still help.
                        if not self._history_replay_url:
                            raise
                self._repair_history_once(self._status_replay_url or self._history_replay_url, final=True)
                if not self._missing_known_segments():
                    return
            except SpaceArchiveStorageError as exc:
                self._log(f"最終補完を中断しました: {exc}")
                return
            except Exception as exc:
                failure = exc
                if attempt == 0:
                    self._log(f"最終補完の再試行待ち: {exc}")
            if attempt == FINAL_REPAIR_ATTEMPTS - 1:
                if failure:
                    self._log(f"最終補完用の履歴を確認できませんでした: {failure}")
                return
            if self.cancel_event.wait(FINAL_REPAIR_RETRY_SECONDS):
                return

    def _poll_history_repairs(self, playlist_url: str | None, dynamic_url: str | None = None) -> None:
        consecutive_errors = 0
        while not self.stop_event.wait(HISTORY_REPAIR_INTERVAL_SECONDS):
            try:
                # Each history playlist is a frozen snapshot, and newer ones get a new name.
                live_url = self._live_url or dynamic_url
                if live_url:
                    playlist_url = self._derive_live_replay_url(live_url)
                    self._history_replay_url = playlist_url
                assert playlist_url is not None
                self._repair_history_once(playlist_url)
                consecutive_errors = 0
            except Exception as exc:
                if self._worker_error:
                    return
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
                segments = self._remember_playlist(dynamic_url, body)
                # History repair retries slow/missing chunks; keep the live poll responsive.
                self._download_batch(segments, workers=4, attempts=1)
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
                if self._worker_error:
                    return
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
                        self._live_url = dynamic_url
                except Exception as exc:
                    status_check_errors += 1
                    if status_check_errors in (1, 5):
                        self._log(f"Space状態の再確認待ち: {exc}")
            self.stop_event.wait(LIVE_POLL_INTERVAL_SECONDS)
        if not self._cancel_requested():
            try:
                body = str(self._read_url(dynamic_url, timeout=PLAYLIST_REQUEST_TIMEOUT_SECONDS))
                segments = self._remember_playlist(dynamic_url, body)
                self._download_batch(segments, workers=4, attempts=1)
            except Exception:
                pass

    @staticmethod
    def _safe_title(value: str) -> str:
        cleaned = INVALID_FILENAME_RE.sub("_", value).strip(" .")
        cleaned = cleaned[:100].rstrip(" .") or "X Space"
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", cleaned.split(".", 1)[0], re.IGNORECASE):
            cleaned = f"_{cleaned}"
        return cleaned

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
        segments = sorted((path for path in self._temp_dir.glob("*.aac") if path.stem.isdigit()), key=lambda path: int(path.stem))
        if not segments:
            raise SpaceArchiveError("保存できる音声チャンクがありません")
        numbers = [int(path.stem) for path in segments]
        with self._claim_lock:
            known_numbers = set(self._known_segments)
        unresolved = len(known_numbers.difference(numbers))
        if unresolved:
            if self.manual_stop_event.is_set():
                self._log(f"注意: 手動停止のため{unresolved}個の未取得チャンクを省略し、取得済み分だけを保存します")
            else:
                self._log(
                    f"警告: 履歴上で確認できた{unresolved}個の音声チャンクを補完できませんでした。"
                    "欠落部分は無音を追加せず、前後をつないで保存します"
                )
        if known_numbers:
            source_gaps = (max(known_numbers) - min(known_numbers) + 1) - len(known_numbers)
            if source_gaps:
                self._log(
                    f"警告: {source_gaps}個のチャンク番号を取得・確認できませんでした。その部分の音声は抜けています"
                )

        combined_aac = self._temp_dir / "combined.aac"
        with combined_aac.open("wb") as combined:
            for segment in segments:
                with segment.open("rb") as source:
                    shutil.copyfileobj(source, combined, length=1024 * 1024)
        extension = "m4a" if self.audio_format in ("source", "m4a") else self.audio_format
        converted = self._temp_dir / f"encoded.{extension}"
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
                str(converted),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode or not converted.is_file() or not converted.stat().st_size:
            raise SpaceArchiveError(f"音声ファイルの結合に失敗しました: {result.stderr.strip()}")
        # Reserve a unique name across simultaneous app instances before publishing the file.
        while True:
            output = self._unique_output_path(self.output_dir, self._safe_title(title), extension)
            try:
                with output.open("xb"):
                    pass
                break
            except FileExistsError:
                continue
        try:
            converted.replace(output)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        try:
            shutil.rmtree(self._temp_dir)
        except OSError:
            self._log("注意: 音声の保存は完了しましたが、一時データを削除できませんでした。")
        return output

    def _run(self) -> None:
        output: Path | None = None
        workers: list[threading.Thread] = []
        success = False
        error: str | None = None
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            info = self._extract_info(allow_no_formats=True)
            if self._cancel_requested():
                raise SpaceArchiveCancelled("録音を中止しました")
            title = str(info.get("fulltitle") or info.get("title") or "X Space")
            with self._claim_lock:
                self._title = title
            live_status = str(info.get("live_status") or "")
            stream_url = str(info.get("url") or "")
            self._temp_dir = Path(tempfile.mkdtemp(prefix=f".recording-{self.space_id}-", dir=self.output_dir))
            self._log(f"Space: {title}")

            if live_status == "is_live":
                if not stream_url:
                    raise SpaceArchiveError("進行中Spaceの音声URLを取得できませんでした。ログイン設定を確認してください")
                self._live_url = stream_url
                self._set_phase("history")
                self._log("ライブ開始前まで遡り、現在音声を並行取得します。")
                poller = threading.Thread(target=self._poll_live, args=(stream_url,), daemon=True)
                poller.start()
                workers.append(poller)
                replay_url: str | None = None
                try:
                    replay_url = self._derive_live_replay_url(stream_url)
                    self._history_replay_url = replay_url
                    self._download_history(replay_url, continuing_live=True)
                except SpaceArchiveStorageError:
                    raise
                except Exception as exc:
                    if not self._cancel_requested():
                        self._log(f"過去音声の取得待ち: {exc}。ライブ取得を続け、履歴を再試行します。")
                if not self.stop_event.is_set():
                    self._set_phase("live")
                    repairer = threading.Thread(
                        target=self._poll_history_repairs,
                        args=(replay_url, stream_url),
                        daemon=True,
                    )
                    repairer.start()
                    workers.append(repairer)
                self.stop_event.wait()
                for worker in workers:
                    worker.join()
                if self._worker_error:
                    raise self._worker_error
                if self.manual_stop_event.is_set():
                    self._log("録音を強制停止しました。最終補完を省略して保存します。")
                # A Space without a public replay can still have its history readable for a while.
                elif self._history_replay_url or not self._ended_without_replay:
                    self._set_phase("final_repair")
                    self._final_repair(stream_url)
            elif live_status in ("was_live", "post_live"):
                if not stream_url:
                    raise SpaceArchiveError(
                        "終了済みSpaceですが、公開リプレイがありません。"
                        "ホストが録音を残していないか、リプレイが削除されています"
                    )
                self._log("終了済みSpaceの公開リプレイを取得します。")
                self._set_phase("history")
                self._download_history(stream_url)
            else:
                raise SpaceArchiveError(f"現在のSpace状態は取得対象外です: {live_status or '不明'}")

            if self.manual_stop_event.is_set() and not self.recovery_dir:
                raise SpaceArchiveCancelled("録音を中止しました。保存できる音声はまだありません")
            self._set_phase("saving")
            output = self._finalize(title)
            self._log(f"保存完了: {output}")
            success = True
            self._set_phase("done")
        except SpaceArchiveCancelled as exc:
            self._log(str(exc))
            success = True
            self._set_phase("cancelled")
        except Exception as exc:
            error = str(exc)
            self._log(f"エラー: {exc}")
            self._set_phase("error")
        finally:
            self.cancel_event.set()
            self.stop_event.set()
            for worker in workers:
                worker.join()
            if not success and self.recovery_dir:
                self._log(f"取得済みの音声チャンクは残しています: {self._temp_dir}")
            elif not output and self._temp_dir:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            with self._claim_lock:
                self._finished_at = time.monotonic()
            self.on_done(success, output, error)
