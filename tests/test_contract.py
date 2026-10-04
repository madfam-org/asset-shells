"""Contract tests against the official IDTA-01002 v3.1.3 OpenAPI documents (vendored, unmodified, in
tests/contract/specs).

1. Route parity: every operation this service exposes under /api/v3.1 carries an official operationId,
   and its method, path template and query parameter names equal the official operation's.
2. Response shapes: for every implemented operation, a real response from the service validates
   against the official 200 response schema; error responses validate against the official ``Result``.

One relaxation, stated here so it is visible: OpenAPI ``oneOf`` (meant with a discriminator, which JSON
Schema does not have) is evaluated as ``anyOf``. Several official Value-Only schemas are plain objects
that overlap (e.g. MultiLanguagePropertyValue and SubmodelElementCollectionValue), so a strict oneOf
would reject every valid value-only object. Each response must still match at least one official branch.
"""

from __future__ import annotations

import copy
import json
import uuid
from functools import cache
from pathlib import Path

import jsonschema
import pytest
import yaml
from aas_fixtures import COMMONS_SHA, instance_env, passport_event, solid_type_env
from conftest import PUB_INST, PUB_TYPES, READ, TENANT_A
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from asset_shells.app import app as service_app
from asset_shells.ids import b64url_encode as enc

SPECS = Path(__file__).parent / "contract" / "specs"
HUB = "https://api.swaggerhub.com/domains/Plattform_i40"
SERVICE_SPECS = [
    "AssetAdministrationShellRepositoryServiceSpecification/V3.1_SSP-002.yaml",
    "SubmodelRepositoryServiceSpecification/V3.1_SSP-002.yaml",
    "DiscoveryServiceSpecification/V3.1_SSP-002.yaml",
    "DiscoveryServiceSpecification/V3.1_SSP-001.yaml",
]
PREFIX = "/api/v3.1"

IMPLEMENTED = {
    # AAS Repository SSP-002
    "GetAllAssetAdministrationShells",
    "GetAllAssetAdministrationShells-Reference",
    "GetAssetAdministrationShellById",
    "GetAssetAdministrationShellById-Reference_AasRepository",
    "GetAssetInformation_AasRepository",
    "GetAllSubmodelReferences_AasRepository",
    "GetSubmodelById_AasRepository",
    "GetSubmodelById-ValueOnly_AasRepository",
    "GetAllSubmodelElements_AasRepository",
    "GetSubmodelElementByPath_AasRepository",
    "GetSubmodelElementByPath-ValueOnly_AasRepository",
    # Submodel Repository SSP-002
    "GetAllSubmodels",
    "GetAllSubmodels-Reference",
    "GetSubmodelById",
    "GetSubmodelById-Metadata",
    "GetSubmodelById-ValueOnly",
    "GetSubmodelById-Reference",
    "GetAllSubmodelElements",
    "GetSubmodelElementByPath_SubmodelRepo",
    "GetSubmodelElementByPath-ValueOnly_SubmodelRepo",
    # Discovery: SSP-002 read operations + GetAllAssetAdministrationShellIdsByAssetLink (SSP-001, read-only)
    "GetAllAssetAdministrationShellIdsByAssetLink",
    "SearchAllAssetAdministrationShellIdsByAssetLink",
    "GetAllAssetLinksById",
}


def _oneof_to_anyof(node):
    if isinstance(node, dict):
        return {("anyOf" if k == "oneOf" else k): _oneof_to_anyof(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_oneof_to_anyof(v) for v in node]
    return node


@cache
def _yaml(rel: str) -> dict:
    return yaml.safe_load((SPECS / rel).read_text(encoding="utf-8"))


@cache
def registry() -> Registry:
    resources = [
        (f"{HUB}/Part1-MetaModel-Schemas/V3.1.3", _oneof_to_anyof(_yaml("Part1-MetaModel-Schemas/openapi.yaml"))),
        (f"{HUB}/Part2-API-Schemas/V3.1.3", _oneof_to_anyof(_yaml("Part2-API-Schemas/openapi.yaml"))),
    ]
    return Registry().with_resources(
        (uri, Resource.from_contents(doc, default_specification=DRAFT7)) for uri, doc in resources
    )


@cache
def official_operations() -> dict[str, dict]:
    ops: dict[str, dict] = {}
    for rel in SERVICE_SPECS:
        for path, item in _yaml(rel)["paths"].items():
            shared = item.get("parameters", [])
            for method, op in item.items():
                if method == "parameters":
                    continue
                ops.setdefault(
                    op["operationId"],
                    {"method": method, "path": path, "op": op, "parameters": shared + op.get("parameters", [])},
                )
    return ops


def _param(p: dict) -> dict:
    if "$ref" in p:
        name = p["$ref"].rsplit("/", 1)[-1]
        return _yaml("Part2-API-Schemas/openapi.yaml")["components"]["parameters"][name]
    return p


def _query_names(params: list[dict]) -> set[str]:
    return {_param(p)["name"] for p in params if _param(p).get("in") == "query"}


@cache
def our_operations() -> dict[str, dict]:
    spec = service_app.openapi()
    ops = {}
    for path, item in spec["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for method, op in item.items():
            ops[op["operationId"]] = {
                "method": method,
                "path": path.removeprefix(PREFIX),
                "query": {p["name"] for p in op.get("parameters", []) if p["in"] == "query"},
            }
    return ops


def test_implemented_set_is_exactly_the_documented_list():
    assert set(our_operations()) == IMPLEMENTED


@pytest.mark.parametrize("operation_id", sorted(IMPLEMENTED))
def test_route_matches_official_operation(operation_id):
    ours, official = our_operations()[operation_id], official_operations()[operation_id]
    assert ours["method"] == official["method"]
    assert ours["path"] == official["path"]
    assert ours["query"] == _query_names(official["parameters"])


def _validator(schema: dict) -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(_oneof_to_anyof(copy.deepcopy(schema)), registry=registry())


def response_schema(operation_id: str, status: str = "200") -> dict:
    response = official_operations()[operation_id]["op"]["responses"][status]
    if "$ref" in response:
        # Keep the reference absolute so relative refs inside Part2 resolve in their own document.
        return {"$ref": response["$ref"] + "/content/application~1json/schema"}
    return response["content"]["application/json"]["schema"]


def assert_valid(operation_id: str, body, status: str = "200") -> None:
    errors = sorted(_validator(response_schema(operation_id, status)).iter_errors(body), key=str)
    assert not errors, f"{operation_id}: {errors[0].message[:300]} at {list(errors[0].absolute_path)}"


UUID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SOLID = solid_type_env()
SHELL = SOLID["assetAdministrationShells"][0]["id"]
PARAM = SOLID["submodels"][1]["id"]
INSTANCE = f"https://id.madfam.io/aas/instance/{UUID_A}"
RECORD = f"https://id.madfam.io/sm/instance/{UUID_A}/ManufacturingRecord"
SLUG_ID = enc(json.dumps({"name": "slug", "value": "fan-duct"}))


def _requests() -> list[tuple[str, str, str, object]]:
    s, p, i, r = enc(SHELL), enc(PARAM), enc(INSTANCE), enc(RECORD)
    return [
        ("GetAllAssetAdministrationShells", "GET", f"/shells?assetIds={SLUG_ID}&limit=1", None),
        ("GetAllAssetAdministrationShells", "GET", "/shells?limit=1", None),
        ("GetAllAssetAdministrationShells-Reference", "GET", "/shells/$reference", None),
        ("GetAssetAdministrationShellById", "GET", f"/shells/{s}", None),
        ("GetAssetAdministrationShellById", "GET", f"/shells/{i}", None),
        ("GetAssetAdministrationShellById-Reference_AasRepository", "GET", f"/shells/{s}/$reference", None),
        ("GetAssetInformation_AasRepository", "GET", f"/shells/{s}/asset-information", None),
        ("GetAllSubmodelReferences_AasRepository", "GET", f"/shells/{s}/submodel-refs?limit=2", None),
        ("GetSubmodelById_AasRepository", "GET", f"/shells/{s}/submodels/{p}", None),
        ("GetSubmodelById-ValueOnly_AasRepository", "GET", f"/shells/{s}/submodels/{p}/$value", None),
        ("GetSubmodelById-ValueOnly_AasRepository", "GET", f"/shells/{i}/submodels/{r}/$value?level=core", None),
        ("GetAllSubmodelElements_AasRepository", "GET", f"/shells/{s}/submodels/{p}/submodel-elements", None),
        (
            "GetSubmodelElementByPath_AasRepository",
            "GET",
            f"/shells/{s}/submodels/{p}/submodel-elements/Parameters%5B0%5D",
            None,
        ),
        (
            "GetSubmodelElementByPath-ValueOnly_AasRepository",
            "GET",
            f"/shells/{s}/submodels/{p}/submodel-elements/Parameters%5B0%5D.Range/$value",
            None,
        ),
        ("GetAllSubmodels", "GET", "/submodels?limit=3", None),
        ("GetAllSubmodels-Reference", "GET", "/submodels/$reference", None),
        ("GetSubmodelById", "GET", f"/submodels/{p}?extent=withBlobValue", None),
        ("GetSubmodelById", "GET", f"/submodels/{r}", None),
        ("GetSubmodelById-Metadata", "GET", f"/submodels/{p}/$metadata", None),
        ("GetSubmodelById-ValueOnly", "GET", f"/submodels/{p}/$value", None),
        ("GetSubmodelById-ValueOnly", "GET", f"/submodels/{enc(SOLID['submodels'][3]['id'])}/$value", None),
        ("GetSubmodelById-ValueOnly", "GET", f"/submodels/{enc(SOLID['submodels'][2]['id'])}/$value", None),
        ("GetSubmodelById-Reference", "GET", f"/submodels/{p}/$reference", None),
        ("GetAllSubmodelElements", "GET", f"/submodels/{r}/submodel-elements", None),
        ("GetSubmodelElementByPath_SubmodelRepo", "GET", f"/submodels/{r}/submodel-elements/Events%5B0%5D", None),
        (
            "GetSubmodelElementByPath-ValueOnly_SubmodelRepo",
            "GET",
            f"/submodels/{r}/submodel-elements/Events%5B0%5D/$value",
            None,
        ),
        (
            "GetSubmodelElementByPath-ValueOnly_SubmodelRepo",
            "GET",
            f"/submodels/{p}/submodel-elements/Printable/$value",
            None,
        ),
        ("GetAllAssetAdministrationShellIdsByAssetLink", "GET", f"/lookup/shells?assetIds={SLUG_ID}", None),
        (
            "SearchAllAssetAdministrationShellIdsByAssetLink",
            "POST",
            "/lookup/shellsByAssetLink",
            [{"name": "slug", "value": "fan-duct"}],
        ),
        ("GetAllAssetLinksById", "GET", f"/lookup/shells/{s}", None),
    ]


@pytest.fixture
def populated(client, auth_header):
    assert (
        client.put(
            f"/madfam/v1/type-environments/solid-hyperobjects/{COMMONS_SHA}",
            json={"environments": [solid_type_env()]},
            headers=auth_header((PUB_TYPES,)),
        ).status_code
        == 201
    )
    pub = auth_header((PUB_INST,), tenant=TENANT_A)
    assert client.post("/madfam/v1/instances", json=instance_env(UUID_A, SHELL), headers=pub).status_code == 201
    event = {"eventId": str(uuid.uuid4()), "submodelIdShort": "ManufacturingRecord", "event": passport_event()}
    assert client.post(f"/madfam/v1/instances/{UUID_A}/passport-events", json=event, headers=pub).status_code == 201
    return client, auth_header((READ,), tenant=TENANT_A)


def test_every_implemented_operation_is_exercised():
    assert {op for op, *_ in _requests()} == IMPLEMENTED


@pytest.mark.parametrize(
    ("operation_id", "method", "path", "body"), _requests(), ids=[f"{r[0]}:{i}" for i, r in enumerate(_requests())]
)
def test_response_matches_official_schema(populated, operation_id, method, path, body):
    client, headers = populated
    response = client.request(method, PREFIX + path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert_valid(operation_id, response.json())


@pytest.mark.parametrize(
    ("operation_id", "path", "status", "headers"),
    [
        ("GetAssetAdministrationShellById", "/shells/***", "400", "read"),
        ("GetAssetAdministrationShellById", f"/shells/{enc(INSTANCE)}", "401", "anon"),
        ("GetAssetAdministrationShellById", f"/shells/{enc(INSTANCE)}", "403", "no_scope"),
        ("GetAssetAdministrationShellById", f"/shells/{enc('urn:x')}", "404", "read"),
        ("GetSubmodelById", f"/submodels/{enc('urn:x')}?level=x", "400", "read"),
        ("GetAllSubmodels", "/submodels?cursor=zz", "400", "read"),
    ],
)
def test_error_bodies_match_official_result(populated, auth_header, operation_id, path, status, headers):
    client, read = populated
    chosen = {"read": read, "anon": {}, "no_scope": auth_header((PUB_INST,), tenant=TENANT_A)}[headers]
    response = client.get(PREFIX + path, headers=chosen)
    assert str(response.status_code) == status
    assert_valid(operation_id, response.json(), status)


def test_paged_results_use_the_official_member_names(populated):
    client, headers = populated
    body = client.get(f"{PREFIX}/submodels?limit=1", headers=headers).json()
    assert set(body) == {"paging_metadata", "result"} and set(body["paging_metadata"]) == {"cursor"}


@pytest.mark.parametrize(
    ("operation_id", "body"),
    [
        ("GetAllAssetAdministrationShells", {"result": []}),  # paging_metadata is required
        ("GetAssetAdministrationShellById", {"modelType": "AssetAdministrationShell", "id": "urn:x"}),  # no assetInfo
        ("GetSubmodelById", {"modelType": "Submodel"}),  # no id
        ("GetAllAssetLinksById", [{"name": "x"}]),  # SpecificAssetId needs a value
        ("GetSubmodelById-Reference", {"keys": []}),  # Reference needs a type
    ],
)
def test_negative_controls_are_rejected(operation_id, body):
    """The contract check is not vacuous: shapes that break the official schema fail it."""
    assert list(_validator(response_schema(operation_id)).iter_errors(body))
