"""
Breadcrumbs core: validation, tag normalisation and the rules around saving and recall.
No SQL here and no web code; this is what the REST API, MCP server and UI all share.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from typing import Callable

from storage import SQLiteStore

TYPES = {"recipe", "discovery", "snippet", "dead-end", "howto", "reference", "question", "idea"}
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
        self._trusted = trusted_agents or set()
        store.seed(SEED_TAGS, SEED_ALIASES)

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
            status = "approved" if agent in self._trusted else "proposed"
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
            return {"saved": True, "id": eid, "tags": applied, "notes": notes, "status": status}

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
               since: str | None = None, limit: int = 10) -> list[dict]:
        match = None
        if query:
            words = re.findall(r"\w+", query)
            if words:
                match = " OR ".join(f'"{w}"*' for w in words)
        with self.store.session() as s:
            full = None
            if tag:
                full, _ = self._resolve_tag(s, tag, create=False)
            return s.search(match=match, type=slug(type) if type else None,
                            since=parse_since(since, self._now()), tag=full,
                            limit=max(1, min(limit, 50)))

    def get_entry(self, eid: int) -> dict | None:
        with self.store.session() as s:
            return s.get_entry(eid)

    def list_tags(self) -> list[dict]:
        with self.store.session() as s:
            return s.list_tags()

    def export(self) -> list[dict]:
        with self.store.session() as s:
            return [s.get_entry(i) for i in s.all_ids()]
