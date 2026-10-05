"""Projection versions (hyperobjects-spec 0.6.0, owner decision 2026-10-04) end to end on PostgreSQL.

A type shell id names the design revision AND the keystone projection version (``…/{hex16}/p{N}``). Here the
keystone is patched from its own version V1 to V2 = V1 + 1 between two releases of the SAME design revisions, as a
keystone bump plus a commons repin would do: the new projection lands as new shells beside the old ones (no 409),
the same id with different bytes is still a 409, the graph and ``/validation`` work on both versions, an instance
matches its type by revision whatever the projection, and tenancy is unchanged.
"""

from __future__ import annotations

import base64
import copy
import json
import uuid

import hyperobjects_aas.ids as keystone_ids
import pytest
from aas_fixtures import COMMONS_SHA, COMMONS_SHA_2, instance_env, material_type_env
from assembly_fixtures import (
    BASE,
    CARTRIDGES,
    COMMONS,
    DIGESTS,
    A,
    B,
    assembly_doc,
    assembly_env,
    cartridge_envs,
    component_nodes,
    instance_assembly_env,
    instance_node,
    shell_id,
)
from conftest import PUB_INST, PUB_TYPES, READ, TENANT_A, TENANT_B
from hyperobjects_aas import build_material_environment, build_solid_environment
from hyperobjects_aas.assembly import build_assembly_environment
from hyperobjects_aas.resolver import bundled_standard_parts_dir
from hyperobjects_schemas.generator_output import normalize_numbers
from y4d_spec.assembly import CompositeResolver, validate_assembly

from asset_shells.ids import b64url_encode

#: The keystone's own projection version (V1) and the bump these tests simulate (V2 = V1 + 1). 0.6.0 wrote 1,
#: 0.7.0 (ASM-1 §9, the Kinematics submodel) writes 2; the tests hold for any pin.
V1 = keystone_ids.PROJECTION_VERSION
V2 = V1 + 1
COMMONS_SHA_3 = "3" * 40
COMMONS_SHA_4 = "4" * 40
ASSET_A = f"{BASE}asset/assembly/{A}"
ASSET_B = f"{BASE}asset/assembly/{B}"
POD = f"{BASE}asset/solid/motor-soft-mount"


def enc(value: str) -> str:
    return b64url_encode(value)


def put_release(client, auth_header, envs, sha, commons="solid-hyperobjects"):
    return client.put(
        f"/madfam/v1/type-environments/{commons}/{sha}",
        json={"environments": envs},
        headers=auth_header((PUB_TYPES,)),
    )


def _envs_at_current_version() -> tuple[dict[str, dict], dict[str, dict]]:
    """Cartridge and assembly environments built NOW, at whatever version the keystone is patched to."""
    cartridges = {slug: build_solid_environment(COMMONS / slug) for slug in CARTRIDGES}
    resolver = CompositeResolver.for_directories(COMMONS, bundled_standard_parts_dir())
    assemblies = {}
    for slug in (A, B):
        report = validate_assembly(assembly_doc(slug), resolver)
        assemblies[slug] = build_assembly_environment(assembly_doc(slug), report)
    return cartridges, assemblies


@pytest.fixture
def two_versions(client, auth_header, monkeypatch):
    """Version V1 (components, then A and B), then the keystone bumped to V2 and the same revisions republished."""
    first = put_release(client, auth_header, list(cartridge_envs().values()), COMMONS_SHA)
    assert first.status_code == 201, first.text
    second = put_release(client, auth_header, [assembly_env(A), assembly_env(B)], COMMONS_SHA_2)
    assert second.status_code == 201, second.text
    monkeypatch.setattr(keystone_ids, "PROJECTION_VERSION", V2)
    cartridges, assemblies = _envs_at_current_version()
    third = put_release(client, auth_header, list(cartridges.values()), COMMONS_SHA_3)
    assert third.status_code == 201, third.text
    fourth = put_release(client, auth_header, list(assemblies.values()), COMMONS_SHA_4)
    assert fourth.status_code == 201, fourth.text
    return {"v1": second.json(), "v2": fourth.json(), "v2_types": third.json(), "envs": (cartridges, assemblies)}


def _v(shell: str, n: int) -> str:
    return shell.rsplit("/p", 1)[0] + f"/p{n}"


# ── publishing ─────────────────────────────────────────────────────────────────
def test_a_new_projection_of_a_stored_revision_is_a_new_shell(two_versions, admin_conn):
    v1_a = f"{BASE}aas/assembly/{A}/{DIGESTS[A][:16]}/p{V1}"
    assert v1_a in two_versions["v1"]["shells"]["created"]
    v2_b = f"{BASE}aas/assembly/{B}/{DIGESTS[B][:16]}/p{V2}"
    assert set(two_versions["v2"]["shells"]["created"]) == {_v(v1_a, V2), v2_b}
    assert two_versions["v2"]["shells"]["unchanged"] == []
    assert len(two_versions["v2_types"]["shells"]["created"]) == len(CARTRIDGES)
    rows = admin_conn.execute("SELECT id FROM shells WHERE global_asset_id = %s ORDER BY seq", (ASSET_A,)).fetchall()
    assert [r[0] for r in rows] == [v1_a, _v(v1_a, V2)]
    stored = admin_conn.execute("SELECT doc FROM shells WHERE id = %s", (_v(v1_a, V2),)).fetchone()[0]
    assert stored["extensions"] == [{"name": "ProjectionVersion", "valueType": "xs:positiveInteger", "value": str(V2)}]
    # Edges are asset-to-asset and the same at both versions; via_shell_id names the versioned row.
    by_shell = admin_conn.execute(
        "SELECT via_shell_id, count(*), array_agg(DISTINCT props->>'typeShell') FILTER (WHERE props ? 'typeShell') "
        "FROM asset_edges WHERE from_asset_id = %s AND kind = 'has_part' GROUP BY via_shell_id ORDER BY 1",
        (ASSET_A,),
    ).fetchall()
    assert [(r[0], r[1]) for r in by_shell] == [(v1_a, 15), (_v(v1_a, V2), 15)]
    assert all(t.endswith(f"/p{V1}") for t in by_shell[0][2]) and all(t.endswith(f"/p{V2}") for t in by_shell[1][2])


def test_the_same_id_with_different_bytes_is_still_409(two_versions, client, auth_header, monkeypatch):
    cartridges, _ = two_versions["envs"]
    env = copy.deepcopy(cartridges["tslot-corner"])
    env["assetAdministrationShells"][0]["idShort"] = "changed"
    response = put_release(client, auth_header, [env], "5" * 40)
    assert response.status_code == 409, response.text
    assert {m["code"] for m in response.json()["messages"]} == {"immutable"}
    # A version-V1 shell republished unchanged next to version V2 is simply "unchanged".
    monkeypatch.setattr(keystone_ids, "PROJECTION_VERSION", V1)
    again = put_release(client, auth_header, [cartridge_envs()["tslot-corner"]], "6" * 40)
    assert again.status_code == 201 and again.json()["shells"]["created"] == []


def test_an_assembly_at_another_version_than_the_keystone_is_422(two_versions, client, auth_header):
    """The service's keystone projects version V2 now: a version-V1 assembly cannot be (re)published as new."""
    env = assembly_env(A)
    doc = copy.deepcopy(env)
    old, new = doc["assetAdministrationShells"][0]["id"], f"{BASE}aas/assembly/{A}/{'0' * 16}/p{V1}"
    text = (
        json.dumps(doc)
        .replace(old, new)
        .replace(f"{BASE}sm/assembly/{A}/{DIGESTS[A][:16]}/p{V1}/", f"{BASE}sm/assembly/{A}/{'0' * 16}/p{V1}/")
    )
    doc = json.loads(text)
    for s in doc["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"]:
        if s["name"] == "assembly_digest":
            s["value"] = "0" * 64
    response = put_release(client, auth_header, [doc], "7" * 40)
    assert response.status_code == 422, response.text
    assert {m["code"] for m in response.json()["messages"]} == {"assembly_projection_version"}


def test_material_cards_version_too(client, auth_header):
    first = put_release(client, auth_header, [material_type_env(projection=1)], COMMONS_SHA)
    second = put_release(client, auth_header, [material_type_env(projection=2)], COMMONS_SHA_2)
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert second.json()["shells"]["created"][0].endswith("/p2")


def test_a_respelt_material_card_is_unchanged_not_a_409(client, auth_header, admin_conn):
    """One content hash, one projection (hyperobjects-spec 0.10.0, projection version 3). The shell id hashes the
    card's canonical JSON, where 220.0 and 220 are one number, and the keystone now projects that canonical form.
    So a card that a JSON.stringify round trip respells has the same id AND the same bytes: the second release
    stores nothing new. Before version 3 the keystone wrote xs:double for 220.0 and xs:integer for 220 under one
    id, and this release was a 409."""
    card = {
        "material": {"slug": "p3-tpu", "name": {"en": "P3 TPU"}, "category": "tpu"},
        "thermodynamics": {"melting_temp": 220.0, "glass_transition_temp": -30.0, "density": 1.21},
    }
    respelt = normalize_numbers(card)
    melting = respelt["thermodynamics"]["melting_temp"]
    assert melting == 220 and isinstance(melting, int)
    as_written, as_respelt = build_material_environment(card), build_material_environment(respelt)
    shell = as_written["assetAdministrationShells"][0]["id"]
    assert shell == as_respelt["assetAdministrationShells"][0]["id"] and shell.endswith(f"/p{V1}")

    first = put_release(client, auth_header, [as_written], COMMONS_SHA)
    second = put_release(client, auth_header, [as_respelt], COMMONS_SHA_2)
    assert first.status_code == 201 and first.json()["shells"]["created"] == [shell], first.text
    assert second.status_code == 201 and second.json()["shells"]["created"] == [], second.text

    (stored,) = admin_conn.execute(
        "SELECT doc FROM submodels WHERE id = %s", (as_written["submodels"][0]["id"],)
    ).fetchone()
    props = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("modelType") == "Property":
                props[node.get("idShort")] = (node.get("valueType"), node.get("value"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(stored)
    assert props["melting_temp"] == ("xs:integer", "220")
    assert props["glass_transition_temp"] == ("xs:integer", "-30")
    assert props["density"] == ("xs:double", "1.21")  # only whole numbers follow their canonical value


# ── reading ────────────────────────────────────────────────────────────────────
def test_the_graph_walk_sees_both_versions(two_versions, client):
    body = client.get("/madfam/v1/graph", params={"root": enc(ASSET_A), "kinds": "has_part"}).json()
    via = {}
    for e in body["edges"]:
        via.setdefault(e["viaShellId"], []).append((e["from"], e["to"], e["kind"], e["props"].get("componentId")))
    v1 = f"{BASE}aas/assembly/{A}/{DIGESTS[A][:16]}/p{V1}"
    assert set(via) == {v1, _v(v1, V2)}
    assert sorted(via[v1]) == sorted(via[_v(v1, V2)]) and len(via[v1]) == 15  # one per component
    (root,) = [n for n in body["nodes"] if n["assetId"] == ASSET_A]
    assert [s["id"] for s in root["shells"]] == [v1, _v(v1, V2)]


def test_validation_defaults_to_the_current_projection_and_reads_older_ones(two_versions, client):
    url = f"/madfam/v1/assemblies/{enc(ASSET_B)}/validation"
    v1 = f"{BASE}aas/assembly/{B}/{DIGESTS[B][:16]}/p{V1}"
    current = client.get(url).json()
    assert current["ok"] is True and current["shellId"] == _v(v1, V2)
    assert current["projection"] == {"shell": V2, "keystone": V2, "compared": True}
    assert current["revisions"] == [v1, _v(v1, V2)]
    older = client.get(url, params={"projection": V1}).json()
    assert older["ok"] is True and older["shellId"] == v1, older
    assert older["projection"] == {"shell": V1, "keystone": V2, "compared": False}
    assert older["report"]["digest"] == current["report"]["digest"] == DIGESTS[B]
    pinned = client.get(url, params={"revision": DIGESTS[B][:16], "projection": V2}).json()
    assert pinned["shellId"] == _v(v1, V2)
    assert client.get(url, params={"projection": V2 + 1}).status_code == 404
    assert client.get(url, params={"projection": 0}).status_code == 400


def test_projections_endpoint_lists_versions_and_the_current_one(two_versions, client):
    body = client.get(f"/madfam/v1/assets/{enc(POD)}/projections").json()
    (revision,) = body["revisions"]
    assert body["assetId"] == POD and body["keystoneProjection"] == V2
    assert [p["version"] for p in revision["projections"]] == [V1, V2]
    assert revision["current"] == revision["projections"][-1]["shellId"]
    assert revision["current"].endswith(f"/{revision['revision']}/p{V2}")
    assembly = client.get(f"/madfam/v1/assets/{enc(ASSET_A)}/projections").json()
    assert assembly["revisions"][0]["revision"] == DIGESTS[A][:16]
    assert client.get(f"/madfam/v1/assets/{enc(f'{BASE}asset/solid/nope')}/projections").status_code == 404
    instance = f"{BASE}asset/instance/{uuid.uuid4()}"
    assert client.get(f"/madfam/v1/assets/{enc(instance)}/projections").status_code == 400
    raw = base64.urlsafe_b64encode(b"\xff").decode().rstrip("=")
    assert client.get(f"/madfam/v1/assets/{raw}/projections").status_code == 400


# ── instances and tenancy ──────────────────────────────────────────────────────
def test_an_instance_matches_its_type_by_revision_whatever_the_projection(two_versions, client, auth_header):
    """Type assembly B at version V2; its cartridge instances derived from the version-V1 type shells."""
    _, assemblies = two_versions["envs"]
    type_env = assemblies[B]
    assert shell_id(type_env).endswith(f"/p{V2}")
    nodes = {}
    for cid, node in component_nodes(type_env).items():
        revision = next(
            (s["value"]["keys"][-1]["value"] for s in node["statements"] if s["idShort"] == "DerivedFrom"), None
        )
        if revision is None:
            nodes[cid] = instance_node(cid, asset=node.get("globalAssetId"))
            continue
        assert revision.endswith(f"/p{V2}")
        part = str(uuid.uuid5(uuid.NAMESPACE_URL, f"p4pver/{cid}"))
        made = client.post(
            "/madfam/v1/instances",
            json=instance_env(part, _v(revision, V1), serial=cid),
            headers=auth_header((PUB_INST,), tenant=TENANT_A),
        )
        assert made.status_code == 201, made.text
        nodes[cid] = instance_node(cid, asset=f"{BASE}asset/instance/{part}")
    assembly_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "p4pver/assembly"))
    response = client.post(
        "/madfam/v1/instances",
        json=instance_assembly_env(assembly_uuid, type_env, nodes),
        headers=auth_header((PUB_INST,), tenant=TENANT_A),
    )
    assert response.status_code == 201, response.text
    instance = f"{BASE}asset/instance/{assembly_uuid}"
    reader_a, reader_b = auth_header((READ,), tenant=TENANT_A), auth_header((READ,), tenant=TENANT_B)
    ok = client.get(f"/madfam/v1/assemblies/{enc(instance)}/validation", headers=reader_a).json()
    assert ok["ok"] is True and ok["type"]["projection"]["shell"] == V2, ok
    # Tenancy is unchanged: another tenant sees neither the instance nor its graph.
    assert client.get(f"/madfam/v1/assemblies/{enc(instance)}/validation", headers=reader_b).status_code == 404
    assert client.get("/madfam/v1/graph", params={"root": enc(instance)}, headers=reader_b).status_code == 404
    assert client.get(f"/madfam/v1/assets/{enc(instance)}/projections", headers=reader_a).status_code == 400
