# Breadcrumbs

A central long-term notebook for AI agents and the person using them. Anything a connected
client saves (a recipe, a discovery, a dead end) can be found later from any other client, or
browsed by you. One SQLite file, three front doors over the same logic:


| Door | Path | For |
|---|---|---|
| MCP (streamable HTTP) | `/mcp` | Claude chat, Claude Code and other code assistants, any MCP client |
| REST | `/api/...` (docs at `/docs`) | scripts, n8n, Home Assistant |
| Browse UI | `/` | you: search, read, approve or reject |

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

`save_entry`, `search`, `get_entry`, `list_tags`, `mark_verified`. The server sends instructions
telling models to search first, save only confirmed things, write self-contained entries, and
reuse existing tags.

## Tags

- Facets: `domain:`, `tech:`, `context:`, plus a free-form `tag:` tier. Hierarchy uses `/`.
- Searching `domain:cooking` also returns everything under `domain:cooking/...`.
- On write, tags resolve through aliases (`py` becomes `tech:python`), then near-duplicates map
  to the existing tag (`bakng` becomes `baking`), then genuinely new tags are created.
- Entry types: recipe, discovery, snippet, dead-end, howto, reference, question, idea.
- New entries are `proposed`: searchable straight away and flagged in the UI until you approve
  (which also marks them verified) or reject.

## Security notes

- Anyone can *register* an OAuth client (that is how Claude connects), but nothing is granted
  until someone enters a valid key on the login page. Each login request allows 5 attempts.
- Keep keys long and random, and always run behind HTTPS.
- Access tokens last 1 hour; refresh tokens 30 days and rotate on use. `/revoke` is enabled
  (the SDK requires an empty `client_secret` form field even for public clients).
- Only keys can approve a connection. There is no per-user account system yet.
- OAuth state lives in the same SQLite file, so back that file up like the rest.

## Layout

| File | Role |
|---|---|
| `storage.py` | All SQL. Swap this module to change the backing store. |
| `core.py` | Validation, tag normalisation, saving and recall rules. No SQL, no web. |
| `oauth.py` | OAuth provider: clients, codes, tokens, login check. |
| `app.py` | `create_app()`: REST, MCP, auth, UI routes. |
| `web.py` | HTML for the browse UI and login page. |
| `main.py` | Entry point for uvicorn. |

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```

## Not built yet

Semantic (embedding) search, the weekly review screen and gardener (merge, prune, stale flags),
per-user accounts and hosting, Postgres.
