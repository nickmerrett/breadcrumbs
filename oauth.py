"""
OAuth for Breadcrumbs, built on the MCP SDK's authorization-server support.

The SDK serves the standard endpoints (discovery metadata, dynamic client registration,
/authorize, /token, revocation) and enforces PKCE. This module supplies the storage and the
one human step: the owner proves who they are by entering one of their Breadcrumbs keys on a
login page. Two kinds of bearer token are accepted on /mcp:

  - static keys from BREADCRUMBS_KEYS (code assistants, scripts)
  - OAuth access tokens issued here (Claude chat and other OAuth-only clients)

Both carry an "agent" name, which is stamped on every entry as provenance.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from storage import SQLiteStore

ACCESS_TTL = 3600            # 1 hour
REFRESH_TTL = 30 * 86400     # 30 days
CODE_TTL = 300               # 5 minutes
PENDING_TTL = 600            # 10 minutes to finish logging in
MAX_LOGIN_ATTEMPTS = 5


class BreadcrumbsProvider:
    def __init__(self, store: SQLiteStore, keys: dict[str, str], public_url: str):
        self.store, self.keys, self.public_url = store, keys, public_url.rstrip("/")
        self._attempts: dict[str, int] = {}

    # ---------------------------------------------------------------- storage
    def _put(self, kind: str, key: str, payload: str, ttl: float | None) -> None:
        now = time.time()
        with self.store.session() as s:
            s.oauth_purge(now)
            s.oauth_put(kind, key, payload, now + ttl if ttl else None)

    def _get(self, kind: str, key: str) -> str | None:
        with self.store.session() as s:
            return s.oauth_get(kind, key, time.time())

    def _delete(self, kind: str, key: str) -> None:
        with self.store.session() as s:
            s.oauth_delete(kind, key)

    # ---------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._get("client", client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._put("client", client_info.client_id, client_info.model_dump_json(), None)

    # -------------------------------------------------------------- authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to our login page."""
        req = secrets.token_urlsafe(24)
        payload = json.dumps({"client_id": client.client_id, "params": params.model_dump(mode="json")})
        self._put("pending", req, payload, PENDING_TTL)
        return f"{self.public_url}/login?req={req}"

    def pending_client_name(self, req: str) -> str | None:
        """For the login page: who is asking for access?"""
        raw = self._get("pending", req)
        if not raw:
            return None
        client_id = json.loads(raw)["client_id"]
        raw_client = self._get("client", client_id)
        if not raw_client:
            return None
        c = OAuthClientInformationFull.model_validate_json(raw_client)
        return c.client_name or c.client_id

    def complete_login(self, req: str, key: str) -> str | None:
        """Check the key; on success return the URL to send the browser back to the client."""
        raw = self._get("pending", req)
        if not raw:
            return None
        self._attempts[req] = self._attempts.get(req, 0) + 1
        if self._attempts[req] > MAX_LOGIN_ATTEMPTS:
            self._delete("pending", req)
            return None
        owner = next((n for k, n in self.keys.items() if hmac.compare_digest(k, key)), None)
        if not owner:
            return ""  # wrong key: stay on the login page
        data = json.loads(raw)
        p = data["params"]
        self._delete("pending", req)
        self._attempts.pop(req, None)
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32), scopes=p.get("scopes") or [],
            expires_at=time.time() + CODE_TTL, client_id=data["client_id"],
            code_challenge=p["code_challenge"], redirect_uri=p["redirect_uri"],
            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
            resource=p.get("resource"), subject=owner)
        self._put("code", code.code, code.model_dump_json(), CODE_TTL)
        return construct_redirect_uri(str(p["redirect_uri"]), code=code.code, state=p.get("state"))

    # ------------------------------------------------------------------ codes
    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        raw = self._get("code", authorization_code)
        if not raw:
            return None
        code = AuthorizationCode.model_validate_json(raw)
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self._delete("code", authorization_code.code)   # single use
        return self._issue(client, authorization_code.scopes, authorization_code.resource,
                           authorization_code.subject)

    # ----------------------------------------------------------------- tokens
    def _issue(self, client: OAuthClientInformationFull, scopes: list[str],
               resource: str | None, subject: str | None) -> OAuthToken:
        agent = client.client_name or client.client_id
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        at = AccessToken(token=access, client_id=client.client_id, scopes=scopes,
                         expires_at=now + ACCESS_TTL, resource=resource, subject=subject,
                         claims={"agent": agent, "refresh": refresh})
        rt = RefreshToken(token=refresh, client_id=client.client_id, scopes=scopes,
                          expires_at=now + REFRESH_TTL, resource=resource, subject=subject)
        self._put("access", access, at.model_dump_json(), ACCESS_TTL)
        self._put("refresh", refresh, rt.model_dump_json(), REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        raw = self._get("refresh", refresh_token)
        if not raw:
            return None
        rt = RefreshToken.model_validate_json(raw)
        return rt if rt.client_id == client.client_id else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        granted = set(refresh_token.scopes)
        if scopes and not set(scopes) <= granted:
            raise TokenError(error="invalid_scope", error_description="scope not previously granted")
        self._delete("refresh", refresh_token.token)    # rotate: the old one dies
        return self._issue(client, scopes or refresh_token.scopes, refresh_token.resource,
                           refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Static keys: same bearer door for code assistants and scripts.
        for key, name in self.keys.items():
            if hmac.compare_digest(key, token):
                return AccessToken(token=token, client_id=name, scopes=[], expires_at=None,
                                   claims={"agent": name})
        raw = self._get("access", token)
        return AccessToken.model_validate_json(raw) if raw else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._delete("access", token.token)
            if token.claims and token.claims.get("refresh"):
                self._delete("refresh", token.claims["refresh"])
        else:
            self._delete("refresh", token.token)
