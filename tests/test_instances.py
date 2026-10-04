"""POST /madfam/v1/instances and the append-only passport events."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from aas_fixtures import COMMONS_SHA, instance_env, passport_event, solid_type_env
from conftest import PUB_INST, PUB_TYPES, READ, TENANT_A

from asset_shells.ids import b64url_encode

UUID1 = "1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d"
TYPE_SHELL = solid_type_env()["assetAdministrationShells"][0]["id"]
INSTANCE_SHELL = f"https://id.madfam.io/aas/instance/{UUID1}"
RECORD = f"https://id.madfam.io/sm/instance/{UUID1}/ManufacturingRecord"


def codes(response) -> list[str]:
    return [m["code"] for m in response.json()["messages"]]


@pytest.fixture
def published_type(client, auth_header):
    response = client.put(
        f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA}",
        json={"environments": [solid_type_env()]},
        headers=auth_header((PUB_TYPES,)),
    )
    assert response.status_code == 201
    return TYPE_SHELL


def post_instance(client, auth_header, body, tenant=TENANT_A, scopes=(PUB_INST,)):
    return client.post("/madfam/v1/instances", json=body, headers=auth_header(scopes, tenant=tenant))


def post_event(client, auth_header, body, uuid_=UUID1, tenant=TENANT_A, scopes=(PUB_INST,)):
    return client.post(
        f"/madfam/v1/instances/{uuid_}/passport-events", json=body, headers=auth_header(scopes, tenant=tenant)
    )


def event_body(event_id: str | None = None, **kw) -> dict:
    return {
        "eventId": event_id or str(uuid.uuid4()),
        "submodelIdShort": "ManufacturingRecord",
        "event": passport_event(**kw),
    }


def test_instance_publish_create_replay_conflict(client, auth_header, published_type, admin_conn):
    body = instance_env(UUID1, published_type)
    first = post_instance(client, auth_header, body)
    assert first.status_code == 201, first.text
    assert first.headers["Location"] == f"/api/v3.1/shells/{b64url_encode(INSTANCE_SHELL)}"
    assert post_instance(client, auth_header, body).status_code == 200
    changed = instance_env(UUID1, published_type, serial="FD-0002")
    response = post_instance(client, auth_header, changed)
    assert response.status_code == 409 and codes(response) == ["identifier_unavailable"]

    shell = client.get(
        f"/api/v3.1/shells/{b64url_encode(INSTANCE_SHELL)}", headers=auth_header((READ,), tenant=TENANT_A)
    )
    assert shell.status_code == 200 and shell.json()["derivedFrom"]["keys"][0]["value"] == TYPE_SHELL


def test_instance_without_derived_from(client, auth_header):
    assert post_instance(client, auth_header, instance_env(UUID1, None)).status_code == 201


def test_unknown_derived_from_is_rejected(client, auth_header):
    response = post_instance(client, auth_header, instance_env(UUID1, TYPE_SHELL))
    assert response.status_code == 422 and codes(response) == ["unknown_derived_from"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda e: e["assetAdministrationShells"].append(
                instance_env("00000000-0000-4000-8000-000000000001", None)["assetAdministrationShells"][0]
            ),
            "environment",
        ),
        (
            lambda e: e.update(
                conceptDescriptions=[{"modelType": "ConceptDescription", "id": "https://id.madfam.io/concept/x"}]
            ),
            "environment",
        ),
        (
            lambda e: e["assetAdministrationShells"][0].update(id="https://id.madfam.io/aas/instance/NOT-A-UUID"),
            "id_scheme",
        ),
        (lambda e: e["assetAdministrationShells"][0]["assetInformation"].update(assetKind="Type"), "asset_kind"),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"].update(
                globalAssetId="https://id.madfam.io/asset/instance/00000000-0000-4000-8000-000000000000"
            ),
            "id_scheme",
        ),
        (
            lambda e: e["assetAdministrationShells"][0].update(
                derivedFrom={
                    "type": "ModelReference",
                    "keys": [{"type": "AssetAdministrationShell", "value": "urn:not-madfam"}],
                }
            ),
            "derived_from",
        ),
        (
            lambda e: e["submodels"][0].update(
                id="https://id.madfam.io/sm/instance/00000000-0000-4000-8000-000000000000/X1"
            ),
            "id_scheme",
        ),
        (lambda e: e["submodels"][0].update(idShort="Record"), "id_scheme"),
        (lambda e: e["assetAdministrationShells"][0].pop("submodels"), "orphan_submodel"),
        (lambda e: e.update(submodels=[]) or e.pop("submodels"), "dangling_reference"),
    ],
)
def test_instance_scheme_rules(client, auth_header, mutate, code):
    env = instance_env(UUID1, None)
    mutate(env)
    response = post_instance(client, auth_header, env)
    assert response.status_code == 422, response.text
    assert code in codes(response)


def test_instance_auth(client, auth_header):
    env = instance_env(UUID1, None)
    assert client.post("/madfam/v1/instances", json=env).status_code == 401
    assert post_instance(client, auth_header, env, scopes=(PUB_TYPES, READ)).status_code == 403
    response = post_instance(client, auth_header, env, tenant=None)
    assert response.status_code == 403 and codes(response) == ["missing_tenant"]
    response = post_instance(client, auth_header, env, tenant="bad tenant!")
    assert response.status_code == 403 and codes(response) == ["invalid_tenant"]


def test_passport_events_append_replay_and_read_back(client, auth_header, admin_conn):
    assert post_instance(client, auth_header, instance_env(UUID1, None)).status_code == 201
    e1 = event_body(event_type="print_started", at="2026-10-02T11:00:00Z")
    e2 = event_body(event_type="print_completed")
    r1 = post_event(client, auth_header, e1)
    assert r1.status_code == 201, r1.text
    assert r1.json()["position"] == 0 and r1.json()["idShortPath"] == "Events[0]"
    assert post_event(client, auth_header, e1).status_code == 200  # replay
    r2 = post_event(client, auth_header, e2)
    assert r2.status_code == 201 and r2.json()["position"] == 1

    reused = dict(e2, event=passport_event(event_type="tampered"))
    response = post_event(client, auth_header, reused)
    assert response.status_code == 409 and codes(response) == ["event_id_reused"]

    read = auth_header((READ,), tenant=TENANT_A)
    path = f"/api/v3.1/submodels/{b64url_encode(RECORD)}/submodel-elements/Events%5B1%5D/$value"
    value = client.get(path, headers=read)
    assert value.status_code == 200
    assert value.json()["EventType"] == "print_completed"

    listing = client.get(f"/madfam/v1/instances/{UUID1}/passport-events?limit=1", headers=read)
    assert listing.status_code == 200
    body = listing.json()
    assert len(body["result"]) == 1 and body["paging_metadata"]["cursor"]
    rest = client.get(
        f"/madfam/v1/instances/{UUID1}/passport-events?cursor={body['paging_metadata']['cursor']}", headers=read
    ).json()
    assert [r["position"] for r in rest["result"]] == [1] and rest["paging_metadata"] == {}

    topics = [r[0] for r in admin_conn.execute("SELECT topic FROM outbox ORDER BY id").fetchall()]
    assert topics == ["instance_shell.published", "passport_event.appended", "passport_event.appended"]


def test_passport_events_are_append_only_in_the_database(client, auth_header, migrated):
    assert post_instance(client, auth_header, instance_env(UUID1, None)).status_code == 201
    assert post_event(client, auth_header, event_body()).status_code == 201
    with psycopg.connect(migrated) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        # Layer 1: no UPDATE/DELETE policy exists, so even the owner (under FORCE RLS) reaches no row.
        assert conn.execute("UPDATE passport_events SET position = 5").rowcount == 0
        assert conn.execute("DELETE FROM passport_events").rowcount == 0
        conn.rollback()
        # Layer 2: with FORCE lifted inside a transaction, the append-only trigger still refuses.
        for statement in ("UPDATE passport_events SET position = 5", "DELETE FROM passport_events"):
            conn.execute("ALTER TABLE passport_events NO FORCE ROW LEVEL SECURITY")
            with pytest.raises(psycopg.errors.RestrictViolation):
                conn.execute(statement)
            conn.rollback()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ([], "body"),
        ({"eventId": "x", "submodelIdShort": "ManufacturingRecord", "event": passport_event()}, "event_id"),
        (
            {"eventId": str(uuid.uuid4()).upper(), "submodelIdShort": "ManufacturingRecord", "event": passport_event()},
            "event_id",
        ),
        ({"eventId": str(uuid.uuid4()), "submodelIdShort": "1bad", "event": passport_event()}, "submodel"),
        (
            {
                "eventId": str(uuid.uuid4()),
                "submodelIdShort": "ManufacturingRecord",
                "event": {"modelType": "Property", "valueType": "xs:string"},
            },
            "event",
        ),
        (
            {
                "eventId": str(uuid.uuid4()),
                "submodelIdShort": "ManufacturingRecord",
                "event": dict(passport_event(), idShort="E1"),
            },
            "event",
        ),
        (
            {
                "eventId": str(uuid.uuid4()),
                "submodelIdShort": "ManufacturingRecord",
                "event": dict(passport_event(), bogus=1),
            },
            "unknown_attribute",
        ),
        (
            {
                "eventId": str(uuid.uuid4()),
                "submodelIdShort": "ManufacturingRecord",
                "event": passport_event(),
                "extra": 1,
            },
            "body",
        ),
    ],
)
def test_passport_event_validation(client, auth_header, body, code):
    assert post_instance(client, auth_header, instance_env(UUID1, None)).status_code == 201
    response = post_event(client, auth_header, body)
    assert response.status_code == 422, response.text
    assert code in codes(response)


def test_passport_event_targets(client, auth_header):
    assert post_event(client, auth_header, event_body()).status_code == 404  # no such instance
    assert post_event(client, auth_header, event_body(), uuid_="NOT-A-UUID").status_code == 400
    env = instance_env(UUID1, None)
    env["submodels"][0]["submodelElements"] = env["submodels"][0]["submodelElements"][:3]
    assert post_instance(client, auth_header, env).status_code == 201
    response = post_event(client, auth_header, event_body())
    assert response.status_code == 422 and codes(response) == ["no_events_list"]
    response = post_event(client, auth_header, dict(event_body(), submodelIdShort="Nameplate"))
    assert response.status_code == 404


def test_passport_event_must_fit_the_list(client, auth_header):
    env = instance_env(UUID1, None)
    events = env["submodels"][0]["submodelElements"][3]
    events["semanticIdListElement"] = {
        "type": "ExternalReference",
        "keys": [{"type": "GlobalReference", "value": "https://id.madfam.io/concept/passport-event"}],
    }
    assert post_instance(client, auth_header, env).status_code == 201
    wrong = passport_event()
    wrong["semanticId"] = {
        "type": "ExternalReference",
        "keys": [{"type": "GlobalReference", "value": "https://id.madfam.io/concept/other"}],
    }
    response = post_event(
        client, auth_header, {"eventId": str(uuid.uuid4()), "submodelIdShort": "ManufacturingRecord", "event": wrong}
    )
    assert response.status_code == 422 and codes(response) == ["metamodel"]


def test_passport_event_auth(client, auth_header):
    assert client.post(f"/madfam/v1/instances/{UUID1}/passport-events", json=event_body()).status_code == 401
    assert post_event(client, auth_header, event_body(), scopes=(READ,)).status_code == 403
    assert post_event(client, auth_header, event_body(), tenant=None).status_code == 403
    assert (
        client.get(
            f"/madfam/v1/instances/{UUID1}/passport-events", headers=auth_header((PUB_INST,), tenant=TENANT_A)
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/madfam/v1/instances/{UUID1}/passport-events", headers=auth_header((READ,), tenant=TENANT_A)
        ).status_code
        == 404
    )
