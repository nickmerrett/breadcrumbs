"""
Breadcrumbs app factory. Three front doors over one Notebook:
  MCP  at /mcp      REST at /api/...      Browse UI at /
"""
from __future__ import annotations

import base64
import contextlib

from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

import web
from core import Notebook
from oauth import BreadcrumbsProvider
from storage import SQLiteStore

INSTRUCTIONS = (
    "A shared long-term notebook used by several AI clients and one human. "
    "Before starting work that may have been done before, call search. "
    "Save with save_entry when you have confirmed something worth keeping: a working recipe, "
    "a verified discovery, a reusable snippet, or a dead end and why it failed. "
    "Entries must be self-contained: never refer to 'the conversation above'. "
    "Call list_tags first and reuse existing tags; do not invent near-duplicates."
)

def parse_keys(spec: str) -> dict[str, str]:
    """'claude:KEY1,code:KEY2' -> {'KEY1': 'claude', 'KEY2': 'code'}"""
    return {k: n for n, k in (p.split(":", 1) for p in spec.split(",") if ":" in p)}


def authenticate(header: str | None, keys: dict[str, str]) -> str | None:
    if not keys:
        return "dev"  # no keys configured: open, for local trials only
    if not header:
        return None
    if header.startswith("Bearer "):
        return keys.get(header[7:])
    if header.startswith("Basic "):  # browsers: any username, the key as password
        try:
            return keys.get(base64.b64decode(header[6:]).decode().split(":", 1)[1])
        except Exception:
            return None
    return None


class EntryIn(BaseModel):
    title: str
    body: str
    type: str
    tags: list[str] = []
    outcome: str | None = None
    summary: str | None = None
    data: dict | None = None
    source_url: str | None = None
    source_context: str | None = None
    confidence: float = Field(0.5, ge=0, le=1)
    state: str | None = None
    due: str | None = None
    force: bool = False


class EntryUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    summary: str | None = None
    outcome: str | None = None
    data: dict | None = None
    source_url: str | None = None
    source_context: str | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    tags: list[str] | None = None


def caller() -> str:
    """Who is calling this MCP tool? Set by the SDK's auth layer from the bearer token."""
    tok = get_access_token()
    if not tok:
        return "dev"  # auth disabled (no keys configured)
    return (tok.claims or {}).get("agent") or tok.client_id


def create_app(db_path: str, keys: dict[str, str], public_url: str = "http://localhost:8080",
               trusted_agents: set[str] | None = None) -> FastAPI:
    store = SQLiteStore(db_path)
    nb = Notebook(store, trusted_agents=trusted_agents)
    public_url = public_url.rstrip("/")
    provider = BreadcrumbsProvider(store, keys, public_url) if keys else None

    # ----------------------------------------------------------------- MCP
    auth_kwargs = {}
    if provider:  # OAuth (Claude chat) plus static bearer keys (code assistants, scripts)
        auth_kwargs = dict(
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=public_url, resource_server_url=f"{public_url}/mcp",
                validate_token_resource=False,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True)))
    mcp = FastMCP("breadcrumbs", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                  **auth_kwargs)

    @mcp.tool()
    def save_entry(title: str, body: str, type: str, tags: list[str] | None = None,
                   outcome: str | None = None, summary: str | None = None,
                   data: dict | None = None, source_url: str | None = None,
                   source_context: str | None = None,
                   confidence: float = 0.5) -> dict:
        """Save a self-contained entry to the shared notebook. type is one of: recipe, discovery,
        snippet, dead-end, howto, reference, question, idea. tags look like 'domain:cooking/baking'
        or 'tech:python' (call list_tags first and reuse them). outcome: worked, partial, failed or
        untested. summary is one plain line. source_context is optional freeform provenance (e.g.
        the conversation topic or project name). For recipes put ingredients and method in body
        (markdown) and any structured fields in data. Returns the id, or a duplicate_of id if the
        entry already exists."""
        try:
            return nb.save_entry(caller(), title, body, type, tags, outcome,
                                 summary, data, source_url, source_context, confidence)
        except ValueError as ex:
            return {"saved": False, "error": str(ex)}

    @mcp.tool()
    def search(query: str | None = None, tag: str | None = None, type: str | None = None,
               since: str | None = None, limit: int = 8, semantic: bool = False) -> list[dict]:
        """Find entries. query is keywords; tag filters (a parent tag includes its children, e.g.
        'domain:cooking'); type filters by entry type; since is '7d' or an ISO date. With no query,
        returns the most recent entries. Set semantic=true for meaning-based search when keywords
        are vague or you cannot recall exact wording. Returns short summaries; use get_entry for full text."""
        return nb.search(query, tag, type, since, limit, semantic)

    @mcp.tool()
    def get_entry(id: int) -> dict:
        """Fetch the full entry (body, structured data, provenance) by id."""
        return nb.get_entry(id) or {"error": "not found"}

    @mcp.tool()
    def list_tags() -> list[dict]:
        """List the tag vocabulary with usage counts. Call before tagging so you reuse existing tags."""
        return nb.list_tags()

    @mcp.tool()
    def mark_verified(id: int) -> dict:
        """Mark an entry as re-checked and still correct (updates last_verified)."""
        return {"ok": nb.verify(id)}

    # ---------------------------------------------------------------- core
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Breadcrumbs", lifespan=lifespan)

    def current_agent(authorization: str | None = Header(None)) -> str:
        a = authenticate(authorization, keys)
        if not a:
            raise HTTPException(401, "Invalid or missing key",
                                headers={"WWW-Authenticate": 'Basic realm="Breadcrumbs"'})
        return a

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---------------------------------------------------------------- REST
    @app.post("/api/entries")
    def api_save(e: EntryIn, agent: str = Depends(current_agent)):
        try:
            return nb.save_entry(agent, **e.model_dump())
        except ValueError as ex:
            raise HTTPException(422, str(ex))

    @app.get("/api/search")
    def api_search(q: str | None = None, tag: str | None = None, type: str | None = None,
                   since: str | None = None, limit: int = 10, semantic: bool = False,
                   offset: int = 0, agent: str = Depends(current_agent)):
        return nb.search(q, tag, type, since, limit, semantic, offset)

    @app.get("/api/entries/{eid}")
    def api_get(eid: int, agent: str = Depends(current_agent)):
        entry = nb.get_entry(eid)
        if not entry:
            raise HTTPException(404, "not found")
        return entry

    @app.patch("/api/entries/{eid}")
    def api_update(eid: int, u: EntryUpdate, agent: str = Depends(current_agent)):
        try:
            return nb.update_entry(eid, **u.model_dump(exclude_none=True))
        except ValueError as ex:
            raise HTTPException(422, str(ex))

    @app.post("/api/entries/{eid}/status")
    def api_status(eid: int, status: str, agent: str = Depends(current_agent)):
        try:
            return {"ok": nb.set_status(eid, status)}
        except ValueError as ex:
            raise HTTPException(422, str(ex))

    @app.post("/api/entries/{eid}/verify")
    def api_verify(eid: int, agent: str = Depends(current_agent)):
        return {"ok": nb.verify(eid)}

    @app.get("/api/tags")
    def api_tags(agent: str = Depends(current_agent)):
        return nb.list_tags()

    @app.get("/api/export")
    def api_export(agent: str = Depends(current_agent)):
        """Everything, as JSON. The store should never be a trap."""
        return nb.export()

    # ---------------------------------------------------------- OAuth login
    # The SDK serves /.well-known/*, /register, /authorize, /token and /revoke.
    # /authorize sends the browser here to prove the owner is present.
    if provider:
        @app.get("/login", response_class=HTMLResponse)
        def login_page(req: str):
            name = provider.pending_client_name(req)
            if not name:
                return HTMLResponse(web.page("Expired", "<p>This connection request has expired. "
                                             "Start it again from the app you are connecting.</p>"), 400)
            return web.page("Connect", web.login(name, req))

        @app.post("/login", response_class=HTMLResponse)
        def login_submit(req: str = Form(...), key: str = Form(...)):
            target = provider.complete_login(req, key)
            if target is None:
                return HTMLResponse(web.page("Expired", "<p>This connection request has expired "
                                             "or had too many attempts. Start it again.</p>"), 400)
            if target == "":
                name = provider.pending_client_name(req) or "the app"
                return HTMLResponse(web.page("Connect", web.login(name, req, "That key was not recognised.")), 401)
            return RedirectResponse(target, status_code=303)

    # ------------------------------------------------------------------ UI
    PAGE_SIZE = 20

    @app.get("/", response_class=HTMLResponse)
    def ui_home(q: str = "", tag: str = "", type: str = "", page: int = 0,
                agent: str = Depends(current_agent)):
        rows = nb.search(q or None, tag or None, type or None, None, PAGE_SIZE, offset=page * PAGE_SIZE)
        has_prev = page > 0
        has_next = len(rows) == PAGE_SIZE
        return web.page("Breadcrumbs", web.home(rows, q, tag, type, page, has_prev, has_next))

    @app.get("/e/{eid}", response_class=HTMLResponse)
    def ui_entry(eid: int, agent: str = Depends(current_agent)):
        entry = nb.get_entry(eid)
        if not entry:
            raise HTTPException(404, "not found")
        return web.page(entry["title"], web.entry(entry))

    @app.get("/e/{eid}/edit", response_class=HTMLResponse)
    def ui_edit_form(eid: int, agent: str = Depends(current_agent)):
        entry = nb.get_entry(eid)
        if not entry:
            raise HTTPException(404, "not found")
        return web.page(f"Edit — {entry['title']}", web.edit_form(entry))

    @app.post("/e/{eid}/edit")
    def ui_edit_submit(eid: int, agent: str = Depends(current_agent),
                       title: str = Form(...), body: str = Form(...),
                       summary: str = Form(""), outcome: str = Form(""),
                       tags: str = Form(""), source_url: str = Form(""),
                       source_context: str = Form("")):
        try:
            nb.update_entry(
                eid,
                title=title, body=body,
                summary=summary or None,
                outcome=outcome or None,
                tags=[t.strip() for t in tags.split(",") if t.strip()] if tags.strip() else None,
                source_url=source_url or None,
                source_context=source_context or None,
            )
        except ValueError as ex:
            raise HTTPException(422, str(ex))
        return RedirectResponse(f"/e/{eid}", status_code=303)

    @app.post("/e/{eid}/act")
    def ui_act(eid: int, do: str = Form(...), agent: str = Depends(current_agent)):
        if do == "approve":
            nb.verify(eid)
        elif do == "archive":
            nb.set_status(eid, "archived")
        else:
            nb.set_status(eid, "rejected")
        return RedirectResponse(f"/e/{eid}", status_code=303)

    # Mounted last so the routes above take precedence; its endpoint is /mcp
    app.mount("/", mcp.streamable_http_app())
    return app
