"""Offline HTTP/FFmpeg integration test. Uses generated tones, never X or cookies."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive
import recorder


def wait_for(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for recording progress")


def run_process(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def main():
    location = recorder.find_ffmpeg()
    if not location:
        raise SystemExit("FFmpeg is required for this optional integration test")
    ffmpeg = str(Path(location) / "ffmpeg.exe")
    ffprobe = str(Path(location) / "ffprobe.exe")
    with tempfile.TemporaryDirectory(prefix="x-space-recorder-integration-") as directory:
        root = Path(directory)
        tone = root / "tone.aac"
        run_process([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.25", "-c:a", "aac", "-f", "adts", str(tone)])
        data = tone.read_bytes()
        chunk_duration = float(run_process([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(tone)]).strip())
        ended = threading.Event()
        lock = threading.Lock()
        requests = Counter()
        fail_once = {"/playlist.m3u8", "/chunk_0_2_a.aac", "/chunk_0_7_a.aac"}

        def playlist(numbers):
            body = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
            for number in numbers:
                body += f"#EXTINF:{chunk_duration},\nchunk_0_{number}_a.aac\n"
            return (body + ("#EXT-X-ENDLIST\n" if ended.is_set() else "")).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                with lock:
                    requests[path] += 1
                    fail = path in fail_once and requests[path] == 1
                if fail:
                    self.send_error(503, "simulated transient failure")
                    return
                if path == "/master_playlist.m3u8":
                    content = b"#EXTM3U\nplaylist.m3u8\n"
                elif path == "/playlist.m3u8":
                    content = playlist(range(12))
                elif path == "/dynamic_playlist.m3u8":
                    content = playlist((9, 10, 11))
                elif path.endswith(".aac"):
                    content = data
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        live_url = base_url + "/dynamic_playlist.m3u8?type=live"
        history_url = base_url + "/playlist.m3u8?type=replay"
        logs = []
        sessions = []
        outcomes = []

        def session_for(audio_format, title, live):
            outcome = []
            session = archive.SpaceArchiveSession(
                url="https://x.com/i/spaces/test", space_id="test", output_dir=root,
                audio_format=audio_format, browser=None, ytdlp_prefix=["unused"],
                ffmpeg_location=location, on_log=logs.append,
                on_done=lambda *args: outcome.append(args),
            )
            session._extract_info = lambda **_kwargs: {"title": title, "live_status": "is_live" if live else "was_live", "url": live_url if live else history_url}
            sessions.append(session)
            return session, outcome

        try:
            with patch("archive.HISTORY_REPAIR_INTERVAL_SECONDS", 0.2), patch("archive.LIVE_POLL_INTERVAL_SECONDS", 0.1):
                session, outcome = session_for("source", "Live repair", True)
                session.start()
                wait_for(lambda: session.progress.downloaded_chunks == 12)
                deleted = [1, 2, 3, 7, 10]
                for number in deleted:
                    session._segment_path(number).unlink()
                wait_for(lambda: all(session._segment_path(number).is_file() for number in deleted))
                ended.set()
                wait_for(lambda: bool(outcome))
                if not outcome[0][0]:
                    raise AssertionError(outcome[0])
                outcomes.append(("live-repair", str(outcome[0][1].name)))
                assert any("過去音声の取得待ち:" in line for line in logs)
                assert any("自動補完完了:" in line for line in logs)
                assert not any("補完できませんでした" in line for line in logs)

                for audio_format in ("m4a", "mp3", "wav", "opus"):
                    session, outcome = session_for(audio_format, f"Replay {audio_format}", False)
                    session.start()
                    wait_for(lambda: bool(outcome))
                    if not outcome[0][0]:
                        raise AssertionError(outcome[0])
                    outcomes.append((audio_format, str(outcome[0][1].name)))

                ended.clear()
                session, outcome = session_for("source", "Manual stop", True)
                session.start()
                wait_for(lambda: session.progress.downloaded_chunks > 0)
                session.stop()
                wait_for(lambda: bool(outcome))
                assert outcome[0][0] and outcome[0][1].is_file()
                outcomes.append(("manual-stop", outcome[0][1].name))

            for label, filename in outcomes:
                info = json.loads(run_process([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_name", "-of", "json", str(root / filename)]))
                duration = float(info["format"]["duration"])
                assert duration > 0
                if label != "manual-stop":
                    assert abs(duration - 12 * chunk_duration) < 0.25, (label, duration)
                run_process([ffmpeg, "-v", "error", "-i", str(root / filename), "-f", "null", "-"])
                print(json.dumps({"case": label, "duration": round(duration, 3), "codec": info["streams"][0]["codec_name"], "result": "pass"}))
            assert not list(root.glob(".recording-*")), "Temporary chunks remain after successful saves"
            print("PASS: live recovery, grouped/scattered deletions, automatic end, manual stop, all formats decoded")
        finally:
            for session in sessions:
                if session.is_running:
                    session.stop()
                    session.thread.join(15)
            server.shutdown()
            server.server_close()
            server_thread.join(2)


if __name__ == "__main__":
    main()
