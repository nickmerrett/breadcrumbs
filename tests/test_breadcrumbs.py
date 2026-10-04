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


def test_update_entry(nb):
    eid = nb.save_entry("a", "Pancake recipe", "Mix flour and eggs slowly.", "idea", ["domain:work"])["id"]
    r = nb.update_entry(eid, title="Waffle recipe", body="Mix flour and butter quickly.", tags=["domain:learning"])
    assert r["updated"] and r["tags"] == ["domain:learning"]
    got = nb.get_entry(eid)
    assert got["title"] == "Waffle recipe" and got["body"] == "Mix flour and butter quickly."
    assert "domain:learning" in got["tags"] and "domain:work" not in got["tags"]
    # FTS re-indexed: unique old words gone, unique new words present
    assert nb.search("Waffle")       # new title indexed
    assert nb.search("butter")       # new body word indexed
    assert nb.search("Pancake") == []  # old title removed
    assert nb.search("eggs") == []     # old body word removed


def test_update_entry_not_found(nb):
    with pytest.raises(ValueError, match="not found"):
        nb.update_entry(999, title="x")


def test_archive_hidden_from_search(nb):
    eid = nb.save_entry("a", "Archived thing", "body", "idea")["id"]
    nb.set_status(eid, "archived")
    assert nb.search("Archived thing") == []
    # But the entry itself is still retrievable directly
    assert nb.get_entry(eid)["status"] == "archived"


def test_pagination(nb):
    for i in range(5):
        nb.save_entry("a", f"Entry {i}", "body", "idea")
    page0 = nb.search(limit=3, offset=0)
    page1 = nb.search(limit=3, offset=3)
    assert len(page0) == 3
    assert len(page1) == 2
    # No overlap
    ids0 = {r["id"] for r in page0}
    ids1 = {r["id"] for r in page1}
    assert ids0.isdisjoint(ids1)


def test_rest_patch_entry(client):
    client.post("/api/entries", headers=auth("K1"),
                json={"title": "Original", "body": "First draft", "type": "idea"})
    r = client.patch("/api/entries/1", headers=auth("K1"),
                     json={"title": "Revised", "body": "Second draft"})
    assert r.status_code == 200 and r.json()["updated"]
    got = client.get("/api/entries/1", headers=auth("K1")).json()
    assert got["title"] == "Revised" and got["body"] == "Second draft"


def test_rest_search_pagination(client):
    for i in range(5):
        client.post("/api/entries", headers=auth("K1"),
                    json={"title": f"Item {i}", "body": "x", "type": "idea"})
    p0 = client.get("/api/search", headers=auth("K1"), params={"limit": 3, "offset": 0}).json()
    p1 = client.get("/api/search", headers=auth("K1"), params={"limit": 3, "offset": 3}).json()
    assert len(p0) == 3 and len(p1) == 2
    assert {r["id"] for r in p0}.isdisjoint({r["id"] for r in p1})


def test_ui_archive_and_edit(client):
    basic = {"Authorization": "Basic " + base64.b64encode(b"me:K1").decode()}
    client.post("/api/entries", headers=auth("K1"),
                json={"title": "To archive", "body": "body", "type": "idea"})
    r = client.post("/e/1/act", data={"do": "archive"}, headers=basic, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/entries/1", headers=auth("K1")).json()["status"] == "archived"
    # Edit form loads
    assert client.get("/e/1/edit", headers=basic).status_code == 200
    # Edit submit redirects back to entry
    r2 = client.post("/e/1/edit", headers=basic,
                     data={"title": "Edited", "body": "new body", "summary": "",
                           "outcome": "", "tags": "", "source_url": "", "source_context": ""},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert client.get("/api/entries/1", headers=auth("K1")).json()["title"] == "Edited"


def test_due_before_filter(nb):
    nb.save_entry("a", "Overdue task", "x", "idea", due="2000-01-01")
    nb.save_entry("a", "Future task", "x", "idea", due="2999-01-01")
    nb.save_entry("a", "No due date", "x", "idea")
    overdue = nb.search(due_before="2001-01-01")
    assert [r["title"] for r in overdue] == ["Overdue task"]
    all_due = nb.search(due_before="2999-12-31")
    titles = {r["title"] for r in all_due}
    assert "Overdue task" in titles and "Future task" in titles
    assert "No due date" not in titles


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
