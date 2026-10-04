"""Test harness.

Database tests need two URLs to ONE PostgreSQL database (see scripts/test-db-setup.sql):

* ``ASSET_SHELLS_TEST_ADMIN_URL`` — the schema owner (NOSUPERUSER, NOBYPASSRLS): runs the migration,
  truncates between tests, and plays "the owner" in the immutability tests;
* ``ASSET_SHELLS_TEST_APP_URL`` — the runtime role the service uses.

If they are missing, every database test ERRORS (it is never skipped): a skipped tenancy proof is not
a pass. Tokens are real RS256 JWTs signed by a key generated per session; the service verifies them
through a JWKS file (``JWKS_PATH``, honoured only in the test environment).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

ADMIN_URL_ENV = "ASSET_SHELLS_TEST_ADMIN_URL"
APP_URL_ENV = "ASSET_SHELLS_TEST_APP_URL"
ISSUER = "https://auth.madfam.io"
AUDIENCE = "asset-shells-api"
KID = "test-key-1"
TABLES = "asset_edges, outbox, passport_events, type_releases, concept_descriptions, asset_ids, submodels, shells"

TENANT_A = "org-tenant-a"
TENANT_B = "org-tenant-b"
READ = "asset-shells:read"
PUB_TYPES = "asset-shells:publish-types"
PUB_INST = "asset-shells:publish-instances"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; database tests cannot run (they are not skipped)")
    return value


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory) -> tuple[rsa.RSAPrivateKey, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    path = tmp_path_factory.mktemp("jwks") / "jwks.json"
    path.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    return key, path


@pytest.fixture(scope="session", autouse=True)
def test_environment(signing_key) -> Iterator[None]:
    _, jwks_path = signing_key
    saved = {k: os.environ.get(k) for k in ("ASSET_SHELLS_ENV", "JWKS_PATH", "APP_DATABASE_URL", "DATABASE_URL")}
    os.environ["ASSET_SHELLS_ENV"] = "test"
    os.environ["JWKS_PATH"] = str(jwks_path)
    os.environ["DB_STARTUP_RETRY_SECONDS"] = "5"
    if os.environ.get(APP_URL_ENV):
        os.environ["APP_DATABASE_URL"] = os.environ[APP_URL_ENV]
    if os.environ.get(ADMIN_URL_ENV):
        os.environ["DATABASE_URL"] = os.environ[ADMIN_URL_ENV]
    from asset_shells import auth, settings

    settings.reset_settings_cache()
    auth.reset_key_cache()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def make_token(signing_key) -> Callable[..., str]:
    key, _ = signing_key

    def make(scopes=(), tenant: str | None = None, **overrides) -> str:
        now = dt.datetime.now(dt.UTC)
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "service-account:jnc_test",
            "iat": now,
            "exp": now + dt.timedelta(minutes=10),
            "scope": " ".join(scopes),
            "token_use": "client_credentials",
        }
        if tenant is not None:
            claims["tenant_id"] = tenant
        headers = {"kid": overrides.pop("kid", KID)}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key, algorithm="RS256", headers=headers)

    return make


@pytest.fixture
def auth_header(make_token) -> Callable[..., dict]:
    def header(scopes=(), tenant: str | None = None, **overrides) -> dict:
        return {"Authorization": f"Bearer {make_token(scopes, tenant, **overrides)}"}

    return header


@pytest.fixture(scope="session")
def migrated() -> str:
    admin = _required(ADMIN_URL_ENV)
    _required(APP_URL_ENV)
    from asset_shells.cli import migrate

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLES}, alembic_version CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS asset_shells_reject_change() CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS asset_shells_type_submodel_immutable() CASCADE")
    migrate(database_url=admin, app_role="asset_shells_app")
    return admin


@pytest.fixture(scope="session")
def pool(migrated) -> Iterator[None]:
    from asset_shells import db

    db.open_pool(os.environ[APP_URL_ENV])
    yield
    db.close_pool()


@pytest.fixture
def admin_conn(migrated) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean_db(pool, migrated) -> None:
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")


@pytest.fixture
def client(clean_db) -> TestClient:
    from asset_shells.app import app

    return TestClient(app, raise_server_exceptions=False)
