# Breadcrumbs

A personal long-term notebook that AI agents and you write to together. Save ideas, recipes,
discoveries, dead ends, and links — then find them again months or years later by keyword,
meaning, or tag. Entries are stamped with who saved them, fully searchable, and yours to
export at any time.

Three front doors over the same store:


| Door | Path | For |
|---|---|---|
| MCP (streamable HTTP) | `/mcp` | Claude chat, Claude Code, any MCP client |
| REST | `/api/...` (docs at `/docs`) | scripts, n8n, Home Assistant, other agents |
| Browse UI | `/` | you: read, search, edit, archive |

## Run locally

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
BREADCRUMBS_KEYS="me:KEY1,code:KEY2" ./venv/bin/uvicorn main:app --port 8080
```

- Keys are `name:key` pairs, comma separated. Generate one with
  `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
- Leave `BREADCRUMBS_KEYS` unset for open local trials only (no auth at all).
- Browser login: any username, a key as the password.
- Data lives in `breadcrumbs.db` (override with `BREADCRUMBS_DB`), in WAL mode.
  `GET /api/export` dumps everything as JSON.

## Deploy (HTTPS)

Claude chat needs a public HTTPS address.

**OpenShift.** Build and push the image, create a Deployment with `BREADCRUMBS_KEYS` and
`BREADCRUMBS_PUBLIC_URL` set to the Route URL, and mount a PVC at `/data`.

**Docker Compose (local or VPS).** Expose the app behind your own proxy or tunnel:

```bash
cp .env.example .env     # set BREADCRUMBS_KEYS and BREADCRUMBS_PUBLIC_URL
docker compose up -d
```

**Cloudflare Tunnel / other tunnel.** Run uvicorn locally and point the tunnel at port 8080,
then set `BREADCRUMBS_PUBLIC_URL` to the tunnel's `https://` address.

`BREADCRUMBS_PUBLIC_URL` must match the address clients use, because OAuth discovery
advertises it.

## Connect a client

**Claude chat** (OAuth). Add a custom connector pointing at `https://YOUR-DOMAIN/mcp` and leave
any client ID/secret fields blank. Claude registers itself automatically, opens a Breadcrumbs
login page in your browser, and you enter one of your keys to allow it. Entries it saves are
stamped with the connector's name. Exact menu labels change; Anthropic's connector
documentation has the current steps.

**Claude Code** (static key):

```bash
claude mcp add --transport http breadcrumbs https://YOUR-DOMAIN/mcp \
  --header "Authorization: Bearer KEY2"
```

Any other client: send `Authorization: Bearer <key>`, or use its OAuth support.

## MCP tools

`save_entry`, `search`, `get_entry`, `list_tags`, `mark_verified`.

The server instructs agents to search before saving, write self-contained entries that make
sense without the surrounding conversation, and reuse existing tags rather than inventing
near-duplicates.

## Entries

Each entry has a **type**, a freeform **body** (markdown), optional **tags**, **outcome**,
**summary**, **source URL**, and **source context**.

Types: `recipe`, `discovery`, `snippet`, `dead-end`, `howto`, `reference`, `question`,
`idea`, `link`.

Entries are saved as `approved` by default. If you configure `BREADCRUMBS_TRUSTED_AGENTS`,
only agents in that list auto-approve — everything else lands as `proposed` and is flagged
in the UI for your review.

## Search

Two modes, selectable per query:

- **Keyword** (default) — SQLite FTS5 full-text search over title, summary, and body.
- **Semantic** (`semantic=true`) — local embedding model (`BAAI/bge-small-en-v1.5`, ~30 MB,
  runs fully offline) finds entries by meaning rather than exact words. Useful when you
  cannot recall the exact wording of something you saved months ago.

Both modes support filtering by tag, type, and date.

## Tags

- Facets: `domain:`, `tech:`, `context:`, plus a free-form `tag:` tier. Hierarchy uses `/`.
- Searching `domain:cooking` also returns everything under `domain:cooking/...`.
- On write, tags resolve through aliases (`py` → `tech:python`), then near-duplicates map
  to the existing tag (`bakng` → `baking`), then genuinely new tags are created.

## Security

The server holds your data in plaintext so that search can work. Protect it accordingly:

- Always run behind HTTPS in production.
- Keep keys long and random.
- Anyone can *register* an OAuth client (that is how Claude connects), but nothing is granted
  until someone enters a valid key on the login page. Each login request allows 5 attempts.
- Access tokens last 1 hour; refresh tokens 30 days and rotate on use.
- The server is open source — you can audit exactly what it does with your data.
- **Self-hosting is the strongest privacy guarantee.** If you run your own instance on an
  encrypted volume, no third party has access to your data.
- `GET /api/export` returns everything as JSON. Your data is never a trap.

## Layout

| File | Role |
|---|---|
| `storage.py` | All SQL, vector store, schema. Swap to change the backing store. |
| `core.py` | Validation, tag normalisation, embedding, saving and recall rules. No SQL, no web. |
| `oauth.py` | OAuth provider: clients, codes, tokens, login check. |
| `app.py` | `create_app()`: REST, MCP, auth, UI routes. |
| `web.py` | HTML for the browse UI and login page. |
| `main.py` | Entry point for uvicorn. |

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```

## Backlog

- Weekly review / gardener (merge duplicates, prune stale entries, surface unverified)
- Per-user accounts and hosted offering
- Image attachments
- Postgres backend
- Audit log (who accessed what, when)
