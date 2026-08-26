"""Real-network check for live/replay history gap repair.

Usage:
    python tests/integration_live_repair.py https://x.com/i/spaces/SPACE_ID
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import archive
import recorder


def main() -> int:
    if len(sys.argv) != 2:
        print("X Space URLを1個指定してください", file=sys.stderr)
        return 2

    url, space_id = recorder.normalize_space_url(sys.argv[1])
    project = Path(__file__).resolve().parents[1]
    ytdlp = project / ".tools" / "yt-dlp.exe"
    if not ytdlp.is_file():
        print(".tools/yt-dlp.exeがありません。build-app.ps1を一度実行してください", file=sys.stderr)
        return 2

    result = subprocess.run(
        [str(ytdlp), "--ignore-config", "--no-download", "--dump-single-json", "--", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    info = json.loads(result.stdout)

    logs: list[str] = []
    with tempfile.TemporaryDirectory(prefix="x-space-repair-test-") as directory:
        session = archive.SpaceArchiveSession(
            url=url,
            space_id=space_id,
            output_dir=Path(directory),
            audio_format="source",
            browser=None,
            ytdlp_prefix=[str(ytdlp)],
            ffmpeg_location=recorder.find_ffmpeg() or "C:/ffmpeg",
            on_log=logs.append,
            on_done=lambda *_: None,
        )
        session._temp_dir = Path(directory)
        stream_url = str(info.get("url") or "")
        if not stream_url:
            raise RuntimeError("音声プレイリストURLがありません")
        replay_url = (
            session._derive_live_replay_url(stream_url)
            if info.get("live_status") == "is_live"
            else stream_url
        )
        playlist = str(session._read_url(replay_url))
        segments = session.parse_playlist(replay_url, playlist)
        if len(segments) < 3:
            raise RuntimeError(f"テストに必要な履歴チャンクが不足しています: {len(segments)}")

        session._remember_segments(segments)
        for sequence, _segment_url in segments:
            session._segment_path(sequence).write_bytes(b"existing-test-data")

        middle = len(segments) // 2
        target_indexes = {
            len(segments) // 4,
            len(segments) // 3,
            middle - 2,
            middle - 1,
            middle,
            middle + 1,
            middle + 2,
            (len(segments) * 3) // 4,
        }
        targets = [segments[index] for index in sorted(target_indexes)]
        for sequence, _target_url in targets:
            session._segment_path(sequence).unlink()
        missing_sequences = {sequence for sequence, _url in session._missing_known_segments()}
        target_sequences = {sequence for sequence, _url in targets}
        if missing_sequences != target_sequences:
            raise AssertionError(
                f"意図した欠損状態にできませんでした: expected={target_sequences}, actual={missing_sequences}"
            )

        original_read_url = session._read_url

        def frozen_playlist(url_to_read: str, *, binary: bool = False, timeout: float = 30):
            return (
                original_read_url(url_to_read, binary=True, timeout=timeout)
                if binary
                else playlist
            )

        with patch.object(session, "_read_url", side_effect=frozen_playlist):
            repairer = threading.Thread(
                target=session._poll_history_repairs,
                args=(replay_url,),
                daemon=True,
            )
            repairer.start()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not all(
                session._segment_path(sequence).is_file() for sequence, _url in targets
            ):
                time.sleep(0.1)
            session.stop_event.set()
            repairer.join(timeout=3)

        repaired_data = {
            sequence: session._segment_path(sequence).read_bytes()
            if session._segment_path(sequence).is_file()
            else b""
            for sequence, _url in targets
        }
        repaired = sum(
            bool(data and data != b"existing-test-data") for data in repaired_data.values()
        )
        if repaired != len(targets):
            raise AssertionError(
                f"自動補完に失敗しました: expected={len(targets)}, repaired={repaired}"
            )

        print(f"Space: {info.get('fulltitle') or info.get('title')}")
        print(f"状態: {info.get('live_status')}")
        print(f"履歴チャンク: {len(segments)}")
        print(f"意図的に削除したチャンク番号: {sorted(target_sequences)}")
        print("内訳: 中央の連続5チャンク＋離れた3チャンク")
        print(f"自動補完したチャンク: {repaired}")
        print(f"再取得バイト数: {sum(len(data) for data in repaired_data.values())}")
        print("ログ:")
        print("".join(logs), end="")
        print("実URLでの欠損自動補完テスト: 成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
