"""The twin graph and assembly validation at /madfam/v1 (ASM-1 §6). Reads only.

* ``GET /graph?root={assetId}&direction=down|up&depth=1..8&kinds=…`` — anyone sees the type graph; a token with
  ``asset-shells:read`` and a tenant also sees that tenant's instance graph.
* ``GET /assemblies/{assetId}/validation`` — anyone for a type assembly; ``asset-shells:read`` + tenant for an
  instance assembly.
* ``GET /assets/{assetId}/projections`` — anyone: every stored revision of a type asset and its projection
  versions, with the current one marked.

**The current projection** (hyperobjects-spec 0.6.0 put the projection version into type shell ids,
``…/{hex16}/p{N}``): of the shells stored for one design revision, the one with the HIGHEST projection version.
Versions only grow (the keystone bumps one whenever the bytes it writes change), so the highest is the newest
projection of that revision. The latest revision is the one whose first shell was stored last.

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
from .ids import InvalidEncodedId, b64url_decode, is_instance_asset_id, is_type_asset_id, parse_type_shell_id

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


def _revisions(cur, asset_id: str, only_assemblies: bool = False) -> list[dict]:
    """The stored type revisions of an asset, oldest first, each with its projections (lowest version first)."""
    cur.execute("SELECT id FROM shells WHERE global_asset_id = %s AND kind = 'type' ORDER BY seq", (asset_id,))
    revisions: dict[str, dict] = {}
    for row in cur.fetchall():
        parsed = parse_type_shell_id(row["id"])
        if parsed is None or (only_assemblies and not assemblies.is_assembly_shell(row["id"])):
            continue
        entry = revisions.setdefault(parsed.digest16, {"revision": parsed.digest16, "projections": []})
        entry["projections"].append({"version": parsed.projection, "shellId": row["id"]})
    out = list(revisions.values())
    for entry in out:
        entry["projections"].sort(key=lambda p: p["version"])
        entry["current"] = entry["projections"][-1]["shellId"]
    return out


def _assembly_shell(cur, asset_id: str, revision: str | None, projection: int | None) -> tuple[str, list[str]]:
    """The shell to validate: the given (or latest) revision, at the given (or current) projection version."""
    revisions = _revisions(cur, asset_id, only_assemblies=True)
    if not revisions:
        raise not_found("No assembly shell with this asset id is visible to the caller")
    every = [p["shellId"] for r in revisions for p in r["projections"]]
    if revision is None:
        chosen = revisions[-1]
    else:
        chosen = next((r for r in revisions if r["revision"] == revision), None)
        if chosen is None:
            raise not_found(f"No revision {revision} of this assembly")
    if projection is None:
        return chosen["current"], every
    found = next((p["shellId"] for p in chosen["projections"] if p["version"] == projection), None)
    if found is None:
        raise not_found(f"No projection version {projection} of revision {chosen['revision']}")
    return found, every


@router.get("/assemblies/{asset_id}/validation", operation_id="GetAssemblyValidation")
def get_assembly_validation(
    asset_id: str,
    revision: str | None = Query(None, pattern="^[0-9a-f]{16}$"),
    projection: int | None = Query(None, ge=1),
    principal: Principal | None = Depends(optional_principal),
) -> JSONResponse:
    """The keystone's verdict on a stored assembly, re-run now over the stored type shells. Default: the latest
    revision at its current projection version."""
    decoded = _asset_id(asset_id, "assetId", plain_ok=False)
    tenant = _tenant_for(principal, decoded)
    with db.transaction(tenant) as cur:
        fetch = assemblies.type_fetcher(cur)
        if is_instance_asset_id(decoded):
            return JSONResponse(_instance_validation(cur, decoded, fetch))
        shell_id, revisions = _assembly_shell(cur, decoded, revision, projection)
        shell, submodels = fetch(shell_id)
        check = assemblies.check_type_assembly(shell, list(submodels.values()), fetch, publishing=False)
    return JSONResponse({"assetId": decoded, "revisions": revisions, **check.body()})


def _instance_validation(cur, asset_id: str, fetch) -> dict:
    shell_id = asset_id.replace("/asset/instance/", "/aas/instance/")
    cur.execute("SELECT doc, derived_from FROM shells WHERE id = %s AND kind = 'instance'", (shell_id,))
    row = cur.fetchone()
    if row is None or not row["derived_from"] or not assemblies.is_assembly_shell(row["derived_from"]):
        raise not_found("No instance assembly with this asset id is visible to the caller")
    type_shell, type_submodels = fetch(row["derived_from"])
    type_check = assemblies.check_type_assembly(type_shell, list(type_submodels.values()), fetch, publishing=False)
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


@router.get("/assets/{asset_id}/projections", operation_id="GetAssetProjections")
def get_asset_projections(asset_id: str, principal: Principal | None = Depends(optional_principal)) -> JSONResponse:
    """Every stored revision of a TYPE asset (oldest first) with its projection versions and the current one."""
    decoded = _asset_id(asset_id, "assetId", plain_ok=False)
    if not is_type_asset_id(decoded):
        raise bad_request("bad_identifier", "assetId is a type asset (…/asset/{solid|soft|material|assembly}/{slug})")
    with db.transaction(_tenant_for(principal, decoded)) as cur:
        revisions = _revisions(cur, decoded)
    if not revisions:
        raise not_found("No shell with this asset id is visible to the caller")
    return JSONResponse(
        {"assetId": decoded, "keystoneProjection": assemblies.keystone_projection_version(), "revisions": revisions}
    )
