"""
SQLite storage for Breadcrumbs.

All SQL lives in this file. The rest of the app talks to `SQLiteStore.session()` and the
methods on `Session`, so swapping the backing store (per-user libSQL, Postgres) means
writing one new module with the same methods.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, type TEXT NOT NULL,
  outcome TEXT, summary TEXT, data TEXT, state TEXT, due TEXT,
  confidence REAL DEFAULT 0.5, source_url TEXT, source_context TEXT,
  author TEXT NOT NULL, status TEXT DEFAULT 'proposed',
  created_at TEXT NOT NULL, last_verified TEXT);
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, tag TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entry_tags(
  entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
  tag_id INTEGER REFERENCES tags(id), PRIMARY KEY(entry_id, tag_id));
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, summary, body);
CREATE INDEX IF NOT EXISTS entries_created ON entries(created_at);
CREATE INDEX IF NOT EXISTS entries_type ON entries(type, status);
CREATE INDEX IF NOT EXISTS entry_tags_tag ON entry_tags(tag_id);
CREATE TABLE IF NOT EXISTS oauth_items(
  kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL,
  PRIMARY KEY(kind, key));
"""

_BRIEF = "e.id,e.title,e.type,e.outcome,e.summary,e.author,e.status,e.created_at"


class Session:
    """One transaction. Obtain via SQLiteStore.session()."""

    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # ------------------------------------------------------------------ tags
    def alias(self, alias: str) -> str | None:
        r = self.c.execute("SELECT tag FROM aliases WHERE alias=?", (alias,)).fetchone()
        return r["tag"] if r else None

    def tag_exists(self, name: str) -> bool:
        return self.c.execute("SELECT 1 FROM tags WHERE name=?", (name,)).fetchone() is not None

    def tag_names(self, facet: str) -> list[str]:
        return [r["name"] for r in self.c.execute(
            "SELECT name FROM tags WHERE name LIKE ?", (facet + ":%",))]

    def create_tag(self, name: str) -> None:
        self.c.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))

    def attach_tag(self, entry_id: int, tag: str) -> None:
        self.c.execute("INSERT OR IGNORE INTO entry_tags SELECT ?, id FROM tags WHERE name=?",
                       (entry_id, tag))

    def tags_of(self, ids: list[int]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {i: [] for i in ids}
        if ids:
            q = ",".join("?" * len(ids))
            for r in self.c.execute(
                f"SELECT et.entry_id, t.name FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                f"WHERE et.entry_id IN ({q}) ORDER BY t.name", ids):
                out[r["entry_id"]].append(r["name"])
        return out

    def list_tags(self) -> list[dict]:
        return [dict(r) for r in self.c.execute(
            "SELECT t.name, COUNT(et.entry_id) AS uses FROM tags t "
            "LEFT JOIN entry_tags et ON et.tag_id=t.id GROUP BY t.id ORDER BY t.name")]

    # --------------------------------------------------------------- entries
    def find_duplicate(self, title: str, type: str) -> int | None:
        r = self.c.execute(
            "SELECT id FROM entries WHERE lower(title)=lower(?) AND type=? AND status!='rejected'",
            (title, type)).fetchone()
        return r["id"] if r else None

    def insert_entry(self, *, title: str, body: str, type: str, outcome: str | None,
                     summary: str | None, data: dict | None, state: str | None, due: str | None,
                     confidence: float, source_url: str | None, source_context: str | None,
                     author: str, created_at: str, status: str = "proposed") -> int:
        cur = self.c.execute(
            "INSERT INTO entries(title,body,type,outcome,summary,data,state,due,confidence,"
            "source_url,source_context,author,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, body, type, outcome, summary, json.dumps(data) if data else None, state, due,
             confidence, source_url, source_context, author, status, created_at))
        eid = cur.lastrowid
        self.c.execute("INSERT INTO fts(rowid,title,summary,body) VALUES(?,?,?,?)",
                       (eid, title, summary or "", body))
        return eid

    def get_entry(self, eid: int) -> dict | None:
        r = self.c.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        d["tags"] = self.tags_of([eid])[eid]
        return d

    def search(self, *, match: str | None, type: str | None, since: str | None,
               tag: str | None, limit: int) -> list[dict]:
        sql, where, params = f"SELECT {_BRIEF} FROM entries e", ["e.status!='rejected'"], []
        if match:
            sql += " JOIN fts ON fts.rowid=e.id"
            where.append("fts MATCH ?")
            params.append(match)
        if type:
            where.append("e.type=?")
            params.append(type)
        if since:
            where.append("e.created_at>=?")
            params.append(since)
        if tag:  # a parent tag also matches everything beneath it
            where.append("EXISTS(SELECT 1 FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                         "WHERE et.entry_id=e.id AND (t.name=? OR t.name LIKE ?))")
            params += [tag, tag + "/%"]
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY bm25(fts)" if match else " ORDER BY e.created_at DESC, e.id DESC"
        sql += " LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.c.execute(sql, params)]
        tg = self.tags_of([r["id"] for r in rows])
        for r in rows:
            r["tags"] = tg[r["id"]]
        return rows

    def set_status(self, eid: int, status: str) -> bool:
        return self.c.execute("UPDATE entries SET status=? WHERE id=?", (status, eid)).rowcount > 0

    def mark_verified(self, eid: int, when: str) -> bool:
        return self.c.execute("UPDATE entries SET last_verified=?, status='approved' WHERE id=?",
                              (when, eid)).rowcount > 0

    def all_ids(self) -> list[int]:
        return [r["id"] for r in self.c.execute("SELECT id FROM entries ORDER BY id")]

    # ----------------------------------------------------------------- oauth
    # Clients, pending requests, codes and tokens share one small key/value table.
    def oauth_put(self, kind: str, key: str, payload: str, expires_at: float | None) -> None:
        self.c.execute("INSERT OR REPLACE INTO oauth_items VALUES(?,?,?,?)",
                       (kind, key, payload, expires_at))

    def oauth_get(self, kind: str, key: str, now: float) -> str | None:
        r = self.c.execute("SELECT payload, expires_at FROM oauth_items WHERE kind=? AND key=?",
                           (kind, key)).fetchone()
        if not r or (r["expires_at"] is not None and r["expires_at"] < now):
            return None
        return r["payload"]

    def oauth_delete(self, kind: str, key: str) -> None:
        self.c.execute("DELETE FROM oauth_items WHERE kind=? AND key=?", (kind, key))

    def oauth_purge(self, now: float) -> None:
        self.c.execute("DELETE FROM oauth_items WHERE expires_at IS NOT NULL AND expires_at<?", (now,))


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.session() as s:
            s.c.executescript(SCHEMA)

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        # WAL lets readers and a writer work at once (several clients, one human browsing).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield Session(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def seed(self, tags: list[str], aliases: dict[str, str]) -> None:
        with self.session() as s:
            for t in tags:
                s.create_tag(t)
            for a, t in aliases.items():
                s.c.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (a, t))
