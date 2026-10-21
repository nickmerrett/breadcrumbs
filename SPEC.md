# Breadcrumbs — Build Spec

**Status:** draft for developer handoff
**Owner:** Nick
**One-liner:** A self-hosted, agent-first but human-friendly record store. Markdown files on disk are the source of truth; a derived SQLite index provides fast search; two interfaces (MCP for agents, web UI for humans) sit on one shared core.

---

## 1. Goals

1. **Self-hosted, no vendor risk.** Runs on infrastructure we own. No paid API in the read/write path for simple lists. Data survives deletion of the app.
2. **Human + agent parity.** The same data, reachable as easily and as fast by a non-technical human as by an agent. Neither interface is second-class.
3. **Portable, open, legible.** Plain markdown + frontmatter on disk. `ls`/`grep`/Obsidian work on it without the app. Copy the folder = full export.
4. **Extensible by type.** New record types are added via a registry, not schema migrations or app forks.
5. **Low floor.** A household member can add, complete and find an item on a phone without ever seeing a schema.

## 2. Non-goals

- **Not** a general filesystem index or full-text search engine. Do not rebuild mlocate/Recoll.
- **Not** multi-tenant SaaS. One shared household instance.
- **Not** a secret/credential store (plaintext files — never).
- **Not** a mirror of systems that already own the data (calendar, contacts, email). Store a *pointer + key facts*, never a copy.
- Do **not** build: a markdown parser, OCR, transcription, an auth system, or a search engine. Integrate existing ones.

## 3. Architecture

```
            ┌──────────────┐     ┌──────────────┐
  agents ──▶│  MCP adapter │     │   Web UI     │◀── humans
            └──────┬───────┘     └──────┬───────┘
                   │   same process / same core
                   └─────────┬──────────┘
                        ┌────▼─────┐
                        │   CORE   │  ← the ONLY place logic lives
                        └────┬─────┘
              ┌──────────────┼──────────────┐
        ┌─────▼─────┐   ┌────▼─────┐   ┌────▼─────┐
        │ entries/  │   │ lists/   │   │ blobs/   │   ← truth (files)
        └─────┬─────┘   └────┬─────┘   └──────────┘
              └──────┬───────┘
                ┌────▼─────┐
                │index.sqlite│  ← DERIVED, disposable, rebuildable
                └──────────┘
```

**Rule:** the UI and the MCP are *thin projections* of the core. Neither owns logic. If they both call `core.*`, they cannot drift, and parity is structural rather than a feature.

## 4. Storage layout

Root is configurable (`$BREADCRUMBS_DIR`, default `./data`).

```
data/
  entries/<ulid>.md          # one file per record
  lists/<slug>.md            # one file per list (a record with type: list)
  blobs/<aa>/<bb>/<sha256>   # binary payloads, content-addressed
  blobs/_derived/            # thumbnails, transcripts, OCR, embeddings (regenerable)
  index.sqlite               # derived; gitignored; delete anytime
  .git/                      # optional: history/undo/withdraw
```

## 5. Record format (entry)

Every record is a markdown file: YAML frontmatter (metadata) + markdown body (content).

```markdown
---
id: 01J8Z6Q2W3M4N5P6Q7R8S9T0V1
type: reference
title: Mutti Passata 680ml
tags: [tag:shopping, domain:grocery]
status: open            # open | done | archived | withdrawn
outcome: untested       # worked | partial | failed | untested
list: null              # set only for type: list records
due: null
created: 2026-10-20T20:55:52Z
updated: 2026-10-20T20:55:52Z
source_url: null
source_context: null
data: { barcode: "80042563" }        # type-specific free-form fields
media: []                            # [{blob, mime, bytes, name, w, h, dur}]
links: []                            # [{rel, entry}] typed edges
---

Free markdown body. A human reads this in Obsidian/grep; an agent parses
the frontmatter. Same bytes.
```

**Reserved frontmatter keys:** `id, type, title, tags, status, outcome, due, created, updated, source_url, source_context, data, media, links`. Any **other** key is allowed and must be preserved verbatim on write.

**`type` is an open string.** v1 ships: `note, reference, list, recipe, image, audio, document, link, task`. Unknown types fall back to a generic renderer.

## 6. List object

A list is a first-class record (`type: list`). **Membership + order live in the list's frontmatter; lifecycle state lives on the member entry.** Do not duplicate state in two places.

```markdown
---
id: 01J8ZB...
type: list
title: Shopping
created: 2026-10-20T09:00:00Z
items:
  - { entry: 01J8Z6Q2..., added: 2026-10-20T20:55:52Z }
  - { entry: 01J8Z7A9..., added: 2026-10-20T09:08:36Z }
---

Optional notes about this list.
```

- Rendered in the UI as a checklist; the checkbox reflects the **entry's** `status`. Toggling writes the entry, not the list.
- **Clear list** = one operation → set `status: done` on all member entries (default), or the hard flavour → empty `items[]` (entries survive as history).
- A human who wants a checklist view is served by the UI, not by hand-editing checkboxes.

**Considered and rejected:** putting membership in a GFM checklist body. Rejected because item state would then exist twice (the `[x]` and the entry's `status`), which reintroduces dual-truth.

## 7. Blobs

- **Content-addressed** by `sha256`, sharded `blobs/<aa>/<bb>/<hash>`.
- **On disk, not in SQLite.** The index stores `{blob, mime, bytes}` only.
- **Never base64 a binary into markdown.**
- Deduping, immutability and integrity come free from the hash.
- **Derived artifacts** (thumbnails, transcripts, OCR text, embeddings) live under `blobs/_derived/`, are regenerable, and are never authoritative.
- Git tracks text (`entries/`, `lists/`); blobs sync via rsync/Syncthing or git-lfs.

## 8. Index (SQLite) — derived

Never authoritative. Rebuildable from files in one command.

```sql
entries(id TEXT PK, type, title, status, outcome, created, updated, due)
entry_tags(entry_id, tag)
entries_fts USING fts5(title, body, tags)          -- keyword search, zero deps
lists(id TEXT PK, title)
list_items(list_id, entry_id, position)
vec_entries(entry_id, embedding)                   -- OPTIONAL: sqlite-vec + local model
```

- `breadcrumbs reindex` → truncate + rebuild from `entries/` + `lists/`. Idempotent.
- **If index and files disagree, files win, always.** No reconciliation logic.

## 9. Write path

```
agent/MCP ─┐
web UI   ─┼─▶ core (validate) → 1. write file (tmp + rename = ATOMIC COMMIT POINT)
sensor   ─┤                   → 2. update index rows (or mark dirty)
human    ─┘                   → 3. optional git commit
```

- **Step 1 is the commit point.** Temp file + `rename()` (atomic on POSIX). Nothing is true until it lands.
- **Step 2 is downstream and safe to fail.** A crash between 1 and 2 leaves files correct and the index stale → reindex fixes it. Never invert the order.
- **Single writer.** All writers go through the core; serialize with an advisory lock (`fcntl`) on the data dir. Nothing writes files directly except the core.
- A **filesystem watcher** (`watchdog`) detects external edits (human in Obsidian) and triggers reindex, so hand-edits appear in the UI.

## 10. Read paths

- **Agent:** MCP → core → SQLite FTS5.
- **Human:** the files directly (any editor), *or* the web UI over the same index.
- **Staleness rule:** files win. On any doubt, reindex.

## 11. Interfaces

### 11.1 HTTP API
Extend the existing FastAPI service (it already has `/api/entries` POST, `/api/search` GET, `/api/entries/{id}` GET/PATCH, `/api/entries/{id}/status` POST, `/api/tags` GET, `/api/export` GET). Add:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/entries` | create (keep `force` duplicate-detection) |
| GET | `/api/entries/{id}` | full record **including `data`/`media`/`links`** |
| PATCH | `/api/entries/{id}` | patch any field incl. `tags`, `status`, `due`, `data` |
| POST | `/api/entries/{id}/status` | `approved\|archived\|done\|proposed\|rejected` |
| POST | `/api/entries/{id}/verify` | exists (mark re-checked) |
| GET | `/api/search` | `q, tag, type, since, limit, offset, semantic, due_before, status` |
| POST | `/api/lists` | create list |
| POST | `/api/lists/{id}/items` | add member(s) |
| DELETE | `/api/lists/{id}/items/{entry}` | remove member |
| POST | `/api/lists/{id}/clear` | **bulk** complete/empty (the "one way to clear") |
| GET | `/api/tags` · `/api/export` | exist |

**Requirements:** `search` must return `data` (or accept a `fields` param) so barcode/stockcode lookups don't need a per-id GET. Unknown routes/values must return 4xx, never a silent empty 200.

### 11.2 MCP tools
Keep `search`, `get_entry`, `save_entry`, `mark_verified`, `list_tags`. **Add the lifecycle tools the API already supports but MCP hides:**

| Tool | Maps to |
|---|---|
| `update_entry(id, ...)` | PATCH |
| `set_status(id, status)` | POST status (`done` = complete) |
| `remove_from_list(list_id, entry_id)` | DELETE list item |
| `clear_list(list_id)` | POST list clear |
| `set_due(id, date)` | PATCH `due` |

Rule: **any operation in one door must exist in the other.** If the agent can do it and a human can't (or vice versa), that is a bug.

### 11.3 Web UI (thin render layer)
Server-rendered (FastAPI + Jinja), no SPA build step. It is a **projection**: it owns no truth.

Screens: List index · List view (checklist) · Record view · Add · Search.

**Normie rules (hard requirements):**
- No install, no account — a URL; PWA "add to home screen".
- **Never show YAML, keys, or file paths.** Friendly labels only ("Due", "Notes").
- **Tap, don't edit.** Checkboxes to complete; one "add" box to create.
- **Forgiving capture** — store input verbatim; the agent splits/enriches later. Never mangle on input.
- **Voice/Signal input** is a first-class add path.
- **One-tap undo** — with no hard delete, an accidental action needs an obvious revert.
- A hidden **"raw" toggle** for power users (you).

## 12. Concurrency & consistency

- Household has several writers: owner, partner (Signal), scanner, agent. All go through the core.
- Advisory lock serializes writes; atomic rename makes each write crash-safe.
- External edits detected by watcher → reindex. Conflicts are rare at this scale; last-write-wins on `updated`, with git as the backstop.

## 13. Round-trip requirements (CRITICAL — do this first)

The app and humans edit the same files. Therefore:

1. **Preserve unknown frontmatter keys verbatim.**
2. **Never rewrite the body.**
3. **Patch only the changed keys**, using a round-trip-aware parser (`ruamel.yaml` for YAML, or `tomlkit` for TOML). **No regex.**
4. Write-back is tested by: hand-write a file containing an unknown key and custom body → app changes `status` → assert body byte-identical and unknown key intact.

If hand edits are ever eaten, the power-user path dies and the project is a net loss. This test gates M0.

## 14. Extensibility (type registry)

- A `type` → renderer + validator mapping.
- Add a type = add a renderer; no migration, no fork.
- Unknown types → generic entry view.
- Generality lives in the registry, **not in user-facing settings** (normies hate knobs).

## 15. Auth

- v1: single bearer token for API/MCP (already in place); a simple shared passcode/secret link for the UI.
- **No per-user accounts** in v1. Derive `author` from the token/source so "who added what" is at least possible later.
- Document this as a deliberate v1 limitation.

## 16. Error handling

- Validate on write; reject with a clear 4xx and a human-readable message (shown in the UI too).
- **No silent no-ops.** Any unrecognised action/field returns an error, not HTTP 200 with an empty body.

## 17. Acceptance tests / invariants

1. **Round-trip:** unknown frontmatter key + custom body survive a `status` change byte-for-byte.
2. **Durability:** `rm index.sqlite && breadcrumbs reindex` reproduces identical query results.
3. **Disposability:** delete the app entirely; `ls data/entries/` still shows every record.
4. **Parity:** every UI action has an API/MCP equivalent, and vice versa (enforced by a parity matrix test).
5. **Atomicity:** kill the process mid-write; on restart the data dir is consistent (no half-written entry).
6. **Clear list:** `POST /api/lists/{id}/clear` completes all members in one call; the open view is empty; history remains.
7. **External edit:** edit a file by hand; within N seconds it appears in the UI/search.
8. **No leak:** the UI never renders a raw frontmatter key.

## 18. Milestones / build order

- **M0 — Round-trip proof.** One entry, read/write, round-trip test green. *Gate.*
- **M1 — Core + index.** Files ⇄ core ⇄ SQLite FTS5; `reindex`; watcher.
- **M2 — MCP adapter.** Existing + new lifecycle tools.
- **M3 — Thin UI.** List index, list view (checklist), record view, add, search; normie rules.
- **M4 — Lists + blobs.** List object + clear; content-addressed blobs; media render.
- **M5 — Polish.** Undo, PWA, voice add, git integration, optional sqlite-vec semantic search.

## 19. Open decisions

1. **Frontmatter format:** YAML (Obsidian-native, looser) vs TOML (stricter, fewer footguns). Lean: YAML for ecosystem, with strict quoting.
2. **UI verbs on clear:** default "mark all done" vs "empty the list". Lean: mark done (keep history), with an explicit hard-empty.
3. **Identity model:** when to move past single-token to per-user `author`.
4. **git commits:** on every write, or batched (fewer, tidy history)?

## 20. Reference points (design inspiration, not dependencies)

- **GBrain** — provenance + "withdraw not delete", markdown-file-per-thing, capability gradient, one MCP surface. Use the self-hosted repo; never the hosted SaaS.
- **Anytype** — local-first typed objects with relations.
- **Obsidian** — a second client over the same folder (YAML frontmatter, `[[wikilinks]]`, GFM).
