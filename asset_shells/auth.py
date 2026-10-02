"""Janua RS256 service tokens — the only identity asset-shells knows (ratified 2026-08-25:
one client per edge, explicit audience, scope per capability, RS256 only, enforced at the target,
fail-closed).

* Signature: RS256 only, the key chosen by the token's ``kid`` from Janua's JWKS. A token without
  ``kid`` is rejected before any key lookup.
* Claims: ``iss`` must equal the configured issuer, ``aud`` must contain ``asset-shells-api``;
  ``exp``, ``iat`` and ``sub`` are required.
* Scopes come from the space-separated ``scope`` claim (Janua's service-token shape).
* ``tenant_id`` (present on org-bound clients) scopes every instance read and write.

Anonymous callers are allowed on type reads only. A request that PRESENTS a token is never
downgraded to anonymous: an invalid token is a 401 even on public routes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import jwt
from fastapi import Request

from .errors import forbidden, unauthorized
from .ids import is_valid_tenant
from .settings import get_settings

log = logging.getLogger(__name__)

SCOPE_READ = "asset-shells:read"
SCOPE_PUBLISH_TYPES = "asset-shells:publish-types"
SCOPE_PUBLISH_INSTANCES = "asset-shells:publish-instances"

_jwks_client: jwt.PyJWKClient | None = None
_local_keys: dict[str, jwt.PyJWK] | None = None


def reset_key_cache() -> None:
    global _jwks_client, _local_keys
    _jwks_client = None
    _local_keys = None


def _load_local_jwks(path: str) -> dict[str, jwt.PyJWK]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keys: dict[str, jwt.PyJWK] = {}
    for jwk in data.get("keys", []):
        if jwk.get("kid"):
            keys[jwk["kid"]] = jwt.PyJWK(jwk)
    return keys


def signing_key_for(token: str, kid: str):
    """The RS256 public key for ``kid``: Janua's JWKS (cached), or a local JWKS file in local/test."""
    global _jwks_client, _local_keys
    s = get_settings()
    if s.jwks_path:
        if _local_keys is None:
            _local_keys = _load_local_jwks(s.jwks_path)
        if kid not in _local_keys:
            raise jwt.InvalidKeyError("unknown kid")
        return _local_keys[kid].key
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            s.effective_jwks_url, cache_keys=True, lifespan=s.jwks_cache_seconds, timeout=5
        )
    return _jwks_client.get_signing_key(kid).key


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str | None
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def verify_token(token: str) -> Principal:
    s = get_settings()
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError("only RS256 is accepted")
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise jwt.InvalidTokenError("token has no kid")
    key = signing_key_for(token, kid)
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=s.janua_audience,
        issuer=s.janua_issuer,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        leeway=30,
    )
    scope_claim = claims.get("scope", "")
    scopes = frozenset(scope_claim.split()) if isinstance(scope_claim, str) else frozenset()
    tenant = claims.get("tenant_id")
    tenant_id = tenant if isinstance(tenant, str) and tenant else None
    return Principal(sub=str(claims["sub"]), tenant_id=tenant_id, scopes=scopes)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized("invalid_authorization", "Authorization must be 'Bearer <token>'")
    return token.strip()


def optional_principal(request: Request) -> Principal | None:
    """Anonymous when no Authorization header; 401 when a presented token does not verify."""
    token = _bearer(request)
    if token is None:
        return None
    try:
        principal = verify_token(token)
    except jwt.PyJWTError as exc:
        log.info("token rejected: %s", type(exc).__name__)
        raise unauthorized("invalid_token", f"Token rejected ({type(exc).__name__})") from None
    request.state.principal_sub = principal.sub
    return principal


def require_principal(request: Request) -> Principal:
    principal = optional_principal(request)
    if principal is None:
        raise unauthorized("missing_token", "This operation requires a Janua service token")
    return principal


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise forbidden("missing_scope", f"The token lacks the scope '{scope}'")


def require_tenant(principal: Principal) -> str:
    if principal.tenant_id is None:
        raise forbidden("missing_tenant", "Instance data requires an organisation-bound token (tenant_id claim)")
    if not is_valid_tenant(principal.tenant_id):
        raise forbidden("invalid_tenant", "The tenant_id claim has an unsupported format")
    return principal.tenant_id


def read_tenant(principal: Principal | None) -> str | None:
    """The tenant whose instance rows a READ may see: only with the read scope and a valid tenant."""
    if principal is None or not principal.has(SCOPE_READ) or principal.tenant_id is None:
        return None
    if not is_valid_tenant(principal.tenant_id):
        return None
    return principal.tenant_id
