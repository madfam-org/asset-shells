"""Token verification: RS256 + kid + iss/aud/exp, fail-closed, never downgraded to anonymous."""

from __future__ import annotations

import datetime as dt

import jwt
import pytest
from aas_fixtures import solid_type_env
from conftest import AUDIENCE, ISSUER, KID, PUB_TYPES, READ, TENANT_A
from cryptography.hazmat.primitives.asymmetric import rsa

from asset_shells import auth, settings
from asset_shells.ids import b64url_encode as enc

INSTANCE = enc("https://id.madfam.io/aas/instance/1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d")
URL = "/api/v3.1/shells"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_reads(client, auth_header):
    assert client.get(URL, headers=auth_header((READ,), tenant=TENANT_A)).status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "yantra4d-api"},
        {"iss": "https://evil.example"},
        {"exp": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)},
        {"iat": None},
        {"sub": None},
        {"kid": "unknown-kid"},
    ],
)
def test_bad_claims_are_rejected_even_on_public_routes(client, make_token, overrides):
    response = client.get(URL, headers=bearer(make_token((READ,), TENANT_A, **overrides)))
    assert response.status_code == 401
    assert response.json()["messages"][0]["code"] == "invalid_token"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_other_signing_key_is_rejected(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now, "exp": now + dt.timedelta(minutes=5)},
        other,
        algorithm="RS256",
        headers={"kid": KID},
    )
    assert client.get(URL, headers=bearer(token)).status_code == 401


def test_symmetric_and_unsigned_tokens_are_rejected(client):
    now = dt.datetime.now(dt.UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "x",
        "iat": now,
        "exp": now + dt.timedelta(minutes=5),
        "scope": READ,
        "tenant_id": TENANT_A,
    }
    hs = jwt.encode(claims, "a-shared-secret-of-thirty-two-bytes!", algorithm="HS256", headers={"kid": KID})
    assert client.get(URL, headers=bearer(hs)).status_code == 401
    none = jwt.encode(claims, None, algorithm="none", headers={"kid": KID})
    assert client.get(URL, headers=bearer(none)).status_code == 401


def test_token_without_kid_is_rejected(client, signing_key):
    key, _ = signing_key
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": now, "exp": now + dt.timedelta(minutes=5)},
        key,
        algorithm="RS256",
    )
    assert client.get(URL, headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "token abc"])
def test_malformed_authorization_header(client, header):
    response = client.get(URL, headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["messages"][0]["code"] == "invalid_authorization"


def test_scope_string_parsing(make_token):
    principal = auth.verify_token(make_token(("asset-shells:read", "other:scope"), TENANT_A))
    assert principal.has(READ) and principal.tenant_id == TENANT_A
    non_string = auth.verify_token(make_token((), None, scope=["asset-shells:read"], tenant_id=7))
    assert non_string.scopes == frozenset() and non_string.tenant_id is None
    assert auth.read_tenant(non_string) is None
    assert auth.read_tenant(auth.Principal("s", "bad tenant", frozenset({READ}))) is None


def test_publish_scopes_do_not_imply_read_of_instances(client, auth_header):
    response = client.get(f"/api/v3.1/shells/{INSTANCE}", headers=auth_header((PUB_TYPES,), tenant=TENANT_A))
    assert response.status_code == 403


def test_jwks_client_path_is_used_outside_local(monkeypatch, make_token, signing_key):
    """Production path: PyJWKClient against <issuer>/.well-known/jwks.json, keyed by kid."""
    key, _ = signing_key
    monkeypatch.setenv("ASSET_SHELLS_ENV", "production")
    monkeypatch.delenv("JWKS_PATH")
    settings.reset_settings_cache()
    auth.reset_key_cache()
    seen = {}

    class FakeJWKClient:
        def __init__(self, url, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs

        def get_signing_key(self, kid):
            seen["kid"] = kid
            return type("K", (), {"key": key.public_key()})()

    monkeypatch.setattr(auth.jwt, "PyJWKClient", FakeJWKClient)
    try:
        principal = auth.verify_token(make_token((READ,), TENANT_A))
        assert principal.tenant_id == TENANT_A
        assert seen["url"] == "https://auth.madfam.io/.well-known/jwks.json" and seen["kid"] == KID
        assert seen["kwargs"]["lifespan"] == 3600
    finally:
        monkeypatch.undo()  # restore ASSET_SHELLS_ENV and JWKS_PATH before the caches are rebuilt
        settings.reset_settings_cache()
        auth.reset_key_cache()


def test_issuer_and_jwks_url_are_configurable(monkeypatch):
    monkeypatch.setenv("JANUA_ISSUER", "https://auth.example.test")
    settings.reset_settings_cache()
    try:
        assert settings.get_settings().effective_jwks_url == "https://auth.example.test/.well-known/jwks.json"
        monkeypatch.setenv("JANUA_JWKS_URL", "https://keys.example.test/jwks")
        settings.reset_settings_cache()
        assert settings.get_settings().effective_jwks_url == "https://keys.example.test/jwks"
    finally:
        monkeypatch.delenv("JANUA_ISSUER")
        monkeypatch.delenv("JANUA_JWKS_URL", raising=False)
        settings.reset_settings_cache()


def test_type_reads_need_no_token(client, auth_header):
    published = client.put(
        "/madfam/v1/type-environments/solid-hyperobjects/" + "a" * 40,
        json={"environments": [solid_type_env()]},
        headers=auth_header((PUB_TYPES,)),
    )
    assert published.status_code == 201
    shell = enc(solid_type_env()["assetAdministrationShells"][0]["id"])
    assert client.get(f"/api/v3.1/shells/{shell}").status_code == 200
