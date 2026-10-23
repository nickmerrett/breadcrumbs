"""
Filesystem watcher (SPEC §9, §17.7).

Watches data/entries/ and data/lists/ for external edits (Obsidian, vim, etc.)
and re-indexes the changed file into SQLite within a few seconds.

Started as a background daemon thread from create_app(); stops cleanly when the
process exits. Requires the 'watchdog' package (in requirements.txt).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filestore import FileStore

log = logging.getLogger("breadcrumbs.watcher")

_SETTLE = 2.0  # seconds to wait after the last event before re-indexing


def _try_import_watchdog():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
        return Observer, FileSystemEventHandler
    except ImportError:
        return None, None


class _Handler:
    """Debounced handler: accumulates dirty file_ids and flushes after _SETTLE seconds."""

    def __init__(self, fs: "FileStore"):
        self.fs = fs
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # Called by watchdog from its internal thread.
    def on_change(self, path: str) -> None:
        p = Path(path)
        if p.suffix != ".md":
            return
        file_id = p.stem
        with self._lock:
            self._dirty.add(file_id)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(_SETTLE, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            ids = list(self._dirty)
            self._dirty.clear()
            self._timer = None
        for file_id in ids:
            path = self.fs._path(file_id)
            if not path.exists():
                # File deleted — remove from index.
                sql_id = self.fs.sql_id_for(file_id)
                if sql_id:
                    with self.fs.sql.session() as s:
                        s.c.execute("DELETE FROM entry_tags WHERE entry_id=?", (sql_id,))
                        s.c.execute("DELETE FROM fts WHERE rowid=?", (sql_id,))
                        s.c.execute("DELETE FROM entries WHERE id=?", (sql_id,))
                    log.info("watcher: removed deleted entry %s", file_id)
                continue
            try:
                self.fs.index_entry(file_id)
                with self.fs.sql.session() as s:
                    s.rebuild_fts()
                log.info("watcher: reindexed %s", file_id)
            except Exception as exc:
                log.warning("watcher: failed to reindex %s: %s", file_id, exc)


def start(fs: "FileStore") -> None:
    """Start the watcher daemon thread. No-op if watchdog is not installed."""
    Observer, FileSystemEventHandler = _try_import_watchdog()
    if Observer is None:
        log.info("watchdog not installed — filesystem watcher disabled")
        return

    handler = _Handler(fs)

    class _WatchdogBridge(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                handler.on_change(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                handler.on_change(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                handler.on_change(event.dest_path)

    bridge = _WatchdogBridge()
    observer = Observer()

    entries_dir = fs.entries_dir
    entries_dir.mkdir(parents=True, exist_ok=True)
    observer.schedule(bridge, str(entries_dir), recursive=False)

    # Watch lists dir too if it exists (created lazily).
    lists_dir = fs.data_dir / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    observer.schedule(bridge, str(lists_dir), recursive=False)

    observer.daemon = True
    observer.start()
    log.info("watcher: watching %s", fs.data_dir)
