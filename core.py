"""
Breadcrumbs core: validation, tag normalisation and the rules around saving and recall.
No SQL here and no web code; this is what the REST API, MCP server and UI all share.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from typing import Callable

import sqlite_vec
from fastembed import TextEmbedding

from storage import SQLiteStore

_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

TYPES = {"recipe", "discovery", "snippet", "dead-end", "howto", "reference", "question", "idea", "link"}
OUTCOMES = {"worked", "partial", "failed", "untested"}
STATUSES = {"proposed", "approved", "rejected", "archived"}
FACETS = {"domain", "tech", "context", "tag"}  # "tag:" is the free-form tier
MAX_TAGS = 8
FUZZY_CUTOFF = 0.88

SEED_TAGS = [
    "domain:cooking", "domain:cooking/baking", "domain:gardening", "domain:home",
    "domain:home/automation", "domain:software", "domain:software/ai-agents",
    "domain:writing", "domain:work", "domain:learning",
    "tech:python", "tech:postgres", "tech:docker", "tech:git", "tech:home-assistant",
    "context:debugging", "context:research",
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
    def __init__(self, store: SQLiteStore, clock: Callable[[], dt.datetime] | None = None,
                 trusted_agents: set[str] | None = None):
        self.store = store
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        # None means "no restriction configured" — everyone is trusted (default/open mode).
        # An explicit set (even empty) means only those agents auto-approve.
        self._trusted = trusted_agents  # None = all trusted
        self._embedder: TextEmbedding | None = None
        store.seed(SEED_TAGS, SEED_ALIASES)

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
        with self.store.session() as s:
            dup = s.find_duplicate(title, t)
            if dup and not force:
                return {"saved": False, "duplicate_of": dup,
                        "hint": "An entry with this title and type exists. Search it, or pass force=true."}
            status = "approved" if (self._trusted is None or agent in self._trusted) else "proposed"
            eid = s.insert_entry(
                title=title.strip(), body=body, type=t, outcome=outcome, summary=summary,
                data=data, state=state, due=due, confidence=confidence, source_url=source_url,
                source_context=source_context, author=agent, created_at=self._ts(), status=status)
            applied, notes = [], []
            for raw in (tags or [])[:MAX_TAGS]:
                tag, note = self._resolve_tag(s, raw, create=True)
                if note:
                    notes.append(note)
                if tag and tag not in applied:
                    applied.append(tag)
                    s.attach_tag(eid, tag)
            text = " ".join(filter(None, [title, summary, body]))
            s.upsert_embedding(eid, self._embed(text))
            return {"saved": True, "id": eid, "tags": applied, "notes": notes, "status": status}

    def update_entry(self, eid: int, title: str | None = None, body: str | None = None,
                     summary: str | None = None, outcome: str | None = None,
                     data: dict | None = None, source_url: str | None = None,
                     source_context: str | None = None, confidence: float | None = None,
                     tags: list[str] | None = None) -> dict:
        with self.store.session() as s:
            existing = s.get_entry(eid)
            if not existing:
                raise ValueError("entry not found")
            fields: dict = {}
            if title is not None:
                fields["title"] = title.strip()
            if body is not None:
                fields["body"] = body
            if summary is not None:
                fields["summary"] = summary
            if outcome is not None:
                if outcome not in OUTCOMES:
                    raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
                fields["outcome"] = outcome
            if data is not None:
                fields["data"] = data
            if source_url is not None:
                fields["source_url"] = source_url
            if source_context is not None:
                fields["source_context"] = source_context
            if confidence is not None:
                fields["confidence"] = min(1.0, max(0.0, confidence))
            if fields:
                s.update_entry(eid, fields)
            # Re-index FTS and re-embed with merged values.
            new_title = fields.get("title", existing["title"])
            new_summary = fields.get("summary", existing["summary"])
            new_body = fields.get("body", existing["body"])
            s.update_fts(eid, new_title, new_summary, new_body)
            text = " ".join(filter(None, [new_title, new_summary, new_body]))
            s.upsert_embedding(eid, self._embed(text))
            # Replace tags if supplied.
            if tags is not None:
                s.c.execute("DELETE FROM entry_tags WHERE entry_id=?", (eid,))
                applied, notes = [], []
                for raw in tags[:MAX_TAGS]:
                    tag, note = self._resolve_tag(s, raw, create=True)
                    if note:
                        notes.append(note)
                    if tag and tag not in applied:
                        applied.append(tag)
                        s.attach_tag(eid, tag)
            else:
                applied = existing["tags"]
                notes = []
            return {"updated": True, "id": eid, "tags": applied, "notes": notes}

    def set_status(self, eid: int, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")
        with self.store.session() as s:
            return s.set_status(eid, status)

    def verify(self, eid: int) -> bool:
        with self.store.session() as s:
            return s.mark_verified(eid, self._ts())

    # --------------------------------------------------------------- reading
    def search(self, query: str | None = None, tag: str | None = None, type: str | None = None,
               since: str | None = None, limit: int = 10, semantic: bool = False,
               offset: int = 0) -> list[dict]:
        with self.store.session() as s:
            full = None
            if tag:
                full, _ = self._resolve_tag(s, tag, create=False)
            t = slug(type) if type else None
            snc = parse_since(since, self._now())
            lim = max(1, min(limit, 50))
            off = max(0, offset)

            if semantic and query:
                # Vector search: get top candidates, then apply tag/type/since filters.
                vec_ids = s.vec_search(self._embed(query), k=(lim + off) * 4)
                if vec_ids:
                    return s.search_by_ids(vec_ids, type=t, since=snc, tag=full, limit=lim, offset=off)
                return []

            match = None
            if query:
                words = re.findall(r"\w+", query)
                if words:
                    match = " OR ".join(f'"{w}"*' for w in words)
            return s.search(match=match, type=t, since=snc, tag=full, limit=lim, offset=off)

    def get_entry(self, eid: int) -> dict | None:
        with self.store.session() as s:
            return s.get_entry(eid)

    def list_tags(self) -> list[dict]:
        with self.store.session() as s:
            return s.list_tags()

    def export(self) -> list[dict]:
        with self.store.session() as s:
            return [s.get_entry(i) for i in s.all_ids()]
