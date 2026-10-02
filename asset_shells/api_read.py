"""Part 2 read API at /api/v3.1 — a subset of the AAS Repository SSP-002, Submodel Repository SSP-002
and Discovery read operations of IDTA-01002 v3.1.3 (aas-specs-api tag v3.1.3). Every route's
``operation_id`` is the official operationId; tests/test_contract.py checks method, path, query
parameters and response bodies against the vendored official OpenAPI.

Visibility: type shells and their submodels are public. Instance shells/submodels are visible only to
a token with ``asset-shells:read`` and a ``tenant_id``, and only that tenant's rows (row-level
security). An anonymous request for an id in the instance namespace is a 401; a token that may not
read instances gets a 403; another tenant's instance is a 404 (existence is not disclosed).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from . import db, repository, views
from .auth import SCOPE_READ, Principal, optional_principal, read_tenant
from .errors import bad_request, forbidden, not_found, unauthorized
from .ids import InvalidEncodedId, b64url_decode, looks_like_instance_id
from .settings import get_settings

router = APIRouter(prefix="/api/v3.1")

SHELLS_TAG = "Asset Administration Shell Repository API"
SUBMODELS_TAG = "Submodel Repository API"
DISCOVERY_TAG = "Asset Administration Shell Basic Discovery API"


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _decode_id(encoded: str, what: str) -> str:
    try:
        return b64url_decode(encoded)
    except InvalidEncodedId:
        raise bad_request("bad_identifier", f"{what} must be UTF8-BASE64-URL-encoded") from None


def _tenant_for(principal: Principal | None, *identifiers: str) -> str | None:
    """The tenant context for a read; refuses instance-namespace ids the caller cannot read."""
    tenant = read_tenant(principal)
    if tenant is None and any(looks_like_instance_id(i) for i in identifiers):
        if principal is None:
            raise unauthorized("missing_token", "Instance data requires a Janua service token")
        raise forbidden("missing_scope", f"Instance data requires '{SCOPE_READ}' and an organisation-bound token")
    return tenant


def _limit(limit: int | None) -> int:
    s = get_settings()
    return min(limit or s.default_page_limit, s.max_page_limit)


def _json(body: object, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _decode_json_param(encoded: str, what: str) -> object:
    try:
        return json.loads(b64url_decode(encoded))
    except (InvalidEncodedId, json.JSONDecodeError):
        raise bad_request("bad_parameter", f"{what} must be base64url-encoded JSON") from None


def _asset_pairs(asset_ids: list[str] | None) -> list[tuple[str, str]]:
    """``assetIds``: repeated or comma-separated base64url(JSON SpecificAssetId | [SpecificAssetId])."""
    pairs: list[tuple[str, str]] = []
    for raw in asset_ids or []:
        for encoded in filter(None, raw.split(",")):
            decoded = _decode_json_param(encoded, "assetIds")
            items = decoded if isinstance(decoded, list) else [decoded]
            for item in items:
                if not (isinstance(item, dict) and isinstance(item.get("name"), str)
                        and isinstance(item.get("value"), str) and item["name"] and item["value"]):
                    raise bad_request("bad_parameter", "each assetId is a JSON object with string name and value")
                pairs.append((item["name"], item["value"]))
    return pairs


def _shell_or_404(cur, shell_id: str) -> dict:
    shell = repository.get_shell(cur, shell_id)
    if shell is None:
        raise not_found()
    return shell


def _submodel_via_shell(cur, shell_id: str, submodel_id: str) -> dict:
    shell = _shell_or_404(cur, shell_id)
    if not repository.shell_references_submodel(shell, submodel_id):
        raise not_found("The shell does not reference this submodel")
    submodel = repository.get_submodel(cur, submodel_id)
    if submodel is None:
        raise not_found()
    return submodel


def _submodel_or_404(cur, submodel_id: str) -> dict:
    submodel = repository.get_submodel(cur, submodel_id)
    if submodel is None:
        raise not_found()
    return submodel


def _element_or_404(submodel: dict, id_short_path: str) -> dict:
    element = views.resolve_path(submodel, views.parse_id_short_path(id_short_path))
    if element is None:
        raise not_found("No submodel element at this idShortPath")
    return element


# ---------------------------------------------------------------------------------------------
# AAS Repository (SSP-002 subset)
# ---------------------------------------------------------------------------------------------


@router.get("/shells", operation_id="GetAllAssetAdministrationShells", tags=[SHELLS_TAG])
def get_all_shells(
    assetIds: list[str] | None = Query(None),  # noqa: N803 - official parameter name
    idShort: str | None = Query(None),  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    pairs = _asset_pairs(assetIds)
    with db.transaction(read_tenant(principal)) as cur:
        page = repository.list_shells(cur, pairs, idShort, cursor, _limit(limit))
    return _json(page.body())


@router.get("/shells/$reference", operation_id="GetAllAssetAdministrationShells-Reference", tags=[SHELLS_TAG])
def get_all_shell_references(
    assetIds: list[str] | None = Query(None),  # noqa: N803
    idShort: str | None = Query(None),  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    pairs = _asset_pairs(assetIds)
    with db.transaction(read_tenant(principal)) as cur:
        page = repository.list_shells(cur, pairs, idShort, cursor, _limit(limit))
    page = repository.Page([views.shell_reference(s["id"]) for s in page.items], page.next_cursor)
    return _json(page.body())


@router.get("/shells/{aasIdentifier}", operation_id="GetAssetAdministrationShellById", tags=[SHELLS_TAG])
def get_shell(aasIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    with db.transaction(_tenant_for(principal, shell_id)) as cur:
        return _json(_shell_or_404(cur, shell_id))


@router.get(
    "/shells/{aasIdentifier}/$reference",
    operation_id="GetAssetAdministrationShellById-Reference_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_reference(aasIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    with db.transaction(_tenant_for(principal, shell_id)) as cur:
        shell = _shell_or_404(cur, shell_id)
    return _json(views.shell_reference(shell["id"]))


@router.get(
    "/shells/{aasIdentifier}/asset-information",
    operation_id="GetAssetInformation_AasRepository",
    tags=[SHELLS_TAG],
)
def get_asset_information(aasIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    with db.transaction(_tenant_for(principal, shell_id)) as cur:
        shell = _shell_or_404(cur, shell_id)
    return _json(shell["assetInformation"])


@router.get(
    "/shells/{aasIdentifier}/submodel-refs",
    operation_id="GetAllSubmodelReferences_AasRepository",
    tags=[SHELLS_TAG],
)
def get_submodel_refs(
    aasIdentifier: str,  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    with db.transaction(_tenant_for(principal, shell_id)) as cur:
        shell = _shell_or_404(cur, shell_id)
    return _json(repository.page_list(shell.get("submodels", []), cursor, _limit(limit)).body())


@router.get(
    "/shells/{aasIdentifier}/submodels/{submodelIdentifier}",
    operation_id="GetSubmodelById_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_submodel(
    aasIdentifier: str,  # noqa: N803
    submodelIdentifier: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, shell_id, sm_id)) as cur:
        submodel = _submodel_via_shell(cur, shell_id, sm_id)
    return _json(views.apply_modifiers(submodel, level, extent))


@router.get(
    "/shells/{aasIdentifier}/submodels/{submodelIdentifier}/$value",
    operation_id="GetSubmodelById-ValueOnly_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_submodel_value(
    aasIdentifier: str,  # noqa: N803
    submodelIdentifier: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, shell_id, sm_id)) as cur:
        submodel = _submodel_via_shell(cur, shell_id, sm_id)
    return _json(views.submodel_value(submodel, level, extent))


@router.get(
    "/shells/{aasIdentifier}/submodels/{submodelIdentifier}/submodel-elements",
    operation_id="GetAllSubmodelElements_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_submodel_elements(
    aasIdentifier: str,  # noqa: N803
    submodelIdentifier: str,  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, shell_id, sm_id)) as cur:
        submodel = _submodel_via_shell(cur, shell_id, sm_id)
    return _elements_page(submodel, level, extent, cursor, limit)


@router.get(
    "/shells/{aasIdentifier}/submodels/{submodelIdentifier}/submodel-elements/{idShortPath}",
    operation_id="GetSubmodelElementByPath_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_submodel_element(
    aasIdentifier: str,  # noqa: N803
    submodelIdentifier: str,  # noqa: N803
    idShortPath: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, shell_id, sm_id)) as cur:
        submodel = _submodel_via_shell(cur, shell_id, sm_id)
    return _json(views.apply_modifiers(_element_or_404(submodel, idShortPath), level, extent))


@router.get(
    "/shells/{aasIdentifier}/submodels/{submodelIdentifier}/submodel-elements/{idShortPath}/$value",
    operation_id="GetSubmodelElementByPath-ValueOnly_AasRepository",
    tags=[SHELLS_TAG],
)
def get_shell_submodel_element_value(
    aasIdentifier: str,  # noqa: N803
    submodelIdentifier: str,  # noqa: N803
    idShortPath: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, shell_id, sm_id)) as cur:
        submodel = _submodel_via_shell(cur, shell_id, sm_id)
    return _json(views.element_value(_element_or_404(submodel, idShortPath), level, extent))


def _elements_page(submodel: dict, level: str, extent: str, cursor: str | None, limit: int | None) -> JSONResponse:
    page = repository.page_list(submodel.get("submodelElements", []), cursor, _limit(limit))
    items = [views.apply_modifiers(e, level, extent) for e in page.items]
    return _json(repository.Page(items, page.next_cursor).body())


# ---------------------------------------------------------------------------------------------
# Submodel Repository (SSP-002 subset)
# ---------------------------------------------------------------------------------------------


def _semantic_filter(semantic_id: str | None) -> tuple[str | None, str | None]:
    """``semanticId``: base64url(JSON Reference) per Part 2; a base64url plain IRI is also accepted."""
    if semantic_id is None:
        return None, None
    try:
        text = b64url_decode(semantic_id)
    except InvalidEncodedId:
        raise bad_request("bad_parameter", "semanticId must be base64url-encoded") from None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None, text
    if isinstance(decoded, dict) and isinstance(decoded.get("keys"), list) and decoded["keys"]:
        return repository.canonical_reference(decoded), None
    if isinstance(decoded, str):
        return None, decoded
    raise bad_request("bad_parameter", "semanticId must encode a Reference (JSON) or an IRI")


def _list_submodels(principal, semantic_id, id_short, cursor, limit) -> repository.Page:
    ref, value = _semantic_filter(semantic_id)
    with db.transaction(read_tenant(principal)) as cur:
        return repository.list_submodels(cur, ref, value, id_short, cursor, _limit(limit))


@router.get("/submodels", operation_id="GetAllSubmodels", tags=[SUBMODELS_TAG])
def get_all_submodels(
    semanticId: str | None = Query(None, min_length=1, max_length=3072),  # noqa: N803
    idShort: str | None = Query(None),  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    level, extent = views.check_level(level), views.check_extent(extent)
    page = _list_submodels(principal, semanticId, idShort, cursor, limit)
    items = [views.apply_modifiers(s, level, extent) for s in page.items]
    return _json(repository.Page(items, page.next_cursor).body())


@router.get("/submodels/$reference", operation_id="GetAllSubmodels-Reference", tags=[SUBMODELS_TAG])
def get_all_submodel_references(
    semanticId: str | None = Query(None, min_length=1, max_length=3072),  # noqa: N803
    idShort: str | None = Query(None),  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    level: str = Query("core"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    if level != "core":
        raise bad_request("bad_parameter", "level=deep is not allowed with Content=Reference (IDTA-01002)")
    page = _list_submodels(principal, semanticId, idShort, cursor, limit)
    items = [views.submodel_reference(s["id"]) for s in page.items]
    return _json(repository.Page(items, page.next_cursor).body())


@router.get("/submodels/{submodelIdentifier}", operation_id="GetSubmodelById", tags=[SUBMODELS_TAG])
def get_submodel(
    submodelIdentifier: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.apply_modifiers(submodel, level, extent))


@router.get("/submodels/{submodelIdentifier}/$metadata", operation_id="GetSubmodelById-Metadata",
            tags=[SUBMODELS_TAG])
def get_submodel_metadata(submodelIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.metadata_of(submodel))


@router.get("/submodels/{submodelIdentifier}/$value", operation_id="GetSubmodelById-ValueOnly", tags=[SUBMODELS_TAG])
def get_submodel_value(
    submodelIdentifier: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.submodel_value(submodel, level, extent))


@router.get("/submodels/{submodelIdentifier}/$reference", operation_id="GetSubmodelById-Reference",
            tags=[SUBMODELS_TAG])
def get_submodel_reference(submodelIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.submodel_reference(submodel["id"]))


@router.get("/submodels/{submodelIdentifier}/submodel-elements", operation_id="GetAllSubmodelElements",
            tags=[SUBMODELS_TAG])
def get_submodel_elements(
    submodelIdentifier: str,  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _elements_page(submodel, level, extent, cursor, limit)


@router.get(
    "/submodels/{submodelIdentifier}/submodel-elements/{idShortPath}",
    operation_id="GetSubmodelElementByPath_SubmodelRepo",
    tags=[SUBMODELS_TAG],
)
def get_submodel_element(
    submodelIdentifier: str,  # noqa: N803
    idShortPath: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.apply_modifiers(_element_or_404(submodel, idShortPath), level, extent))


@router.get(
    "/submodels/{submodelIdentifier}/submodel-elements/{idShortPath}/$value",
    operation_id="GetSubmodelElementByPath-ValueOnly_SubmodelRepo",
    tags=[SUBMODELS_TAG],
)
def get_submodel_element_value(
    submodelIdentifier: str,  # noqa: N803
    idShortPath: str,  # noqa: N803
    level: str = Query("deep"),
    extent: str = Query("withoutBlobValue"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    sm_id = _decode_id(submodelIdentifier, "submodelIdentifier")
    level, extent = views.check_level(level), views.check_extent(extent)
    with db.transaction(_tenant_for(principal, sm_id)) as cur:
        submodel = _submodel_or_404(cur, sm_id)
    return _json(views.element_value(_element_or_404(submodel, idShortPath), level, extent))


# ---------------------------------------------------------------------------------------------
# Discovery (read operations)
# ---------------------------------------------------------------------------------------------


@router.get("/lookup/shells", operation_id="GetAllAssetAdministrationShellIdsByAssetLink", tags=[DISCOVERY_TAG])
def lookup_shells(
    assetIds: list[str] | None = Query(None),  # noqa: N803
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    pairs = _asset_pairs(assetIds)
    with db.transaction(read_tenant(principal)) as cur:
        page = repository.list_shell_ids_by_assets(cur, pairs, cursor, _limit(limit))
    return _json(page.body())


@router.post("/lookup/shellsByAssetLink", operation_id="SearchAllAssetAdministrationShellIdsByAssetLink",
             tags=[DISCOVERY_TAG])
async def search_shells_by_asset_link(
    request: Request,
    limit: int | None = Query(None, ge=1),
    cursor: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise bad_request("bad_body", "the body is a JSON array of AssetLink objects") from None
    if not isinstance(body, list) or not all(
        isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("value"), str) for i in body
    ):
        raise bad_request("bad_body", "the body is a JSON array of {name, value} AssetLink objects")
    pairs = [(i["name"], i["value"]) for i in body]

    def run() -> repository.Page:
        with db.transaction(read_tenant(principal)) as cur:
            return repository.list_shell_ids_by_assets(cur, pairs, cursor, _limit(limit))

    page = await run_in_threadpool(run)
    return _json(page.body())


@router.get("/lookup/shells/{aasIdentifier}", operation_id="GetAllAssetLinksById", tags=[DISCOVERY_TAG])
def get_asset_links(aasIdentifier: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:  # noqa: N803
    shell_id = _decode_id(aasIdentifier, "aasIdentifier")
    with db.transaction(_tenant_for(principal, shell_id)) as cur:
        links = repository.asset_links(cur, shell_id)
    if links is None:
        raise not_found()
    return _json(links)
