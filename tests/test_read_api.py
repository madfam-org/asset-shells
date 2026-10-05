"""Part 2 read behaviour over HTTP: filters, pagination, modifiers, error handling."""

from __future__ import annotations

import json

import pytest
from aas_fixtures import COMMONS_SHA, material_type_env, solid_type_env
from conftest import PUB_TYPES

from asset_shells.ids import b64url_encode as enc

SOLID = solid_type_env()
SOLID_SHELL = SOLID["assetAdministrationShells"][0]["id"]
PARAMETRIC = SOLID["submodels"][1]["id"]
MATING = SOLID["submodels"][2]["id"]
MATERIAL = material_type_env()
MATERIAL_SHELL = MATERIAL["assetAdministrationShells"][0]["id"]


@pytest.fixture
def types(client, auth_header):
    extra = [solid_type_env(slug=f"part-{i}", seed=f"p{i}") for i in range(3)]
    response = client.put(
        f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA}",
        json={"environments": [SOLID, MATERIAL, *extra]},
        headers=auth_header((PUB_TYPES,)),
    )
    assert response.status_code == 201
    return client


def test_shell_pagination_walks_everything_once(types):
    seen, cursor, pages = [], None, 0
    while True:
        url = "/api/v3.1/shells?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = types.get(url).json()
        seen += [s["id"] for s in body["result"]]
        pages += 1
        cursor = body["paging_metadata"].get("cursor")
        if not cursor:
            break
    assert pages == 3 and len(seen) == 5 and len(set(seen)) == 5


def test_asset_id_filters(types):
    gid = enc(json.dumps({"name": "globalAssetId", "value": "https://id.madfam.io/asset/solid/fan-duct"}))
    slug = enc(json.dumps({"name": "slug", "value": "fan-duct"}))
    wrong = enc(json.dumps({"name": "slug", "value": "other"}))
    both = enc(
        json.dumps(
            [
                {"name": "GLOBALASSETID", "value": "https://id.madfam.io/asset/solid/fan-duct"},
                {"name": "commons", "value": "solid-hyperobjects"},
            ]
        )
    )
    ids = lambda url: [s["id"] for s in types.get(url).json()["result"]]  # noqa: E731
    assert ids(f"/api/v3.1/shells?assetIds={gid}") == [SOLID_SHELL]
    assert ids(f"/api/v3.1/shells?assetIds={gid}&assetIds={slug}") == [SOLID_SHELL]
    assert ids(f"/api/v3.1/shells?assetIds={gid},{wrong}") == []
    assert ids(f"/api/v3.1/shells?assetIds={both}") == [SOLID_SHELL]
    assert types.get(f"/api/v3.1/lookup/shells?assetIds={slug}").json()["result"] == [SOLID_SHELL]
    found = types.post(
        "/api/v3.1/lookup/shellsByAssetLink?limit=1", json=[{"name": "commons", "value": "solid-hyperobjects"}]
    ).json()
    assert len(found["result"]) == 1 and found["paging_metadata"]["cursor"]
    links = types.get(f"/api/v3.1/lookup/shells/{enc(SOLID_SHELL)}").json()
    assert links[0] == {"name": "globalAssetId", "value": "https://id.madfam.io/asset/solid/fan-duct"}
    assert [link["name"] for link in links[1:]] == ["commons", "slug", "tree_sha256"]


def test_id_short_and_semantic_filters(types):
    assert [s["id"] for s in types.get("/api/v3.1/shells?idShort=bambu-tpu-95a").json()["result"]] == [MATERIAL_SHELL]
    ref = enc(
        json.dumps(
            {
                "type": "ExternalReference",
                "keys": [{"type": "GlobalReference", "value": "https://id.madfam.io/smt/mating-interfaces/1/0"}],
            }
        )
    )
    plain = enc("https://id.madfam.io/smt/mating-interfaces/1/0")
    quoted = enc(json.dumps("https://id.madfam.io/smt/mating-interfaces/1/0"))
    for value in (ref, plain, quoted):
        result = types.get(f"/api/v3.1/submodels?semanticId={value}").json()["result"]
        assert len(result) == 4 and all(r["idShort"] == "MatingInterfaces" for r in result)
    refs = types.get(f"/api/v3.1/submodels/$reference?semanticId={ref}&idShort=MatingInterfaces").json()["result"]
    assert refs[0] == {"type": "ModelReference", "keys": [{"type": "Submodel", "value": refs[0]["keys"][0]["value"]}]}
    assert types.get("/api/v3.1/submodels/$reference?level=deep").status_code == 400
    assert types.get(f"/api/v3.1/submodels?semanticId={enc('[1]')}").status_code == 400
    assert types.get("/api/v3.1/submodels?semanticId=***").status_code == 400


def test_shell_level_reads(types):
    shell = types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}").json()
    assert shell == SOLID["assetAdministrationShells"][0]  # stored as published
    assert types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}/asset-information").json()["assetKind"] == "Type"
    refs = types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}/submodel-refs?limit=3").json()
    assert len(refs["result"]) == 3 and refs["paging_metadata"]["cursor"]
    rest = types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}/submodel-refs?cursor={refs['paging_metadata']['cursor']}")
    assert len(rest.json()["result"]) == 1 and rest.json()["paging_metadata"] == {}
    assert types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}/$reference").json()["keys"][0]["value"] == SOLID_SHELL
    all_refs = types.get("/api/v3.1/shells/$reference").json()["result"]
    assert {r["keys"][0]["type"] for r in all_refs} == {"AssetAdministrationShell"}


def test_submodel_reads_and_modifiers(types):
    base = f"/api/v3.1/submodels/{enc(PARAMETRIC)}"
    full = types.get(base).json()
    assert "value" not in full["submodelElements"][2]  # Blob value omitted by default
    with_blob = types.get(base + "?extent=withBlobValue").json()
    assert with_blob["submodelElements"][2]["value"] == "PHN2Zy8+"
    core = types.get(base + "?level=core").json()
    assert "value" not in core["submodelElements"][0]  # the list keeps its attributes, loses its items
    assert types.get(base + "/$metadata").json().get("submodelElements") is None
    value = types.get(base + "/$value").json()
    assert value["Parameters"][0] == {
        "ParameterId": "fan_size",
        "Default": 120,
        "Range": {"min": 40, "max": 140},
        "Unit": "mm",
    }
    assert value["WallLoopsMin"] == 3 and value["Printable"] is True and "value" not in value["PreviewIcon"]
    assert types.get(base + "/$value?level=core").json()["Parameters"] == []
    assert types.get(base + "/$value?extent=withBlobValue").json()["PreviewIcon"]["value"] == "PHN2Zy8+"
    assert types.get(base + "/$reference").json()["keys"][0] == {"type": "Submodel", "value": PARAMETRIC}
    elements = types.get(base + "/submodel-elements?limit=2").json()
    assert [e["idShort"] for e in elements["result"]] == ["Parameters", "Presets"]
    nested = types.get(base + "/submodel-elements/Parameters%5B1%5D.Range/$value").json()
    assert nested == {"min": 20, "max": 100}
    assert types.get(base + "/submodel-elements/Parameters%5B1%5D").json()["modelType"] == "SubmodelElementCollection"


def test_via_shell_superpath(types):
    base = f"/api/v3.1/shells/{enc(SOLID_SHELL)}/submodels/{enc(MATING)}"
    assert types.get(base).json()["id"] == MATING
    value = types.get(base + "/$value").json()["fan_screw_pattern"]
    assert value["Symmetry"] == 4 and value["Frame"]["Origin"] == "[0, 0, 0]"
    assert value["CompatibleWith"]["second"]["keys"][0]["value"] == "https://id.madfam.io/asset/solid/fan-adapter"
    assert value["Standard"]["keys"][0]["value"] == "https://id.madfam.io/concept/pc-fan-corner-holes"
    assert types.get(base + "/submodel-elements").json()["result"][0]["idShort"] == "fan_screw_pattern"
    assert types.get(base + "/submodel-elements/fan_screw_pattern.Frame.Part").json()["value"] == "body"
    assert types.get(base + "/submodel-elements/fan_screw_pattern.Frame.Part/$value").json() == "body"
    material_sm = MATERIAL["submodels"][0]["id"]
    assert types.get(f"/api/v3.1/shells/{enc(SOLID_SHELL)}/submodels/{enc(material_sm)}").status_code == 404


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/v3.1/shells/not*base64", 400, "bad_identifier"),
        (f"/api/v3.1/shells/{enc('https://id.madfam.io/aas/solid/nope/0000000000000000/p1')}", 404, "not_found"),
        ("/api/v3.1/shells?limit=0", 400, "bad_parameter"),
        ("/api/v3.1/shells?cursor=", 400, "bad_cursor"),
        ("/api/v3.1/shells?cursor=bm90LWEtY3Vyc29y", 400, "bad_cursor"),
        ("/api/v3.1/shells?cursor=%25%25", 400, "bad_cursor"),
        ("/api/v3.1/shells?assetIds=e30", 400, "bad_parameter"),
        ("/api/v3.1/shells?assetIds=bm90IGpzb24", 400, "bad_parameter"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}?level=shallow", 400, "bad_parameter"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}?extent=all", 400, "bad_parameter"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}/submodel-elements/Parameters.x", 404, "not_found"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}/submodel-elements/Presets%5B0%5D", 404, "not_found"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}/submodel-elements/Parameters%5B9%5D", 404, "not_found"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}/submodel-elements/a..b", 400, "bad_id_short_path"),
        (f"/api/v3.1/submodels/{enc(PARAMETRIC)}/submodel-elements/Nope", 404, "not_found"),
        (f"/api/v3.1/submodels/{enc('urn:none')}", 404, "not_found"),
        ("/api/v3.1/nothing-here", 404, "not_found"),
    ],
)
def test_errors_are_part2_results(types, path, status, code):
    response = types.get(path)
    assert response.status_code == status, response.text
    message = response.json()["messages"][0]
    assert message["code"] == code and message["messageType"] == "Error"
    assert response.headers["X-Request-Id"] == message["correlationId"]


def test_lookup_body_errors(types):
    assert types.post("/api/v3.1/lookup/shellsByAssetLink", content=b"nope").status_code == 400
    assert types.post("/api/v3.1/lookup/shellsByAssetLink", json={"name": "x"}).status_code == 400
    assert types.get(f"/api/v3.1/lookup/shells/{enc('urn:none')}").status_code == 404


def test_method_not_allowed_is_a_result(types):
    response = types.delete(f"/api/v3.1/shells/{enc(SOLID_SHELL)}")
    assert response.status_code == 405 and response.json()["messages"][0]["code"] == "method_not_allowed"


def test_root_and_openapi(types):
    assert types.get("/").json()["readApi"] == "/api/v3.1"
    spec = types.get("/openapi.json").json()
    assert "/api/v3.1/shells" in spec["paths"] and "/madfam/v1/instances" in spec["paths"]
