"""The twin graph and assembly validation at /madfam/v1 (ASM-1 §6). Reads only.

* ``GET /graph?root={assetId}&direction=down|up&depth=1..8&kinds=…`` — anyone sees the type graph; a token with
  ``asset-shells:read`` and a tenant also sees that tenant's instance graph.
* ``GET /assemblies/{assetId}/validation`` — anyone for a type assembly; ``asset-shells:read`` + tenant for an
  instance assembly.

``assetId`` follows Part 2: UTF8-BASE64-URL-encoded. ``root`` also accepts a plain ``https://`` IRI (URL-encoded
by the client). Visibility is row-level security's, as on the read API: an instance id the caller cannot read
is a 401/403 by the same rules, and another tenant's instance is a 404 (existence is not disclosed).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from . import assemblies, db, graph
from .api_read import _tenant_for
from .auth import Principal, optional_principal
from .errors import bad_request, not_found
from .ids import InvalidEncodedId, b64url_decode, is_instance_asset_id, parse_type_shell_id

router = APIRouter(prefix="/madfam/v1", tags=["MADFAM twin graph"])


def _asset_id(raw: str, what: str, plain_ok: bool) -> str:
    if plain_ok and raw.startswith("https://"):
        return raw
    try:
        return b64url_decode(raw)
    except InvalidEncodedId:
        raise bad_request("bad_identifier", f"{what} must be UTF8-BASE64-URL-encoded") from None


def _kinds(raw: str | None) -> list[str]:
    if raw is None or raw == "":
        return list(graph.KINDS)
    kinds = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = sorted(set(kinds) - set(graph.KINDS))
    if unknown or not kinds:
        raise bad_request("bad_parameter", f"kinds is a comma-separated subset of {', '.join(graph.KINDS)}")
    return sorted(set(kinds), key=graph.KINDS.index)


@router.get("/graph", operation_id="GetTwinGraph")
def get_graph(
    root: str = Query(..., min_length=1),
    direction: str = Query("down", pattern="^(down|up)$"),
    depth: int = Query(1, ge=1, le=graph.MAX_DEPTH),
    kinds: str | None = Query(None),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    root_id = _asset_id(root, "root", plain_ok=True)
    wanted = _kinds(kinds)
    tenant = _tenant_for(principal, root_id)
    with db.transaction(tenant) as cur:
        if not graph.root_is_known(cur, root_id):
            raise not_found()
        result = graph.walk(cur, root_id, direction, depth, wanted)
    return JSONResponse({"root": root_id, "direction": direction, "depth": depth, "kinds": wanted, **result})


def _latest_assembly_shell(cur, asset_id: str, revision: str | None) -> tuple[str, list[str]]:
    cur.execute("SELECT id FROM shells WHERE global_asset_id = %s AND kind = 'type' ORDER BY seq", (asset_id,))
    ids = [r["id"] for r in cur.fetchall() if assemblies.is_assembly_shell(r["id"])]
    if not ids:
        raise not_found("No assembly shell with this asset id is visible to the caller")
    if revision is None:
        return ids[-1], ids
    chosen = [i for i in ids if parse_type_shell_id(i).digest16 == revision]
    if not chosen:
        raise not_found(f"No revision {revision} of this assembly")
    return chosen[0], ids


@router.get("/assemblies/{asset_id}/validation", operation_id="GetAssemblyValidation")
def get_assembly_validation(
    asset_id: str,
    revision: str | None = Query(None, pattern="^[0-9a-f]{16}$"),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    """The keystone's verdict on a stored assembly, re-run now over the stored type shells."""
    decoded = _asset_id(asset_id, "assetId", plain_ok=False)
    tenant = _tenant_for(principal, decoded)
    with db.transaction(tenant) as cur:
        fetch = assemblies.type_fetcher(cur)
        if is_instance_asset_id(decoded):
            return JSONResponse(_instance_validation(cur, decoded, fetch))
        shell_id, revisions = _latest_assembly_shell(cur, decoded, revision)
        shell, submodels = fetch(shell_id)
        check = assemblies.check_type_assembly(shell, list(submodels.values()), fetch)
    return JSONResponse({"assetId": decoded, "revisions": revisions, **check.body()})


def _instance_validation(cur, asset_id: str, fetch) -> dict:
    shell_id = asset_id.replace("/asset/instance/", "/aas/instance/")
    cur.execute("SELECT doc, derived_from FROM shells WHERE id = %s AND kind = 'instance'", (shell_id,))
    row = cur.fetchone()
    if row is None or not row["derived_from"] or not assemblies.is_assembly_shell(row["derived_from"]):
        raise not_found("No instance assembly with this asset id is visible to the caller")
    type_shell, type_submodels = fetch(row["derived_from"])
    type_check = assemblies.check_type_assembly(type_shell, list(type_submodels.values()), fetch)
    cur.execute("SELECT doc FROM submodels WHERE shell_id = %s ORDER BY seq", (shell_id,))
    submodels = [r["doc"] for r in cur.fetchall()]
    problems, _edges = assemblies.check_instance_assembly(cur, row["doc"], submodels, (type_shell, type_submodels))
    return {
        "assetId": asset_id,
        "shellId": shell_id,
        "ok": type_check.ok and not problems,
        "keystone": assemblies.keystone_version(),
        "instance": {"ok": not problems, "problems": [{"code": p.code, "text": p.text} for p in problems]},
        "type": type_check.body(),
    }
