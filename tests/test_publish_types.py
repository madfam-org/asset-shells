"""PUT /madfam/v1/type-environments/{commons}/{sha}: validation, idempotency, immutability, scope."""

from __future__ import annotations

import pytest
from aas_fixtures import COMMONS_SHA, COMMONS_SHA_2, clone, material_type_env, solid_type_env
from conftest import PUB_INST, PUB_TYPES, READ, TENANT_A

URL = f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA}"


def put(client, auth_header, body, url=URL, scopes=(PUB_TYPES,), **kw):
    return client.put(url, json=body, headers=auth_header(scopes, **kw))


def codes(response) -> list[str]:
    return [m["code"] for m in response.json()["messages"]]


def test_publish_creates_then_replays(client, auth_header, admin_conn):
    body = {"environments": [solid_type_env(), material_type_env()]}
    first = put(client, auth_header, body)
    assert first.status_code == 201, first.text
    summary = first.json()
    assert summary["created"] is True
    assert len(summary["shells"]["created"]) == 2
    assert len(summary["submodels"]["created"]) == 5
    assert len(summary["conceptDescriptions"]["created"]) == 5

    again = put(client, auth_header, body)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["contentSha256"] == summary["contentSha256"]

    topics = [r[0] for r in admin_conn.execute("SELECT topic FROM outbox ORDER BY id").fetchall()]
    assert topics.count("type_shell.published") == 2
    assert topics.count("type_release.published") == 1
    assert topics.count("concept_description.changed") == 5


def test_formatting_only_differences_are_a_noop(client, auth_header):
    env = solid_type_env()
    assert put(client, auth_header, {"environments": [env]}).status_code == 201
    reordered = {k: env[k] for k in reversed(list(env))}
    assert put(client, auth_header, {"environments": [reordered]}).status_code == 200


def test_new_release_reuses_unchanged_shells(client, auth_header):
    assert put(client, auth_header, {"environments": [solid_type_env()]}).status_code == 201
    second = put(
        client,
        auth_header,
        {"environments": [solid_type_env(), material_type_env()]},
        url=f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA_2}",
    )
    assert second.status_code == 201
    body = second.json()
    assert len(body["shells"]["unchanged"]) == 1 and len(body["shells"]["created"]) == 1
    assert len(body["conceptDescriptions"]["unchanged"]) == 5


def test_release_is_immutable(client, auth_header):
    assert put(client, auth_header, {"environments": [solid_type_env()]}).status_code == 201
    other = put(client, auth_header, {"environments": [material_type_env()]})
    assert other.status_code == 409
    assert codes(other) == ["release_conflict"]


def test_shell_content_is_immutable_per_id(client, auth_header):
    assert put(client, auth_header, {"environments": [solid_type_env()]}).status_code == 201
    changed = solid_type_env()
    changed["submodels"][0]["submodelElements"][1]["value"] = "Someone Else"
    response = put(
        client,
        auth_header,
        {"environments": [changed]},
        url=f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA_2}",
    )
    assert response.status_code == 409
    assert codes(response) == ["immutable"]


def test_concept_description_update_is_allowed(client, auth_header):
    assert put(client, auth_header, {"environments": [solid_type_env()]}).status_code == 201
    env = solid_type_env()
    env["conceptDescriptions"][0]["description"][0]["text"] = "revised definition"
    response = put(
        client,
        auth_header,
        {"environments": [env]},
        url=f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA_2}",
    )
    assert response.status_code == 201
    assert response.json()["conceptDescriptions"]["updated"] == ["https://id.madfam.io/concept/parameter"]


def test_duplicate_ids_with_different_content_are_rejected(client, auth_header):
    a, b = solid_type_env(), solid_type_env()
    b["conceptDescriptions"][0]["description"][0]["text"] = "different"
    response = put(client, auth_header, {"environments": [a, b]})
    assert response.status_code == 422
    assert "duplicate_id" in codes(response)


@pytest.mark.parametrize(
    ("mutate", "code", "path_fragment"),
    [
        (lambda e: e["assetAdministrationShells"][0].update(id="https://example.org/aas/1"), "id_scheme", "/id"),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"].update(assetKind="Instance"),
            "asset_kind",
            "assetKind",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"].update(
                globalAssetId="https://id.madfam.io/asset/solid/other"
            ),
            "id_scheme",
            "globalAssetId",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"].pop(2),
            "asset_ids",
            "specificAssetIds",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"][0].update(
                value="soft-hyperobjects"
            ),
            "asset_ids",
            "specificAssetIds",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"][1].update(
                value="other-slug"
            ),
            "asset_ids",
            "specificAssetIds",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"][2].update(
                value="0" * 64
            ),
            "asset_ids",
            "specificAssetIds",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["assetInformation"]["specificAssetIds"].append(
                {"name": "slug", "value": "fan-duct"}
            ),
            "asset_ids",
            "specificAssetIds",
        ),
        (
            lambda e: e["assetAdministrationShells"][0].update(
                derivedFrom={
                    "type": "ModelReference",
                    "keys": [
                        {
                            "type": "AssetAdministrationShell",
                            "value": "https://id.madfam.io/aas/solid/x/0000000000000000/p1",
                        }
                    ],
                }
            ),
            "derived_from",
            "derivedFrom",
        ),
        (lambda e: e["submodels"][0].update(idShort="Typeplate"), "id_scheme", "/submodels/0/idShort"),
        (lambda e: e["assetAdministrationShells"][0].pop("extensions"), "projection_version", "/extensions"),
        (
            lambda e: e["assetAdministrationShells"][0]["extensions"][0].update(value="2"),
            "projection_version",
            "/extensions",
        ),
        (
            # An unversioned (pre-0.6.0) shell id is not a type shell id.
            lambda e: e["assetAdministrationShells"][0].update(id=e["assetAdministrationShells"][0]["id"][:-3]),
            "id_scheme",
            "/assetAdministrationShells/0/id",
        ),
        (
            # A submodel of another projection version is outside the shell's namespace.
            lambda e: e["assetAdministrationShells"][0]["submodels"][0]["keys"][0].update(
                value=e["submodels"][0]["id"].replace("/p1/", "/p2/")
            ),
            "id_scheme",
            "/assetAdministrationShells/0/submodels",
        ),
        (
            lambda e: e["submodels"][0].update(id="https://id.madfam.io/sm/solid/fan-duct/Nameplate"),
            "id_scheme",
            "/submodels/0/id",
        ),
        (lambda e: e["submodels"].pop(0), "dangling_reference", "/environments/0"),
        (lambda e: e["assetAdministrationShells"][0]["submodels"].pop(0), "orphan_submodel", "/submodels/0/id"),
        (
            lambda e: e["conceptDescriptions"][0].update(id="https://example.org/cd/1"),
            "id_scheme",
            "/conceptDescriptions/0/id",
        ),
        (
            lambda e: e["assetAdministrationShells"][0]["submodels"].append(
                {"type": "ModelReference", "keys": [{"type": "AssetAdministrationShell", "value": "urn:x"}]}
            ),
            "reference",
            "/submodels/",
        ),
    ],
)
def test_sem1_rules_reject_with_paths(client, auth_header, mutate, code, path_fragment):
    env = solid_type_env()
    mutate(env)
    response = put(client, auth_header, {"environments": [env]})
    assert response.status_code == 422, response.text
    messages = response.json()["messages"]
    match = [m for m in messages if m["code"] == code]
    assert match, messages
    assert any(path_fragment in m["path"] for m in match), messages
    assert all(m["messageType"] == "Error" and m["timestamp"] and m["correlationId"] for m in messages)


def test_submodel_outside_shell_namespace_and_shared_reference(client, auth_header):
    env = solid_type_env()
    other = solid_type_env(slug="fan-adapter", seed="fan-adapter@1")
    env["assetAdministrationShells"][0]["submodels"].append(other["assetAdministrationShells"][0]["submodels"][0])
    env["assetAdministrationShells"] += other["assetAdministrationShells"]
    env["submodels"] += other["submodels"]
    response = put(client, auth_header, {"environments": [env]})
    assert response.status_code == 422
    texts = " ".join(m["text"] for m in response.json()["messages"])
    assert "not in this shell's namespace" in texts and "referenced by two shells" in texts


def test_material_kind_not_allowed_for_unknown_commons_kind(client, auth_header):
    env = solid_type_env()
    response = put(
        client,
        auth_header,
        {"environments": [env]},
        url=f"/madfam/v1/type-environments/soft-hyperobjects/{COMMONS_SHA}",
    )
    assert response.status_code == 422
    assert "id_scheme" in codes(response) and "asset_ids" in codes(response)


def test_metamodel_errors_are_rejected(client, auth_header):
    env = solid_type_env()
    env["submodels"][1]["submodelElements"][3]["value"] = "not-an-int"
    response = put(client, auth_header, {"environments": [env]})
    assert response.status_code == 422
    assert codes(response) == ["metamodel"]

    env = solid_type_env()
    env["submodels"][1]["submodelElements"][0]["value"][0]["value"][0]["bogus"] = 1
    response = put(client, auth_header, {"environments": [env]})
    assert response.status_code == 422
    assert codes(response) == ["unknown_attribute"]
    assert response.json()["messages"][0]["path"].endswith("/value/0/value/0/bogus")

    env = solid_type_env()
    env["assetAdministrationShells"][0]["idShort"] = "9-bad"
    response = put(client, auth_header, {"environments": [env]})
    assert response.status_code == 422 and codes(response)[0] == "schema"


@pytest.mark.parametrize(
    ("url", "status", "code"),
    [
        (f"/madfam/v1/type-environments/unknown-commons/{COMMONS_SHA}", 400, "unknown_commons"),
        ("/madfam/v1/type-environments/solid-hyperobjects/not-a-sha", 400, "bad_sha"),
    ],
)
def test_path_validation(client, auth_header, url, status, code):
    response = put(client, auth_header, {"environments": [solid_type_env()]}, url=url)
    assert response.status_code == status and codes(response) == [code]


@pytest.mark.parametrize("body", [[], {"environments": []}, {"environments": "x"}, {"other": 1}])
def test_body_shape(client, auth_header, body):
    response = put(client, auth_header, body)
    assert response.status_code == 422 and codes(response) == ["body"]


def test_non_object_environment(client, auth_header):
    response = put(client, auth_header, {"environments": [[1, 2]]})
    assert response.status_code == 422 and codes(response) == ["schema"]


def test_empty_environment_has_no_shell(client, auth_header):
    response = put(client, auth_header, {"environments": [{}]})
    assert response.status_code == 422 and codes(response) == ["environment"]


def test_scope_and_token_are_enforced(client, auth_header):
    body = {"environments": [solid_type_env()]}
    assert client.put(URL, json=body).status_code == 401
    assert put(client, auth_header, body, scopes=(READ, PUB_INST), tenant=TENANT_A).status_code == 403
    assert client.put(URL, json=body, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.put(URL, json=body, headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401


def test_body_limits_and_media_type(client, auth_header, monkeypatch):
    headers = auth_header((PUB_TYPES,))
    bad_json = client.put(URL, content=b"{not json", headers={**headers, "Content-Type": "application/json"})
    assert bad_json.status_code == 400 and codes(bad_json) == ["bad_json"]
    wrong_type = client.put(URL, content=b"{}", headers={**headers, "Content-Type": "text/plain"})
    assert wrong_type.status_code == 415
    from asset_shells import settings

    monkeypatch.setenv("MAX_PUBLISH_BYTES", "64")
    settings.reset_settings_cache()
    try:
        big = put(client, auth_header, {"environments": [solid_type_env()]})
        assert big.status_code == 413 and codes(big) == ["too_large"]
        streamed = client.put(
            URL, content=iter([b"[" + b" " * 100, b"]"]), headers={**headers, "Content-Type": "application/json"}
        )
        assert streamed.status_code == 413
    finally:
        monkeypatch.delenv("MAX_PUBLISH_BYTES")
        settings.reset_settings_cache()


def test_type_rows_are_immutable_even_for_the_owner(client, auth_header, migrated):
    import psycopg

    assert put(client, auth_header, {"environments": [clone(solid_type_env())]}).status_code == 201
    with psycopg.connect(migrated) as conn:
        # Layer 1: FORCE ROW LEVEL SECURITY — no UPDATE policy reaches a type row, owner included.
        assert conn.execute("UPDATE shells SET id_short = 'x'").rowcount == 0
        assert conn.execute("UPDATE submodels SET id_short = 'x'").rowcount == 0
        conn.rollback()
        # Layer 2: the triggers refuse even when the owner lifts FORCE inside a transaction.
        for table, statement in (
            ("shells", "UPDATE shells SET id_short = 'x'"),
            ("submodels", "UPDATE submodels SET id_short = 'x'"),
            ("shells", "DELETE FROM shells"),
        ):
            conn.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            with pytest.raises(psycopg.errors.RestrictViolation):
                conn.execute(statement)
            conn.rollback()
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute("DELETE FROM type_releases")
        conn.rollback()
