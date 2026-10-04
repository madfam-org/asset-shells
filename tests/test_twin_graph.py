"""The twin graph (ASM-1 §6) end to end on PostgreSQL, as the runtime role.

Assemblies A (Voron 2.4-class motion frame) and B (5-inch FPV quad) are published after their component
type shells; the keystone re-validates each against the stored shells; the edges land in the publish
transaction; the graph is walked both ways at several depths; a failing assembly is a 422 that carries the
report; an instance assembly must match its type; and no tenant sees another tenant's instance graph.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import uuid

import psycopg
import pytest
from aas_fixtures import COMMONS_SHA, COMMONS_SHA_2, instance_env
from assembly_fixtures import (
    BASE,
    DIGESTS,
    A,
    B,
    assembly_env,
    cartridge_envs,
    child,
    component_nodes,
    instance_assembly_env,
    instance_node,
    shell_id,
    submodel,
)
from conftest import APP_URL_ENV, PUB_INST, PUB_TYPES, READ, TENANT_A, TENANT_B

from asset_shells.ids import b64url_encode

ASSET_A = f"{BASE}asset/assembly/{A}"
ASSET_B = f"{BASE}asset/assembly/{B}"
FRAME = f"{BASE}asset/standard/fpv-frame-5in-x-225"
MOTOR = f"{BASE}asset/standard/motor-2207"
POD = f"{BASE}asset/solid/motor-soft-mount"
TOOLHEAD = "https://github.com/VoronDesign/Voron-Stealthburner"


def enc(value: str) -> str:
    return b64url_encode(value)


def put_release(client, auth_header, envs, sha=COMMONS_SHA):
    return client.put(
        f"/madfam/v1/type-environments/solid-hyperobjects/{sha}",
        json={"environments": envs},
        headers=auth_header((PUB_TYPES,)),
    )


def graph(client, root, headers=None, **params):
    query = {"root": enc(root), **params}
    return client.get("/madfam/v1/graph", params=query, headers=headers or {})


def edge_set(body, kind=None):
    return [(e["from"], e["to"], e["kind"]) for e in body["edges"] if kind is None or e["kind"] == kind]


@pytest.fixture
def published(client, auth_header):
    """The component type shells (release 1), then assemblies A and B (release 2)."""
    first = put_release(client, auth_header, list(cartridge_envs().values()))
    assert first.status_code == 201, first.text
    assert first.json()["edges"] == 0  # these cartridges carry no hardware BoM
    second = put_release(client, auth_header, [assembly_env(A), assembly_env(B)], COMMONS_SHA_2)
    assert second.status_code == 201, second.text
    return second.json()


# ── publish-time validation and the edges it writes ───────────────────────────
def test_assemblies_publish_after_their_components_with_edges(published, admin_conn):
    assert sorted(published["shells"]["created"]) == sorted(
        [f"{BASE}aas/assembly/{A}/{DIGESTS[A][:16]}", f"{BASE}aas/assembly/{B}/{DIGESTS[B][:16]}"]
    )
    # A: 15 components + 15 mates; B: 13 + 13.
    assert published["edges"] == 15 + 15 + 13 + 13
    rows = admin_conn.execute(
        "SELECT kind, count(*), bool_and(tenant_id IS NULL) FROM asset_edges GROUP BY kind ORDER BY kind"
    ).fetchall()
    assert rows == [("has_part", 28, True), ("mates_with", 28, True)]


def test_a_replayed_release_writes_no_edges(published, client, auth_header, admin_conn):
    again = put_release(client, auth_header, [assembly_env(A), assembly_env(B)], COMMONS_SHA_2)
    assert again.status_code == 200 and again.json()["edges"] == 0
    assert admin_conn.execute("SELECT count(*) FROM asset_edges").fetchone()[0] == 56


def test_components_and_assembly_in_one_release(client, auth_header):
    envs = [*cartridge_envs().values(), assembly_env(B)]
    response = put_release(client, auth_header, envs)
    assert response.status_code == 201, response.text
    assert response.json()["edges"] == 26


def test_an_assembly_before_its_components_is_422_with_the_report(client, auth_header, admin_conn):
    response = put_release(client, auth_header, [assembly_env(B)])
    assert response.status_code == 422
    body = response.json()
    assert {m["code"] for m in body["messages"]} == {"assembly_invalid"}
    assert any("is not published" in m["text"] for m in body["messages"])
    (report,) = body["assemblyReports"]
    assert report["ok"] is False and report["report"]["digest"] is None
    assert {e["subject"] for e in report["report"]["errors"] if e["code"] == "resolve"} == {
        "pod_fl",
        "pod_fr",
        "pod_rl",
        "pod_rr",
        "fc_standoffs",
        "battery_pad",
        "camera_cage",
    }
    assert admin_conn.execute("SELECT count(*) FROM shells").fetchone()[0] == 0  # nothing written


def _with_document(env: dict, mutate) -> dict:
    env = copy.deepcopy(env)
    blob = child(submodel(env, "AssemblyDocument"), "Document")
    doc = json.loads(base64.b64decode(blob["value"]))
    mutate(doc)
    blob["value"] = base64.b64encode(json.dumps(doc).encode()).decode()
    return env


def test_a_failing_assembly_is_422_with_the_closure_report(client, auth_header):
    assert put_release(client, auth_header, list(cartridge_envs().values())).status_code == 201

    def wrong_angle(doc):
        doc["mates"][11]["angle_deg"] = 40  # the cage's closing ear mate, against the geometry

    response = put_release(client, auth_header, [_with_document(assembly_env(B), wrong_angle)], COMMONS_SHA_2)
    assert response.status_code == 422
    (report,) = response.json()["assemblyReports"]
    (error,) = report["report"]["errors"]
    assert error["code"] == "closure" and error["subject"] == "cage_ear_right_on_plate"
    assert "stated angle_deg 40° but the geometry realises 0°" in error["message"]
    closing = next(m for m in report["report"]["mates"] if m["mate"] == "cage_ear_right_on_plate")
    assert closing["ok"] is False and closing["x_axis_deg"] == pytest.approx(40)


def test_a_published_projection_that_disagrees_with_its_document_is_422(client, auth_header):
    assert put_release(client, auth_header, list(cartridge_envs().values())).status_code == 201
    env = assembly_env(B)
    mates = submodel(env, "Mates")
    note = next(a for a in mates["submodelElements"][0]["annotations"] if a["idShort"] == "InterfaceB")
    note["value"] = "rigid_clamp"  # the document still says arm_clamp
    response = put_release(client, auth_header, [env], COMMONS_SHA_2)
    assert response.status_code == 422
    texts = [m["text"] for m in response.json()["messages"]]
    assert texts == [f"submodel '{mates['id']}' is different: it is not the keystone projection of the document"]


def test_an_assembly_whose_component_revision_is_unknown_is_422(client, auth_header):
    envs = cartridge_envs()
    del envs["battery-pad"]
    assert put_release(client, auth_header, list(envs.values())).status_code == 201
    response = put_release(client, auth_header, [assembly_env(B)], COMMONS_SHA_2)
    assert response.status_code == 422
    assert any("battery_pad" in m["text"] and "is not published" in m["text"] for m in response.json()["messages"])


# ── the walk ─────────────────────────────────────────────────────────────────
def test_down_from_an_assembly_at_depth_1_is_its_bill_of_materials(published, client):
    body = graph(client, ASSET_A, direction="down", depth=1).json()
    assert body["root"] == ASSET_A and body["depth"] == 1
    has_part = edge_set(body, "has_part")
    assert len(has_part) == len(body["edges"]) == 15
    targets = {t for _f, t, _k in has_part}
    assert TOOLHEAD in targets  # the external GPL design is a node by its URL
    assert sum(1 for _f, t, _k in has_part if t == f"{BASE}asset/standard/extrusion-2020") == 3
    root = body["nodes"][0]
    assert root["assetId"] == ASSET_A and root["depth"] == 0
    assert root["shells"] == [{"id": f"{BASE}aas/assembly/{A}/{DIGESTS[A][:16]}", "kind": "type"}]
    assert len(body["nodes"]) == 1 + len(targets) == 13
    pod = next(e for e in body["edges"] if e["props"].get("componentId") == "motor_bracket_a")
    assert pod["props"]["typeShell"].startswith(f"{BASE}aas/solid/nema-bracket/")
    assert pod["viaSubmodelId"].endswith("/BillOfMaterials")


def test_down_at_depth_2_adds_the_mates_of_the_components(published, client):
    body = graph(client, ASSET_A, direction="down", depth=2).json()
    assert len(edge_set(body, "has_part")) == 15
    assert len(edge_set(body, "mates_with")) == 15
    assert {e["depth"] for e in body["edges"]} == {1, 2}
    only_mates = graph(client, ASSET_A, direction="down", depth=2, kinds="mates_with").json()
    assert only_mates["edges"] == []  # an assembly is not itself mated to anything


def test_down_from_the_fpv_frame_follows_its_mates(published, client):
    body = graph(client, FRAME, direction="down", depth=1, kinds="mates_with").json()
    targets = sorted(t for _f, t, _k in edge_set(body))
    assert targets.count(POD) == 4
    assert targets.count(f"{BASE}asset/solid/fpv-camera-cage") == 2  # both ears, on the outer faces
    ears = [e for e in body["edges"] if e["props"]["mateId"].startswith("cage_ear")]
    assert {e["props"]["angleDeg"] for e in ears} == {"0"}
    deeper = graph(client, FRAME, direction="down", depth=2, kinds="mates_with").json()
    assert sorted(t for _f, t, _k in edge_set(deeper) if _f == POD) == [MOTOR] * 4
    assert {n["assetId"]: n["depth"] for n in deeper["nodes"]}[MOTOR] == 2


def test_up_walks_from_a_part_to_the_assemblies_that_use_it(published, client):
    body = graph(client, MOTOR, direction="up", depth=1, kinds="mates_with").json()
    assert edge_set(body) == [(POD, MOTOR, "mates_with")] * 4
    two = graph(client, MOTOR, direction="up", depth=2, kinds="mates_with").json()
    assert {f for f, _t, _k in edge_set(two)} == {POD, FRAME}
    used_in = graph(client, POD, direction="up", depth=1, kinds="has_part").json()
    assert edge_set(used_in) == [(ASSET_B, POD, "has_part")] * 4
    everything = graph(client, MOTOR, direction="up", depth=3).json()
    assert ASSET_B in {n["assetId"] for n in everything["nodes"]}
    assert ASSET_A not in {n["assetId"] for n in everything["nodes"]}


def test_graph_parameters_are_validated(published, client):
    assert graph(client, ASSET_A, depth=9).status_code == 400
    assert graph(client, ASSET_A, depth=0).status_code == 400
    assert graph(client, ASSET_A, direction="sideways").status_code == 400
    assert graph(client, ASSET_A, kinds="has_part,owns").status_code == 400
    assert client.get("/madfam/v1/graph", params={"root": "%%%"}).status_code == 400
    assert graph(client, f"{BASE}asset/assembly/no-such-thing").status_code == 404
    plain = client.get("/madfam/v1/graph", params={"root": ASSET_B})
    assert plain.status_code == 200 and len(plain.json()["edges"]) == 13


# ── the validation endpoint ──────────────────────────────────────────────────
def test_validation_of_a_stored_type_assembly(published, client):
    response = client.get(f"/madfam/v1/assemblies/{enc(ASSET_B)}/validation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["problems"] == []
    assert body["shellId"] == f"{BASE}aas/assembly/{B}/{DIGESTS[B][:16]}"
    assert body["report"]["digest"] == DIGESTS[B]
    assert len(body["report"]["mates"]) == 13 and all(m["ok"] for m in body["report"]["mates"])
    assert body["keystone"]
    by_revision = client.get(f"/madfam/v1/assemblies/{enc(ASSET_B)}/validation", params={"revision": DIGESTS[B][:16]})
    assert by_revision.status_code == 200
    assert (
        client.get(f"/madfam/v1/assemblies/{enc(ASSET_B)}/validation", params={"revision": "0" * 16}).status_code == 404
    )
    assert client.get(f"/madfam/v1/assemblies/{enc(POD)}/validation").status_code == 404


# ── instance assemblies and tenancy ──────────────────────────────────────────
def _uuid(tag: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"p4graph/{tag}"))


def _publish_instance(client, auth_header, env, tenant):
    return client.post("/madfam/v1/instances", json=env, headers=auth_header((PUB_INST,), tenant=tenant))


def _instance_of_b(client, auth_header, tenant, tag, *, skip=(), wrong=()):
    """Publish one instance per cartridge component of B (derivedFrom its type revision), then the instance
    assembly. Returns (response, assembly uuid)."""
    type_env = assembly_env(B)
    nodes = {}
    for cid, node in component_nodes(type_env).items():
        revision = next(
            (s["value"]["keys"][-1]["value"] for s in node["statements"] if s["idShort"] == "DerivedFrom"), None
        )
        if revision is None:
            nodes[cid] = instance_node(cid, asset=node.get("globalAssetId"))
            continue
        if cid in skip:
            continue
        part_uuid = _uuid(f"{tag}/{cid}")
        derived = shell_id(cartridge_envs()["battery-pad"]) if cid in wrong else revision
        made = _publish_instance(client, auth_header, instance_env(part_uuid, derived, serial=cid), tenant)
        assert made.status_code == 201, made.text
        nodes[cid] = instance_node(cid, asset=f"{BASE}asset/instance/{part_uuid}")
    assembly_uuid = _uuid(f"{tag}/assembly")
    env = instance_assembly_env(assembly_uuid, type_env, nodes)
    return _publish_instance(client, auth_header, env, tenant), assembly_uuid


def test_an_instance_assembly_matches_its_type_and_gets_edges(published, client, auth_header, admin_conn):
    response, uid = _instance_of_b(client, auth_header, TENANT_A, "a1")
    assert response.status_code == 201, response.text
    with admin_conn.transaction():  # even the owner sees instance rows only in their tenant's context
        admin_conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_A,))
        rows = admin_conn.execute(
            "SELECT kind, count(*) FROM asset_edges WHERE via_shell_id = %s GROUP BY kind ORDER BY kind",
            (f"{BASE}aas/instance/{uid}",),
        ).fetchall()
        derived = admin_conn.execute(
            "SELECT count(*) FROM asset_edges WHERE tenant_id = %s AND kind = 'derived_from'", (TENANT_A,)
        ).fetchone()[0]
    assert rows == [("derived_from", 1), ("has_part", 13), ("mates_with", 13)]
    assert derived == 8  # seven component instances + the assembly


def test_an_instance_assembly_missing_a_component_is_422(published, client, auth_header):
    response, _ = _instance_of_b(client, auth_header, TENANT_A, "a2", skip=("battery_pad",))
    assert response.status_code == 422
    assert [m["text"] for m in response.json()["messages"]] == [
        "component 'battery_pad' of the type assembly is missing"
    ]


def test_an_instance_component_of_the_wrong_type_is_422(published, client, auth_header):
    response, _ = _instance_of_b(client, auth_header, TENANT_A, "a3", wrong=("pod_fl",))
    assert response.status_code == 422
    (message,) = response.json()["messages"]
    assert "component 'pod_fl'" in message["text"] and "not the type component" in message["text"]


def test_a_tenant_cannot_see_another_tenants_instance_graph(published, client, auth_header):
    response, uid = _instance_of_b(client, auth_header, TENANT_A, "a4")
    assert response.status_code == 201
    instance = f"{BASE}asset/instance/{uid}"
    reader_a = auth_header((READ,), tenant=TENANT_A)
    reader_b = auth_header((READ,), tenant=TENANT_B)

    mine = graph(client, instance, reader_a, depth=2)
    assert mine.status_code == 200
    kinds = {e["kind"] for e in mine.json()["edges"]}
    assert kinds == {"has_part", "mates_with", "derived_from"}
    assert all(e["instance"] for e in mine.json()["edges"] if e["depth"] == 1)
    assert any(not e["instance"] for e in mine.json()["edges"] if e["depth"] == 2)  # the public type graph

    assert graph(client, instance, reader_b, depth=2).status_code == 404  # existence is not disclosed
    assert graph(client, instance).status_code == 401
    assert graph(client, instance, auth_header((PUB_INST,), tenant=TENANT_A)).status_code == 403

    # From the type side, B sees the public type graph only; A also sees its own instance hanging off it.
    up_a = graph(client, ASSET_B, reader_a, direction="up", depth=1, kinds="derived_from").json()
    up_b = graph(client, ASSET_B, reader_b, direction="up", depth=1, kinds="derived_from").json()
    assert edge_set(up_a) == [(instance, ASSET_B, "derived_from")]
    assert up_b["edges"] == []
    anonymous = graph(client, POD, direction="up", depth=1).json()
    assert all(not e["instance"] for e in anonymous["edges"])

    ok_a = client.get(f"/madfam/v1/assemblies/{enc(instance)}/validation", headers=reader_a)
    assert ok_a.status_code == 200 and ok_a.json()["ok"] is True, ok_a.text
    assert ok_a.json()["instance"] == {"ok": True, "problems": []}
    assert client.get(f"/madfam/v1/assemblies/{enc(instance)}/validation", headers=reader_b).status_code == 404


# ── the database, as the runtime role ────────────────────────────────────────
@pytest.fixture
def app_conn(published, client, auth_header):
    response, uid = _instance_of_b(client, auth_header, TENANT_A, "db")
    assert response.status_code == 201
    with psycopg.connect(os.environ[APP_URL_ENV]) as conn:
        yield conn, f"{BASE}aas/instance/{uid}"


def _as(conn, tenant):
    conn.rollback()
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant or "",))


def test_db_edges_are_filtered_and_checked(app_conn):
    app_conn, shell_a = app_conn
    _as(app_conn, TENANT_B)
    assert app_conn.execute("SELECT count(*) FROM asset_edges WHERE tenant_id IS NOT NULL").fetchone()[0] == 0
    assert app_conn.execute("SELECT count(*) FROM asset_edges").fetchone()[0] == 56  # the type graph only
    _as(app_conn, TENANT_A)
    assert app_conn.execute("SELECT count(*) FROM asset_edges WHERE via_shell_id = %s", (shell_a,)).fetchone()[0] == 27
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # an edge written for another tenant
        app_conn.execute(
            "INSERT INTO asset_edges (from_asset_id, to_asset_id, kind, via_shell_id, position, tenant_id) "
            "VALUES ('x', 'y', 'has_part', %s, 99, %s)",
            (shell_a, TENANT_A),
        )
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # an edge of B hung on A's shell
        app_conn.execute(
            "INSERT INTO asset_edges (from_asset_id, to_asset_id, kind, via_shell_id, position, tenant_id) "
            "VALUES ('x', 'y', 'has_part', %s, 99, %s)",
            (shell_a, TENANT_B),
        )
    _as(app_conn, TENANT_B)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # a type edge under tenant context
        app_conn.execute(
            "INSERT INTO asset_edges (from_asset_id, to_asset_id, kind, via_shell_id, position) "
            "VALUES ('x', 'y', 'has_part', %s, 99)",
            (f"{BASE}aas/assembly/{B}/{DIGESTS[B][:16]}",),
        )
    app_conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE asset_edges SET props = '{}'",
        "DELETE FROM asset_edges",
        "TRUNCATE asset_edges",
        "ALTER TABLE asset_edges NO FORCE ROW LEVEL SECURITY",
    ],
)
def test_runtime_role_cannot_change_edges(app_conn, statement):
    app_conn, _shell = app_conn
    _as(app_conn, TENANT_A)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(statement)
    app_conn.rollback()


def test_edges_are_append_only_even_for_the_owner(published, admin_conn):
    with admin_conn.transaction():
        admin_conn.execute("ALTER TABLE asset_edges NO FORCE ROW LEVEL SECURITY")
        with pytest.raises(psycopg.errors.RestrictViolation):
            admin_conn.execute("UPDATE asset_edges SET props = '{}'")
        raise psycopg.Rollback()
    assert admin_conn.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'asset_edges'").fetchone()[0]
