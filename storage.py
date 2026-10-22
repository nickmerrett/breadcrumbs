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

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, file_id TEXT UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
  type TEXT NOT NULL, outcome TEXT, summary TEXT, data TEXT, state TEXT, due TEXT,
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
CREATE INDEX IF NOT EXISTS entries_file_id ON entries(file_id);
CREATE INDEX IF NOT EXISTS entry_tags_tag ON entry_tags(tag_id);
CREATE TABLE IF NOT EXISTS lists(
  id INTEGER PRIMARY KEY, file_id TEXT UNIQUE, title TEXT NOT NULL,
  author TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS list_items(
  list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE,
  entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  added_at TEXT NOT NULL,
  PRIMARY KEY(list_id, entry_id));
CREATE INDEX IF NOT EXISTS list_items_list ON list_items(list_id, position);
CREATE TABLE IF NOT EXISTS oauth_items(
  kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL,
  PRIMARY KEY(kind, key));
"""

# Applied once when an existing DB lacks the file_id column (upgrading from SQLite-only).
MIGRATE = "ALTER TABLE entries ADD COLUMN file_id TEXT"

# Separate from SCHEMA because vec0 is a virtual table that requires the extension loaded first.
VEC_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS entry_vecs USING vec0(entry_id INTEGER PRIMARY KEY, embedding FLOAT[384])"

EMBEDDING_DIM = 384

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
                     author: str, created_at: str, status: str = "proposed",
                     file_id: str | None = None, last_verified: str | None = None) -> int:
        cur = self.c.execute(
            "INSERT INTO entries(file_id,title,body,type,outcome,summary,data,state,due,confidence,"
            "source_url,source_context,author,status,created_at,last_verified) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, title, body, type, outcome, summary, json.dumps(data) if data else None,
             state, due, confidence, source_url, source_context, author, status, created_at,
             last_verified))
        eid = cur.lastrowid
        self.c.execute("INSERT INTO fts(rowid,title,summary,body) VALUES(?,?,?,?)",
                       (eid, title, summary or "", body))
        return eid

    def upsert_embedding(self, entry_id: int, vec_bytes: bytes) -> None:
        # vec0 virtual tables don't support INSERT OR REPLACE; delete first.
        self.c.execute("DELETE FROM entry_vecs WHERE entry_id=?", (entry_id,))
        self.c.execute(
            "INSERT INTO entry_vecs(entry_id, embedding) VALUES(?, ?)",
            (entry_id, vec_bytes))

    def vec_search(self, vec_bytes: bytes, k: int) -> list[int]:
        """Return up to k entry_ids ordered by cosine-like distance (L2 on normalised vecs)."""
        rows = self.c.execute(
            "SELECT entry_id FROM entry_vecs WHERE embedding MATCH ? AND k=? ORDER BY distance",
            (vec_bytes, k)).fetchall()
        return [r["entry_id"] for r in rows]

    def get_entry(self, eid: int) -> dict | None:
        r = self.c.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        d["tags"] = self.tags_of([eid])[eid]
        return d

    def update_entry(self, eid: int, fields: dict) -> bool:
        if not fields:
            return False
        set_clause = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [eid]
        return self.c.execute(
            f"UPDATE entries SET {set_clause} WHERE id=?", params).rowcount > 0

    def update_fts(self, eid: int, title: str, summary: str | None, body: str) -> None:
        # FTS5 standalone tables don't support per-row delete via parameterised SQL.
        # Delete the stale row and insert the new content; the caller must call
        # rebuild_fts() in a separate session after committing, to flush stale index entries.
        self.c.execute("DELETE FROM fts WHERE rowid=?", (eid,))
        self.c.execute("INSERT INTO fts(rowid,title,summary,body) VALUES(?,?,?,?)",
                       (eid, title, summary or "", body))

    def rebuild_fts(self) -> None:
        self.c.execute("INSERT INTO fts(fts) VALUES('rebuild')")

    def search(self, *, match: str | None, type: str | None, since: str | None,
               tag: str | None, limit: int, offset: int = 0, due_before: str | None = None,
               status: str | None = None) -> list[dict]:
        # Default: hide rejected and archived. 'done' is shown (recipes, discoveries stay useful).
        default_exclude = "e.status NOT IN ('rejected','archived')"
        sql, where, params = f"SELECT {_BRIEF} FROM entries e", [default_exclude], []
        if status:
            where.append("e.status=?")
            params.append(status)
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
        if due_before:
            where.append("e.due IS NOT NULL AND e.due<=?")
            params.append(due_before)
        if tag:  # a parent tag also matches everything beneath it
            where.append("EXISTS(SELECT 1 FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                         "WHERE et.entry_id=e.id AND (t.name=? OR t.name LIKE ?))")
            params += [tag, tag + "/%"]
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY bm25(fts)" if match else " ORDER BY e.created_at DESC, e.id DESC"
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = [dict(r) for r in self.c.execute(sql, params)]
        tg = self.tags_of([r["id"] for r in rows])
        for r in rows:
            r["tags"] = tg[r["id"]]
        return rows

    def search_by_ids(self, ids: list[int], *, type: str | None, since: str | None,
                      tag: str | None, limit: int, offset: int = 0) -> list[dict]:
        """Filter a pre-ranked list of ids (from vec_search) and return brief rows."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        where = [f"e.id IN ({placeholders})", "e.status NOT IN ('rejected','archived')"]
        params: list = list(ids)
        if type:
            where.append("e.type=?")
            params.append(type)
        if since:
            where.append("e.created_at>=?")
            params.append(since)
        if tag:
            where.append("EXISTS(SELECT 1 FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                         "WHERE et.entry_id=e.id AND (t.name=? OR t.name LIKE ?))")
            params += [tag, tag + "/%"]
        # Preserve vector ranking order via CASE expression.
        order = " ".join(f"WHEN e.id={eid} THEN {i}" for i, eid in enumerate(ids))
        sql = (f"SELECT {_BRIEF} FROM entries e WHERE " + " AND ".join(where) +
               f" ORDER BY CASE {order} ELSE {len(ids)} END LIMIT ? OFFSET ?")
        params += [limit, offset]
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


    # ------------------------------------------------------------------ lists

    def insert_list(self, *, file_id: str, title: str, author: str, created_at: str) -> int:
        cur = self.c.execute(
            "INSERT INTO lists(file_id, title, author, created_at) VALUES(?,?,?,?)",
            (file_id, title, author, created_at))
        return cur.lastrowid

    def get_list(self, list_id: int) -> dict | None:
        r = self.c.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()
        return dict(r) if r else None

    def get_list_by_file_id(self, file_id: str) -> dict | None:
        r = self.c.execute("SELECT * FROM lists WHERE file_id=?", (file_id,)).fetchone()
        return dict(r) if r else None

    def all_lists(self) -> list[dict]:
        return [dict(r) for r in self.c.execute("SELECT * FROM lists ORDER BY created_at DESC")]

    def list_items(self, list_id: int) -> list[dict]:
        """Return full entry rows for a list, ordered by position."""
        rows = [dict(r) for r in self.c.execute(
            f"SELECT {_BRIEF}, e.file_id, li.position, li.added_at FROM entries e "
            "JOIN list_items li ON li.entry_id=e.id "
            "WHERE li.list_id=? ORDER BY li.position, li.added_at", (list_id,))]
        tg = self.tags_of([r["id"] for r in rows])
        for r in rows:
            r["tags"] = tg[r["id"]]
        return rows

    def add_list_item(self, list_id: int, entry_id: int, added_at: str) -> None:
        max_pos = self.c.execute(
            "SELECT COALESCE(MAX(position),0) FROM list_items WHERE list_id=?",
            (list_id,)).fetchone()[0]
        self.c.execute(
            "INSERT OR IGNORE INTO list_items(list_id,entry_id,position,added_at) VALUES(?,?,?,?)",
            (list_id, entry_id, max_pos + 1, added_at))

    def remove_list_item(self, list_id: int, entry_id: int) -> bool:
        return self.c.execute(
            "DELETE FROM list_items WHERE list_id=? AND entry_id=?",
            (list_id, entry_id)).rowcount > 0

    def clear_list(self, list_id: int) -> list[int]:
        """Mark all open items done. Returns the entry_ids that were changed."""
        ids = [r["entry_id"] for r in self.c.execute(
            "SELECT li.entry_id FROM list_items li "
            "JOIN entries e ON e.id=li.entry_id "
            "WHERE li.list_id=? AND e.status NOT IN ('done','archived','rejected')",
            (list_id,))]
        if ids:
            self.c.execute(
                f"UPDATE entries SET status='done' WHERE id IN ({','.join('?'*len(ids))})", ids)
        return ids

    def delete_list(self, list_id: int) -> bool:
        return self.c.execute("DELETE FROM lists WHERE id=?", (list_id,)).rowcount > 0


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
            s.c.execute(VEC_SCHEMA)
            # Migrate existing DBs that pre-date the file_id column.
            cols = {r[1] for r in s.c.execute("PRAGMA table_info(entries)")}
            if "file_id" not in cols:
                s.c.execute(MIGRATE)

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
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
