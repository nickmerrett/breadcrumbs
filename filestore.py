"""
File-based entry store (SPEC §4, §5, §9).

Entries live as  data/entries/<id>.md  — YAML frontmatter + markdown body.
SQLite (data/index.sqlite) is a *derived* index: disposable, rebuildable.

Write path (SPEC §9):
  1. Write / patch the .md file  (atomic rename — the commit point).
  2. Update the SQLite index rows (safe to fail; reindex fixes it).

Read path: always SQLite (fast FTS + vector search).
"""
from __future__ import annotations

import datetime as dt
import io
import uuid
from pathlib import Path

from ruamel.yaml import YAML

import mdfile
from storage import SQLiteStore


def _new_id() -> str:
    """Compact uppercase UUID hex — 32 chars, no hyphens."""
    return uuid.uuid4().hex.upper()


def _ts_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _render_new(fm: dict, body: str) -> str:
    """Render a brand-new .md file from a dict + body."""
    buf = io.StringIO()
    _yaml().dump(fm, buf)
    return f"---\n{buf.getvalue()}---\n{body}"


class FileStore:
    """
    Single writer for both the on-disk .md files and the derived SQLite index.
    All mutations go through here so the two stores stay in sync.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.entries_dir = self.data_dir / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.sql = SQLiteStore(str(self.data_dir / "index.sqlite"))

    # ---------------------------------------------------------------- helpers

    def _path(self, file_id: str) -> Path:
        return self.entries_dir / f"{file_id}.md"

    # --------------------------------------------------- write: new entry file

    def write_new(
        self, *,
        type: str, title: str, body: str, tags: list[str], status: str,
        outcome: str | None, due: str | None, source_url: str | None,
        source_context: str | None, summary: str | None, data: dict | None,
        state: str | None, confidence: float, author: str,
    ) -> str:
        """Write a brand-new .md file. Returns the new file_id."""
        file_id = _new_id()
        now = _ts_now()
        fm = {
            "id": file_id,
            "type": type,
            "title": title,
            "tags": tags,
            "status": status,
            "outcome": outcome,
            "due": due,
            "created": now,
            "updated": now,
            "source_url": source_url,
            "source_context": source_context,
            "summary": summary,
            "data": data,
            "state": state,
            "confidence": confidence,
            "author": author,
            "last_verified": None,
        }
        mdfile._atomic_write(self._path(file_id), _render_new(fm, body))
        return file_id

    # ------------------------------------------------ write: patch existing file

    def patch(self, file_id: str, changes: dict) -> None:
        """Patch named frontmatter keys. Always stamps updated."""
        if "updated" not in changes:
            changes = {**changes, "updated": _ts_now()}
        mdfile.patch_entry(self._path(file_id), changes)

    # ---------------------------------------------------- write: SQLite index

    def index_entry(self, file_id: str) -> int:
        """Read the .md file and upsert it into SQLite. Returns the integer row id."""
        path = self._path(file_id)
        fm, body = mdfile.read_entry(path)
        tags = list(fm.get("tags") or [])
        raw_data = fm.get("data")
        data = dict(raw_data) if raw_data else None

        with self.sql.session() as s:
            # Remove any stale row for this file_id first.
            existing = s.c.execute(
                "SELECT id FROM entries WHERE file_id=?", (file_id,)
            ).fetchone()
            if existing:
                row_id = existing["id"]
                s.c.execute("DELETE FROM entry_tags WHERE entry_id=?", (row_id,))
                s.c.execute("DELETE FROM fts WHERE rowid=?", (row_id,))
                s.c.execute("DELETE FROM entries WHERE id=?", (row_id,))

            row_id = s.insert_entry(
                file_id=file_id,
                title=str(fm.get("title", "")),
                body=body,
                type=str(fm.get("type", "note")),
                outcome=fm.get("outcome"),
                summary=fm.get("summary"),
                data=data,
                state=fm.get("state"),
                due=fm.get("due"),
                confidence=float(fm.get("confidence") or 0.5),
                source_url=fm.get("source_url"),
                source_context=fm.get("source_context"),
                author=str(fm.get("author", "unknown")),
                created_at=str(fm.get("created", _ts_now())),
                status=str(fm.get("status", "proposed")),
                last_verified=fm.get("last_verified"),
            )
            for tag in tags:
                s.create_tag(tag)
                s.attach_tag(row_id, tag)
        return row_id

    def update_index_fields(self, file_id: str, sql_id: int, fields: dict,
                            tags: list[str] | None = None) -> None:
        """Fast-path: update specific columns in SQLite without re-reading the file."""
        with self.sql.session() as s:
            if fields:
                s.update_entry(sql_id, fields)
            if tags is not None:
                s.c.execute("DELETE FROM entry_tags WHERE entry_id=?", (sql_id,))
                for tag in tags:
                    s.create_tag(tag)
                    s.attach_tag(sql_id, tag)
            if any(k in fields for k in ("title", "summary", "body")):
                row = s.get_entry(sql_id)
                if row:
                    s.update_fts(sql_id, row["title"], row.get("summary"), row["body"])

    # ------------------------------------------------- read: id translation

    def sql_id_for(self, file_id: str) -> int | None:
        with self.sql.session() as s:
            row = s.c.execute(
                "SELECT id FROM entries WHERE file_id=?", (file_id,)
            ).fetchone()
            return row["id"] if row else None

    def file_id_for(self, sql_id: int) -> str | None:
        with self.sql.session() as s:
            row = s.c.execute(
                "SELECT file_id FROM entries WHERE id=?", (sql_id,)
            ).fetchone()
            return row["file_id"] if row else None

    # ---------------------------------------------------------------- reindex

    def reindex(self) -> int:
        """Rebuild the SQLite index from all .md files on disk. Returns count indexed."""
        with self.sql.session() as s:
            s.c.execute("DELETE FROM entry_tags")
            s.c.execute("DELETE FROM entries")
            s.c.execute("DELETE FROM fts")
            s.c.execute("DELETE FROM entry_vecs")

        count = 0
        for md_path in sorted(self.entries_dir.glob("*.md")):
            file_id = md_path.stem
            try:
                self.index_entry(file_id)
                count += 1
            except Exception as exc:
                print(f"reindex: skipping {md_path.name}: {exc}")

        with self.sql.session() as s:
            s.rebuild_fts()
        return count
