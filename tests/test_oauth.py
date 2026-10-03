"""The full OAuth dance as Claude chat performs it: discover, register, authorise, token."""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import create_app, parse_keys

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "o.db"), parse_keys("owner:OWNERKEY"), "http://localhost:8080")
    with TestClient(app) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, name="Claude"):
    r = client.post("/register", json={
        "client_name": name, "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def start_auth(client, client_id, challenge, state="st8"):
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state},
        follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    assert loc.path == "/login"
    return parse_qs(loc.query)["req"][0]


def get_code(client, req, key="OWNERKEY"):
    r = client.post("/login", data={"req": req, "key": key}, follow_redirects=False)
    return r


def tokens(client, client_id, code, verifier):
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CALLBACK, "code_verifier": verifier})


def full_flow(client, name="Claude"):
    cid = register(client, name)
    verifier, challenge = pkce()
    req = start_auth(client, cid, challenge)
    r = get_code(client, req)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["st8"]
    t = tokens(client, cid, q["code"][0], verifier)
    assert t.status_code == 200, t.text
    return cid, t.json()


def test_discovery_metadata(client):
    unauth = client.post("/mcp", json={})
    assert unauth.status_code == 401
    assert "resource_metadata" in unauth.headers["www-authenticate"]
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["authorization_servers"][0].rstrip("/") == "http://localhost:8080"
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" in meta and "S256" in meta["code_challenge_methods_supported"]


def test_login_page_names_the_client(client):
    cid = register(client, "Claude")
    req = start_auth(client, cid, pkce()[1])
    page = client.get("/login", params={"req": req})
    assert page.status_code == 200 and "Connect Claude" in page.text


def test_wrong_key_does_not_issue_a_code(client):
    cid = register(client)
    req = start_auth(client, cid, pkce()[1])
    r = get_code(client, req, key="nope")
    assert r.status_code == 401 and "not recognised" in r.text
    assert get_code(client, req).status_code == 303          # the right key still works afterwards


def test_login_attempts_are_limited(client):
    cid = register(client)
    req = start_auth(client, cid, pkce()[1])
    for _ in range(5):
        get_code(client, req, key="nope")
    assert get_code(client, req, key="OWNERKEY").status_code == 400   # request burned


def test_full_flow_and_provenance(client):
    cid, tok = full_flow(client, "Claude")
    h = {"Authorization": f"Bearer {tok['access_token']}", **MCP}
    saved = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "save_entry", "arguments": {"title": "Pancakes", "body": "Rest it.", "type": "recipe"}}})
    assert saved.status_code == 200
    entry = client.get("/api/entries/1", headers={"Authorization": "Bearer OWNERKEY"}).json()
    assert entry["author"] == "Claude"                       # identity from the OAuth client


def test_pkce_is_enforced(client):
    cid = register(client)
    verifier, challenge = pkce()
    req = start_auth(client, cid, challenge)
    code = parse_qs(urlparse(get_code(client, req).headers["location"]).query)["code"][0]
    assert tokens(client, cid, code, "wrong-verifier-" + "x" * 40).status_code == 400


def test_code_is_single_use(client):
    cid = register(client)
    verifier, challenge = pkce()
    req = start_auth(client, cid, challenge)
    code = parse_qs(urlparse(get_code(client, req).headers["location"]).query)["code"][0]
    assert tokens(client, cid, code, verifier).status_code == 200
    assert tokens(client, cid, code, verifier).status_code == 400


def test_refresh_rotates_tokens(client):
    cid, tok = full_flow(client)
    r = client.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
                                    "refresh_token": tok["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]
    reuse = client.post("/token", data={"grant_type": "refresh_token", "client_id": cid,
                                        "refresh_token": tok["refresh_token"]})
    assert reuse.status_code == 400                          # old refresh token is dead
    ok = client.post("/mcp", headers={"Authorization": f"Bearer {new['access_token']}", **MCP},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert ok.status_code == 200


def test_revoked_token_stops_working(client):
    cid, tok = full_flow(client)
    # The SDK's /revoke insists on a client_secret field even for public clients; empty is fine.
    r = client.post("/revoke", data={"token": tok["access_token"], "client_id": cid, "client_secret": ""})
    assert r.status_code == 200
    r = client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}", **MCP},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_static_key_still_opens_mcp(client):
    r = client.post("/mcp", headers={"Authorization": "Bearer OWNERKEY", **MCP},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
