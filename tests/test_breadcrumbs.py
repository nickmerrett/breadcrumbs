import base64

import pytest
from fastapi.testclient import TestClient

from app import create_app, parse_keys
from core import Notebook
from storage import SQLiteStore

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def nb(tmp_path):
    return Notebook(SQLiteStore(tmp_path / "t.db"))


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"), parse_keys("claude:K1,code:K2"))
    with TestClient(app) as c:
        yield c


def auth(key):
    return {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------------ core
def test_save_and_search(nb):
    r = nb.save_entry("claude", "Fluffy pancakes", "Rest the batter 10 min.", "recipe",
                      ["domain:cooking"], summary="Rested batter is fluffier")
    assert r["saved"] and r["status"] == "approved"
    hits = nb.search("pancake")
    assert [h["title"] for h in hits] == ["Fluffy pancakes"]
    assert hits[0]["author"] == "claude"


def test_aliases_and_fuzzy_tags(nb):
    r = nb.save_entry("a", "T", "b", "discovery", ["food", "py", "domain:cooking/bakng", "Brand New"])
    assert "domain:cooking" in r["tags"]
    assert "tech:python" in r["tags"]
    assert "domain:cooking/baking" in r["tags"]      # typo mapped to existing tag
    assert "tag:brand-new" in r["tags"]              # unknown goes to free tier
    assert any("mapped to existing" in n for n in r["notes"])


def test_duplicate_detection_and_force(nb):
    nb.save_entry("a", "Same", "x", "recipe")
    dup = nb.save_entry("b", "same", "y", "recipe")
    assert dup["saved"] is False and "duplicate_of" in dup
    assert nb.save_entry("b", "same", "y", "recipe", force=True)["saved"]


def test_parent_tag_and_alias_filters(nb):
    nb.save_entry("a", "Sourdough", "x", "recipe", ["domain:cooking/baking"])
    nb.save_entry("a", "Pasta", "x", "recipe", ["domain:cooking"])
    nb.save_entry("a", "Pruning", "x", "howto", ["domain:gardening"])
    assert {h["title"] for h in nb.search(tag="domain:cooking")} == {"Sourdough", "Pasta"}
    assert {h["title"] for h in nb.search(tag="baking")} == {"Sourdough"}   # alias in filter
    assert {h["title"] for h in nb.search(type="howto")} == {"Pruning"}


def test_status_and_verify(nb):
    eid = nb.save_entry("a", "Keep me", "x", "idea")["id"]
    nb.set_status(eid, "rejected")
    assert nb.search("keep") == []                    # rejected entries are hidden
    assert nb.verify(eid) is True
    got = nb.get_entry(eid)
    assert got["status"] == "approved" and got["last_verified"]


def test_semantic_search(nb):
    nb.save_entry("a", "Sourdough starter", "Feed with flour and water daily.", "recipe",
                  summary="Maintaining a live sourdough culture")
    nb.save_entry("a", "Git rebase tips", "Use interactive rebase to squash commits.", "snippet")
    hits = nb.search("fermented bread culture", semantic=True)
    assert hits and hits[0]["title"] == "Sourdough starter"


def test_source_context_stored(nb):
    eid = nb.save_entry("a", "Pasta dough", "Mix flour and eggs.", "recipe",
                        source_context="dinner party planning")["id"]
    assert nb.get_entry(eid)["source_context"] == "dinner party planning"


def test_since_filter(nb):
    nb.save_entry("a", "Recent", "x", "idea")
    assert len(nb.search(since="7d")) == 1
    assert nb.search(since="2999-01-01") == []


def test_validation(nb):
    with pytest.raises(ValueError):
        nb.save_entry("a", "T", "b", "nonsense")
    with pytest.raises(ValueError):
        nb.save_entry("a", "T", "b", "recipe", outcome="great")
    with pytest.raises(ValueError):
        nb.set_status(1, "bogus")


def test_wal_enabled(tmp_path):
    store = SQLiteStore(tmp_path / "w.db")
    with store.session() as s:
        assert s.c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_export_round_trip(nb):
    nb.save_entry("a", "One", "b", "recipe", data={"servings": 4})
    out = nb.export()
    assert out[0]["title"] == "One" and out[0]["data"] == {"servings": 4}


# ------------------------------------------------------------------- REST
def test_rest_requires_key(client):
    assert client.get("/api/tags").status_code == 401
    assert client.get("/api/tags", headers=auth("nope")).status_code == 401
    assert client.get("/api/tags", headers=auth("K1")).status_code == 200


def test_rest_save_search_export(client):
    body = {"title": "Fluffy pancakes", "body": "Rest it.", "type": "recipe", "tags": ["food"]}
    assert client.post("/api/entries", json=body, headers=auth("K1")).json()["saved"]
    hits = client.get("/api/search", params={"q": "pancakes"}, headers=auth("K2")).json()
    assert hits[0]["author"] == "claude"
    assert len(client.get("/api/export", headers=auth("K2")).json()) == 1
    bad = client.post("/api/entries", json={**body, "type": "x"}, headers=auth("K1"))
    assert bad.status_code == 422


def test_ui_pages(client):
    client.post("/api/entries", headers=auth("K1"),
                json={"title": "T <b>", "body": "b", "type": "idea"})
    basic = {"Authorization": "Basic " + base64.b64encode(b"me:K1").decode()}
    home = client.get("/", headers=basic)
    assert home.status_code == 200 and "&lt;b&gt;" in home.text   # escaped
    assert client.get("/e/1", headers=basic).status_code == 200
    r = client.post("/e/1/act", data={"do": "approve"}, headers=basic, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", headers={}).status_code == 401


# -------------------------------------------------------------------- MCP
def rpc(client, key, method, params=None, id_=1):
    return client.post("/mcp", headers={**auth(key), **MCP_HEADERS},
                       json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_mcp_requires_key(client):
    assert client.post("/mcp", json={}).status_code == 401


def test_mcp_tools_and_author_stamp(client):
    names = {t["name"] for t in rpc(client, "K2", "tools/list").json()["result"]["tools"]}
    assert names == {"save_entry", "search", "get_entry", "list_tags", "mark_verified"}
    saved = rpc(client, "K2", "tools/call", {"name": "save_entry", "arguments": {
        "title": "Pg index trick", "body": "b", "type": "discovery", "tags": ["tech:postgresql"]}})
    assert saved.status_code == 200
    entry = client.get("/api/entries/1", headers=auth("K1")).json()
    assert entry["author"] == "code"                  # identity came from the MCP key
    assert "tech:postgres" in entry["tags"]
    found = rpc(client, "K1", "tools/call", {"name": "search", "arguments": {"query": "index"}})
    assert "Pg index trick" in found.text
