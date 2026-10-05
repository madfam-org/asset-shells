"""Tenant isolation proofs: tenant B can neither read nor write tenant A's rows through any endpoint,
and the database enforces the same rules for the runtime role directly (row-level security, composite
tenant foreign keys, grants)."""

from __future__ import annotations

import json
import os
import uuid

import psycopg
import pytest
from aas_fixtures import COMMONS_SHA, instance_env, passport_event, solid_type_env
from conftest import APP_URL_ENV, PUB_INST, PUB_TYPES, READ, TENANT_A, TENANT_B

from asset_shells.ids import b64url_encode

UUID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
UUID_B = "bbbbbbbb-0000-4000-8000-00000000000b"
SHELL_A = f"https://id.madfam.io/aas/instance/{UUID_A}"
SHELL_B = f"https://id.madfam.io/aas/instance/{UUID_B}"
RECORD_A = f"https://id.madfam.io/sm/instance/{UUID_A}/ManufacturingRecord"
TYPE_ENV = solid_type_env()
TYPE_SHELL = TYPE_ENV["assetAdministrationShells"][0]["id"]


def enc(value: str) -> str:
    return b64url_encode(value)


@pytest.fixture
def world(client, auth_header):
    assert (
        client.put(
            f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA}",
            json={"environments": [solid_type_env()]},
            headers=auth_header((PUB_TYPES,)),
        ).status_code
        == 201
    )
    for tenant, uid, serial in ((TENANT_A, UUID_A, "A-1"), (TENANT_B, UUID_B, "B-1")):
        response = client.post(
            "/madfam/v1/instances",
            json=instance_env(uid, TYPE_SHELL, serial=serial),
            headers=auth_header((PUB_INST,), tenant=tenant),
        )
        assert response.status_code == 201, response.text
        event = {"eventId": str(uuid.uuid4()), "submodelIdShort": "ManufacturingRecord", "event": passport_event()}
        response = client.post(
            f"/madfam/v1/instances/{uid}/passport-events", json=event, headers=auth_header((PUB_INST,), tenant=tenant)
        )
        assert response.status_code == 201
    return {
        "A": auth_header((READ,), tenant=TENANT_A),
        "B": auth_header((READ,), tenant=TENANT_B),
        "no_scope": auth_header((PUB_INST,), tenant=TENANT_B),
        "no_tenant": auth_header((READ,)),
        "anon": {},
    }


INSTANCE_READS = [
    f"/api/v3.1/shells/{enc(SHELL_A)}",
    f"/api/v3.1/shells/{enc(SHELL_A)}/$reference",
    f"/api/v3.1/shells/{enc(SHELL_A)}/asset-information",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodel-refs",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodels/{enc(RECORD_A)}",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodels/{enc(RECORD_A)}/$value",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodels/{enc(RECORD_A)}/submodel-elements",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodels/{enc(RECORD_A)}/submodel-elements/Goc1InstanceId",
    f"/api/v3.1/shells/{enc(SHELL_A)}/submodels/{enc(RECORD_A)}/submodel-elements/Goc1InstanceId/$value",
    f"/api/v3.1/submodels/{enc(RECORD_A)}",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/$metadata",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/$value",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/$reference",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/submodel-elements",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/submodel-elements/Events%5B0%5D",
    f"/api/v3.1/submodels/{enc(RECORD_A)}/submodel-elements/Events%5B0%5D/$value",
    f"/api/v3.1/lookup/shells/{enc(SHELL_A)}",
]


@pytest.mark.parametrize("path", INSTANCE_READS)
def test_every_instance_read_is_tenant_scoped(client, world, path):
    assert client.get(path, headers=world["A"]).status_code == 200
    assert client.get(path, headers=world["B"]).status_code == 404  # existence not disclosed
    assert client.get(path, headers=world["anon"]).status_code == 401
    assert client.get(path, headers=world["no_scope"]).status_code == 403
    assert client.get(path, headers=world["no_tenant"]).status_code == 403


def _shell_ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {
        s["id"] if isinstance(s, dict) and "id" in s else (s["keys"][0]["value"] if isinstance(s, dict) else s)
        for s in response.json()["result"]
    }


@pytest.mark.parametrize("who", ["A", "B", "anon", "no_scope", "no_tenant"])
def test_listings_only_show_types_and_own_instances(client, world, who):
    expected = {TYPE_SHELL} | ({SHELL_A} if who == "A" else {SHELL_B} if who == "B" else set())
    headers = world[who]
    assert _shell_ids(client.get("/api/v3.1/shells", headers=headers)) == expected
    assert _shell_ids(client.get("/api/v3.1/shells/$reference", headers=headers)) == expected
    assert _shell_ids(client.get("/api/v3.1/lookup/shells", headers=headers)) == expected
    assert _shell_ids(client.post("/api/v3.1/lookup/shellsByAssetLink", json=[], headers=headers)) == expected
    by_idshort = client.get("/api/v3.1/shells?idShort=printed_part", headers=headers)
    assert _shell_ids(by_idshort) == expected - {TYPE_SHELL}
    submodels = client.get("/api/v3.1/submodels", headers=headers).json()["result"]
    instance_submodels = {s["id"] for s in submodels if "/sm/instance/" in s["id"]}
    assert instance_submodels == (
        {RECORD_A} if who == "A" else {RECORD_A.replace(UUID_A, UUID_B)} if who == "B" else set()
    )


def test_asset_lookup_does_not_cross_tenants(client, world):
    query = enc(json.dumps({"name": "globalAssetId", "value": f"https://id.madfam.io/asset/instance/{UUID_A}"}))
    assert client.get(f"/api/v3.1/lookup/shells?assetIds={query}", headers=world["A"]).json()["result"] == [SHELL_A]
    assert client.get(f"/api/v3.1/lookup/shells?assetIds={query}", headers=world["B"]).json()["result"] == []
    assert client.get(f"/api/v3.1/shells?assetIds={query}", headers=world["B"]).json()["result"] == []
    body = [{"name": "serial", "value": "A-1"}]
    assert client.post("/api/v3.1/lookup/shellsByAssetLink", json=body, headers=world["B"]).json()["result"] == []


def test_tenant_b_cannot_write_tenant_a(client, world, auth_header):
    b_pub = auth_header((PUB_INST,), tenant=TENANT_B)
    event = {"eventId": str(uuid.uuid4()), "submodelIdShort": "ManufacturingRecord", "event": passport_event()}
    assert client.post(f"/madfam/v1/instances/{UUID_A}/passport-events", json=event, headers=b_pub).status_code == 404
    hijack = client.post(
        "/madfam/v1/instances", json=instance_env(UUID_A, TYPE_SHELL, serial="B-HIJACK"), headers=b_pub
    )
    assert hijack.status_code == 409
    replay_as_b = client.post(
        "/madfam/v1/instances", json=instance_env(UUID_A, TYPE_SHELL, serial="A-1"), headers=b_pub
    )
    assert replay_as_b.status_code == 409  # identical content does not make A's row B's
    assert (
        client.get(
            f"/madfam/v1/instances/{UUID_A}/passport-events", headers=auth_header((READ,), tenant=TENANT_B)
        ).status_code
        == 404
    )
    links = client.get(f"/api/v3.1/lookup/shells/{enc(SHELL_A)}", headers=world["A"]).json()
    assert {"name": "serial", "value": "A-1"} in links
    events = client.get(f"/madfam/v1/instances/{UUID_A}/passport-events", headers=world["A"]).json()["result"]
    assert len(events) == 1


def test_event_id_of_another_tenant_cannot_be_reused(client, world, auth_header, admin_conn):
    with psycopg.connect(os.environ["ASSET_SHELLS_TEST_ADMIN_URL"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        event_id = str(conn.execute("SELECT event_id FROM passport_events").fetchone()[0])
    body = {"eventId": event_id, "submodelIdShort": "ManufacturingRecord", "event": passport_event()}
    response = client.post(
        f"/madfam/v1/instances/{UUID_B}/passport-events", json=body, headers=auth_header((PUB_INST,), tenant=TENANT_B)
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------------------------
# The database, as the runtime role, without the service in between
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def app_conn(world):
    with psycopg.connect(os.environ[APP_URL_ENV]) as conn:
        yield conn


def _as(conn, tenant: str | None) -> None:
    conn.rollback()
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant or "",))


def test_db_reads_are_filtered(app_conn):
    _as(app_conn, TENANT_B)
    ids = {r[0] for r in app_conn.execute("SELECT id FROM shells").fetchall()}
    assert ids == {TYPE_SHELL, SHELL_B}
    assert app_conn.execute("SELECT count(*) FROM passport_events WHERE shell_id = %s", (SHELL_A,)).fetchone()[0] == 0
    assert app_conn.execute("SELECT count(*) FROM asset_ids WHERE shell_id = %s", (SHELL_A,)).fetchone()[0] == 0
    _as(app_conn, None)
    assert {r[0] for r in app_conn.execute("SELECT id FROM shells").fetchall()} == {TYPE_SHELL}
    app_conn.rollback()
    # A connection that never set the tenant sees types only.
    assert {r[0] for r in app_conn.execute("SELECT id FROM shells").fetchall()} == {TYPE_SHELL}


def test_db_writes_are_checked(app_conn):
    row = ("https://id.madfam.io/aas/instance/cccccccc-0000-4000-8000-00000000000c", "instance")
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # writing a row for another tenant
        app_conn.execute(
            "INSERT INTO shells (id, kind, tenant_id, doc, content_sha256) VALUES (%s, %s, %s, '{}', %s)",
            (*row, TENANT_A, "0" * 64),
        )
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # a type row under tenant context
        app_conn.execute(
            "INSERT INTO shells (id, kind, tenant_id, doc, content_sha256) VALUES (%s, 'type', NULL, '{}', %s)",
            ("https://id.madfam.io/aas/solid/x/0000000000000000/p1", "0" * 64),
        )
    _as(app_conn, None)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # an instance row without tenant context
        app_conn.execute(
            "INSERT INTO shells (id, kind, tenant_id, doc, content_sha256) VALUES (%s, %s, %s, '{}', %s)",
            (*row, TENANT_B, "0" * 64),
        )
    _as(app_conn, TENANT_B)
    assert app_conn.execute("UPDATE submodels SET doc = '{}' WHERE id = %s", (RECORD_A,)).rowcount == 0
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # a child row pointing at A's shell
        app_conn.execute(
            "INSERT INTO submodels (id, shell_id, kind, tenant_id, doc, content_sha256) "
            "VALUES (%s, %s, 'instance', %s, '{}', %s)",
            (f"https://id.madfam.io/sm/instance/{UUID_A}/Injected", SHELL_A, TENANT_B, "0" * 64),
        )
    app_conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM shells",
        "DELETE FROM passport_events",
        "TRUNCATE submodels",
        "SELECT * FROM outbox",
        "UPDATE outbox SET published_at = now()",
        "UPDATE shells SET id_short = 'x'",
        "ALTER TABLE shells NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE shells DISABLE ROW LEVEL SECURITY",
    ],
)
def test_runtime_role_lacks_dangerous_privileges(app_conn, statement):
    _as(app_conn, TENANT_A)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)
    app_conn.rollback()
