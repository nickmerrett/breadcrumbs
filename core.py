"""
Breadcrumbs core: validation, tag normalisation and the rules around saving and recall.
No SQL here and no web code; this is what the REST API, MCP server and UI all share.

Storage is file-first (SPEC §9):
  writes → .md file (atomic commit point) → SQLite index update
  reads  → SQLite (fast FTS + vector search)
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from pathlib import Path
from typing import Callable

import sqlite_vec
from fastembed import TextEmbedding

from filestore import FileStore

_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

TYPES = {"recipe", "discovery", "snippet", "dead-end", "howto", "reference", "question", "idea", "link", "task"}
OUTCOMES = {"worked", "partial", "failed", "untested"}
STATUSES = {"proposed", "approved", "rejected", "archived", "done"}
FACETS = {"domain", "tech", "context", "tag"}  # "tag:" is the free-form tier
MAX_TAGS = 8
FUZZY_CUTOFF = 0.88

SEED_TAGS = [
    "domain:cooking", "domain:cooking/baking", "domain:gardening", "domain:home",
    "domain:home/automation", "domain:software", "domain:software/ai-agents",
    "domain:writing", "domain:work", "domain:learning",
    "domain:shopping", "domain:gifts",
    "tech:python", "tech:postgres", "tech:docker", "tech:git", "tech:home-assistant",
    "context:debugging", "context:research", "context:christmas-2025",
]
SEED_ALIASES = {
    "py": "tech:python", "python3": "tech:python", "postgresql": "tech:postgres",
    "pg": "tech:postgres", "hass": "tech:home-assistant", "food": "domain:cooking",
    "recipes": "domain:cooking", "baking": "domain:cooking/baking",
    "smart-home": "domain:home/automation", "agents": "domain:software/ai-agents",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_since(s: str | None, now: dt.datetime) -> str | None:
    """'7d' means seven days ago; anything else is passed through as an ISO date."""
    if not s:
        return None
    m = re.fullmatch(r"(\d+)d", s.strip())
    if m:
        return (now - dt.timedelta(days=int(m[1]))).isoformat()
    return s.strip()


class Notebook:
    def __init__(self, fs: FileStore, clock: Callable[[], dt.datetime] | None = None,
                 trusted_agents: set[str] | None = None):
        self.fs = fs
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        # None = no restriction — everyone is trusted (open/dev mode).
        self._trusted = trusted_agents
        self._embedder: TextEmbedding | None = None
        fs.sql.seed(SEED_TAGS, SEED_ALIASES)
        self._warmup()

    def _warmup(self) -> None:
        self._embed("warmup")

    def _embed(self, text: str) -> bytes:
        if self._embedder is None:
            self._embedder = TextEmbedding(_EMBED_MODEL)
        vec = next(self._embedder.embed([text]))
        return sqlite_vec.serialize_float32(vec)

    def _now(self) -> dt.datetime:
        return self._clock()

    def _ts(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ tags

    def _split(self, raw: str) -> tuple[str, str]:
        facet, sep, rest = raw.partition(":")
        if not sep or slug(facet) not in FACETS:
            facet, rest = "tag", raw
        path = "/".join(p for p in (slug(x) for x in rest.split("/")) if p)
        return slug(facet), path

    def _resolve_tag(self, s, raw: str, create: bool) -> tuple[str | None, str | None]:
        """Map an agent-supplied tag onto the controlled vocabulary: (tag, note)."""
        facet, path = self._split(raw)
        if not path:
            return None, f"ignored empty tag '{raw}'"
        full = f"{facet}:{path}"
        if hit := s.alias(path.split("/")[-1]):
            return hit, None
        if s.tag_exists(full):
            return full, None
        near = difflib.get_close_matches(full, s.tag_names(facet), n=1, cutoff=FUZZY_CUTOFF)
        if near:
            return near[0], f"'{raw}' mapped to existing tag {near[0]}"
        if create:
            s.create_tag(full)
            return full, f"new tag created: {full}"
        return full, None

    def _resolve_tags(self, s, raw_tags: list[str]) -> tuple[list[str], list[str]]:
        applied, notes = [], []
        for raw in raw_tags[:MAX_TAGS]:
            tag, note = self._resolve_tag(s, raw, create=True)
            if note:
                notes.append(note)
            if tag and tag not in applied:
                applied.append(tag)
        return applied, notes

    # --------------------------------------------------------------- writing

    def save_entry(self, agent: str, title: str, body: str, type: str,
                   tags: list[str] | None = None, outcome: str | None = None,
                   summary: str | None = None, data: dict | None = None,
                   source_url: str | None = None, source_context: str | None = None,
                   confidence: float = 0.5, state: str | None = None, due: str | None = None,
                   force: bool = False) -> dict:
        t = slug(type)
        if t not in TYPES:
            raise ValueError(f"type must be one of {sorted(TYPES)}")
        if outcome and outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
        if not title.strip() or not body.strip():
            raise ValueError("title and body are required")
        confidence = min(1.0, max(0.0, confidence))

        # Resolve tags and check for duplicates via SQLite (fast).
        with self.fs.sql.session() as s:
            dup = s.find_duplicate(title, t)
            if dup and not force:
                return {"saved": False, "duplicate_of": dup,
                        "hint": "An entry with this title and type exists. Search it, or pass force=true."}
            applied, notes = self._resolve_tags(s, tags or [])

        status = "approved" if (self._trusted is None or agent in self._trusted) else "proposed"

        # Step 1: write .md file (the atomic commit point).
        file_id = self.fs.write_new(
            type=t, title=title.strip(), body=body, tags=applied, status=status,
            outcome=outcome, due=due, source_url=source_url, source_context=source_context,
            summary=summary, data=data, state=state, confidence=confidence, author=agent,
        )

        # Step 2: index into SQLite (safe to fail — reindex will fix it).
        sql_id = self.fs.index_entry(file_id)

        # Step 3: store embedding.
        vec_bytes = self._embed(" ".join(filter(None, [title.strip(), summary, body])))
        with self.fs.sql.session() as s:
            s.upsert_embedding(sql_id, vec_bytes)

        return {"saved": True, "id": sql_id, "file_id": file_id,
                "tags": applied, "notes": notes, "status": status}

    def update_entry(self, eid: int, title: str | None = None, body: str | None = None,
                     summary: str | None = None, outcome: str | None = None,
                     data: dict | None = None, source_url: str | None = None,
                     source_context: str | None = None, confidence: float | None = None,
                     tags: list[str] | None = None, state: str | None = None,
                     due: str | None = None) -> dict:
        # Look up the file_id for this SQL row.
        file_id = self.fs.file_id_for(eid)
        if not file_id:
            raise ValueError("entry not found")

        with self.fs.sql.session() as s:
            existing = s.get_entry(eid)
        if not existing:
            raise ValueError("entry not found")

        # Build frontmatter patch (only what changed).
        fm_patch: dict = {}
        sql_fields: dict = {}

        def _set(fm_key: str, sql_key: str, val):
            fm_patch[fm_key] = val
            sql_fields[sql_key] = val

        if title is not None:
            _set("title", "title", title.strip())
        if body is not None:
            _set("body", "body", body)
        if summary is not None:
            _set("summary", "summary", summary)
        if outcome is not None:
            if outcome not in OUTCOMES:
                raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
            _set("outcome", "outcome", outcome)
        if data is not None:
            _set("data", "data", data)
        if source_url is not None:
            _set("source_url", "source_url", source_url)
        if source_context is not None:
            _set("source_context", "source_context", source_context)
        if confidence is not None:
            c = min(1.0, max(0.0, confidence))
            _set("confidence", "confidence", c)
        if state is not None:
            _set("state", "state", state)
        if due is not None:
            _set("due", "due", due)

        applied, notes = [], []
        if tags is not None:
            with self.fs.sql.session() as s:
                applied, notes = self._resolve_tags(s, tags)
        else:
            applied = existing["tags"]

        # Step 1: patch the .md file first.
        if fm_patch:
            self.fs.patch(file_id, fm_patch)

        # Step 2: update SQLite index.
        self.fs.update_index_fields(
            file_id, eid, sql_fields,
            tags=applied if tags is not None else None,
        )

        # Step 3: re-embed if text changed.
        if any(k in fm_patch for k in ("title", "summary", "body")):
            new_title = fm_patch.get("title", existing["title"])
            new_summary = fm_patch.get("summary", existing.get("summary"))
            new_body = fm_patch.get("body", existing["body"])
            vec_bytes = self._embed(" ".join(filter(None, [new_title, new_summary, new_body])))
            with self.fs.sql.session() as s:
                s.upsert_embedding(eid, vec_bytes)
            with self.fs.sql.session() as s:
                s.rebuild_fts()

        return {"updated": True, "id": eid, "file_id": file_id,
                "tags": applied, "notes": notes}

    def set_status(self, eid: int, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")
        file_id = self.fs.file_id_for(eid)
        if not file_id:
            return False
        # Step 1: patch the file.
        self.fs.patch(file_id, {"status": status})
        # Step 2: update the index.
        with self.fs.sql.session() as s:
            return s.set_status(eid, status)

    def verify(self, eid: int) -> bool:
        file_id = self.fs.file_id_for(eid)
        if not file_id:
            return False
        now = self._ts()
        # Step 1: patch the file.
        self.fs.patch(file_id, {"status": "approved", "last_verified": now})
        # Step 2: update the index.
        with self.fs.sql.session() as s:
            return s.mark_verified(eid, now)

    # --------------------------------------------------------------- reading

    def search(self, query: str | None = None, tag: str | None = None, type: str | None = None,
               since: str | None = None, limit: int = 10, semantic: bool = False,
               offset: int = 0, due_before: str | None = None,
               status: str | None = None) -> list[dict]:
        with self.fs.sql.session() as s:
            full = None
            if tag:
                full, _ = self._resolve_tag(s, tag, create=False)
            t = slug(type) if type else None
            snc = parse_since(since, self._now())
            lim = max(1, min(limit, 50))
            off = max(0, offset)
            due = due_before if due_before != "today" else self._now().date().isoformat()

            if semantic and query:
                vec_ids = s.vec_search(self._embed(query), k=(lim + off) * 4)
                if vec_ids:
                    return s.search_by_ids(vec_ids, type=t, since=snc, tag=full, limit=lim, offset=off)
                return []

            match = None
            if query:
                words = re.findall(r"\w+", query)
                if words:
                    match = " OR ".join(f'"{w}"*' for w in words)
            return s.search(match=match, type=t, since=snc, tag=full, limit=lim, offset=off,
                            due_before=due, status=status)

    def get_entry(self, eid: int) -> dict | None:
        with self.fs.sql.session() as s:
            return s.get_entry(eid)

    def list_tags(self) -> list[dict]:
        with self.fs.sql.session() as s:
            return s.list_tags()

    def export(self) -> list[dict]:
        with self.fs.sql.session() as s:
            return [s.get_entry(i) for i in s.all_ids()]

    def reindex(self) -> int:
        """Rebuild the SQLite index from .md files on disk."""
        return self.fs.reindex()

    # --------------------------------------------------------------- lists

    def create_list(self, agent: str, title: str) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        file_id = self.fs._new_list_id()
        now = self._ts()
        # Write the .md file first.
        self.fs.write_new_list(file_id=file_id, title=title.strip(), author=agent, created=now)
        # Index into SQLite.
        with self.fs.sql.session() as s:
            list_id = s.insert_list(file_id=file_id, title=title.strip(),
                                    author=agent, created_at=now)
        return {"id": list_id, "file_id": file_id, "title": title.strip()}

    def get_list(self, list_id: int) -> dict | None:
        with self.fs.sql.session() as s:
            lst = s.get_list(list_id)
            if not lst:
                return None
            lst["items"] = s.list_items(list_id)
            return lst

    def all_lists(self) -> list[dict]:
        with self.fs.sql.session() as s:
            return s.all_lists()

    def add_to_list(self, agent: str, list_id: int, entry_id: int) -> dict:
        with self.fs.sql.session() as s:
            if not s.get_list(list_id):
                raise ValueError("list not found")
            if not s.get_entry(entry_id):
                raise ValueError("entry not found")
            s.add_list_item(list_id, entry_id, self._ts())
            # Patch the list .md file to record membership.
            lst = s.get_list(list_id)
        if lst and lst.get("file_id"):
            self.fs.patch_list(lst["file_id"], list_id)
        return {"ok": True, "list_id": list_id, "entry_id": entry_id}

    def remove_from_list(self, list_id: int, entry_id: int) -> dict:
        with self.fs.sql.session() as s:
            ok = s.remove_list_item(list_id, entry_id)
            lst = s.get_list(list_id)
        if ok and lst and lst.get("file_id"):
            self.fs.patch_list(lst["file_id"], list_id)
        return {"ok": ok, "list_id": list_id, "entry_id": entry_id}

    def clear_list(self, list_id: int) -> dict:
        """Mark all open items in the list as done."""
        with self.fs.sql.session() as s:
            changed_ids = s.clear_list(list_id)
            lst = s.get_list(list_id)
        # Patch the status in each entry's .md file.
        for eid in changed_ids:
            file_id = self.fs.file_id_for(eid)
            if file_id:
                self.fs.patch(file_id, {"status": "done"})
        return {"ok": True, "list_id": list_id, "cleared": len(changed_ids)}

