#!/usr/bin/env python3
"""Eagle Library Watcher

Monitors the Eagle library's images/ directory via FSEvents (watchdog).
When a new metadata.json appears (new item imported), schedules a debounced
call to `auto_tag.py --item-id <ID>` so newly imported images get auto-tagged.

Designed to run as a launchd LaunchAgent. Logs to stdout/stderr (launchd
redirects these to the paths defined in the plist).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

SCRIPT_DIR = Path(__file__).resolve().parent
AUTO_TAG = SCRIPT_DIR / "auto_tag.py"

# Default library — override via EAGLE_LIBRARY env var (or the plist).
DEFAULT_LIBRARY = Path(
    os.path.expanduser("~/Eagle/MyLibrary.library")
)

# Wait this long after last filesystem event for an item before processing.
# Eagle writes metadata.json several times during import (thumbnail, palette,
# etc.) — we want to run only after writes have settled.
DEBOUNCE_SECONDS = 8.0

# Regex to extract Eagle item id from paths like .../images/MXXXX.info/metadata.json
ID_RE = re.compile(r"/images/([A-Z0-9]+)\.info/")


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class DebouncedQueue:
    """Collect item ids, schedule processing after DEBOUNCE_SECONDS of quiet."""

    def __init__(self, delay: float, worker):
        self._delay = delay
        self._worker = worker
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="debounce")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def push(self, item_id: str) -> None:
        with self._cond:
            self._pending[item_id] = time.monotonic()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stop:
                    return
                if not self._pending:
                    self._cond.wait()
                    continue
                now = time.monotonic()
                due = [k for k, t in self._pending.items() if now - t >= self._delay]
                if due:
                    for k in due:
                        del self._pending[k]
                else:
                    # Wait until the oldest pending is due
                    oldest = min(self._pending.values())
                    self._cond.wait(timeout=max(0.1, self._delay - (now - oldest)))
                    continue
            for item_id in due:
                try:
                    self._worker(item_id)
                except Exception as e:  # noqa: BLE001
                    log(f"worker error for {item_id}: {e}")


class EagleHandler(FileSystemEventHandler):
    def __init__(self, queue: DebouncedQueue):
        self.queue = queue

    def _enqueue_from_path(self, path: str) -> None:
        m = ID_RE.search(path)
        if not m:
            return
        item_id = m.group(1)
        # Only act on metadata.json events (actual item manifests)
        if not path.endswith("metadata.json"):
            return
        self.queue.push(item_id)

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue_from_path(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._enqueue_from_path(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dst = getattr(event, "dest_path", "") or ""
        self._enqueue_from_path(dst)


def load_env_file(env_path: Path) -> None:
    """Minimal .env loader — populates os.environ if keys are missing."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def run_auto_tag(item_id: str) -> None:
    log(f"→ tagging {item_id}")
    result = subprocess.run(
        [sys.executable, str(AUTO_TAG), "--item-id", item_id],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode == 0:
        log(f"✅ {item_id}: {result.stdout.strip().splitlines()[-1] if result.stdout.strip() else 'ok'}")
    else:
        tail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        log(f"❌ {item_id} exit={result.returncode}: {' | '.join(tail[-3:]) if tail else '(no output)'}")


def main() -> int:
    # auto_tag.py uses the `claude` CLI under the user's Claude subscription
    # (OAuth) — no ANTHROPIC_API_KEY needed. Optionally load a local .env next
    # to this script for convenience vars (e.g. EAGLE_LIBRARY, CLAUDE_BIN).
    load_env_file(SCRIPT_DIR / ".env")

    library = Path(os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)))
    watch_dir = library / "images"
    if not watch_dir.exists():
        log(f"❌ watch dir not found: {watch_dir}")
        return 1

    log(f"📚 library: {library.name}")
    log(f"👁  watching: {watch_dir}")
    log(f"⏱  debounce: {DEBOUNCE_SECONDS}s")

    queue = DebouncedQueue(DEBOUNCE_SECONDS, run_auto_tag)
    queue.start()

    observer = Observer()
    observer.schedule(EagleHandler(queue), str(watch_dir), recursive=True)
    observer.start()
    log("🚀 watcher started")

    try:
        while observer.is_alive():
            observer.join(timeout=1.0)
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        queue.stop()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
