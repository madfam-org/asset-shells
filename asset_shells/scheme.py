"""SEM-1 rules on whole environments, applied after the metamodel gates (validation.py) pass.

Type environments (``PUT /madfam/v1/type-environments/{commons}/{sha}``):
* shell ids ``https://id.madfam.io/aas/{solid|soft|material}/{slug}/{hex16}``; the kind must be one the
  commons may publish; ``assetKind`` = Type; ``globalAssetId`` = ``…/asset/{kind}/{slug}``;
  no ``derivedFrom`` (SEM-1 §5);
* design shells (solid/soft) carry the specificAssetIds ``commons`` (= the path commons), ``slug``
  (= the id slug) and ``tree_sha256`` (64 hex, starting with the id's hex16);
* every submodel id is ``…/sm/{kind}/{slug}/{hex16}/{SubmodelIdShort}`` with ``SubmodelIdShort`` equal
  to the submodel's idShort, is referenced by exactly one shell of the same design revision, and every
  reference resolves inside the environment;
* concept descriptions are MADFAM concepts ``…/concept/{term}``.

Instance environments (``POST /madfam/v1/instances``): exactly one shell
``…/aas/instance/{uuid}`` with ``assetKind`` = Instance and ``globalAssetId`` = ``…/asset/instance/{uuid}``,
submodels ``…/sm/instance/{uuid}/{SubmodelIdShort}``, an optional ``derivedFrom`` naming a type shell,
no concept descriptions.

Deviation recorded in the report: SEM-1 §1 names no submodel id for material shells; the analogous
``…/sm/material/{slug}/{content16}/{SubmodelIdShort}`` is accepted.
"""

from __future__ import annotations

from .errors import Problem
from .ids import (
    COMMONS_KINDS,
    DESIGN_KINDS,
    InstanceShellId,
    TypeShellId,
    instance_submodel_parts,
    is_concept_id,
    is_sha256,
    parse_instance_shell_id,
    parse_type_shell_id,
    type_submodel_parts,
)


def _submodel_ref_ids(shell: dict, base: str, problems: list[Problem]) -> list[str]:
    ids = []
    for i, ref in enumerate(shell.get("submodels", [])):
        keys = ref.get("keys", [])
        if ref.get("type") != "ModelReference" or len(keys) != 1 or keys[0].get("type") != "Submodel":
            problems.append(
                Problem(
                    "reference",
                    "a submodel reference is a ModelReference with one Submodel key",
                    f"{base}/submodels/{i}",
                )
            )
            continue
        ids.append(keys[0]["value"])
    return ids


def _specific(shell: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sid in shell.get("assetInformation", {}).get("specificAssetIds", []):
        out.setdefault(sid.get("name", ""), []).append(sid.get("value", ""))
    return out


def _check_type_shell(shell: dict, tsid: TypeShellId, commons: str, base: str, problems: list[Problem]) -> None:
    info = shell.get("assetInformation", {})
    if tsid.kind not in COMMONS_KINDS[commons]:
        problems.append(
            Problem("id_scheme", f"commons '{commons}' does not publish '{tsid.kind}' shells", f"{base}/id")
        )
    if info.get("assetKind") != "Type":
        problems.append(
            Problem("asset_kind", "type shells have assetKind 'Type'", f"{base}/assetInformation/assetKind")
        )
    if info.get("globalAssetId") != tsid.asset_id:
        problems.append(
            Problem("id_scheme", f"globalAssetId must be '{tsid.asset_id}'", f"{base}/assetInformation/globalAssetId")
        )
    if "derivedFrom" in shell:
        problems.append(Problem("derived_from", "type shells carry no derivedFrom (SEM-1 §5)", f"{base}/derivedFrom"))
    specific = _specific(shell)
    for name, values in specific.items():
        if len(values) > 1:
            problems.append(
                Problem(
                    "asset_ids",
                    f"specificAssetId '{name}' appears more than once",
                    f"{base}/assetInformation/specificAssetIds",
                )
            )
    path = f"{base}/assetInformation/specificAssetIds"
    required = ("commons", "slug", "tree_sha256") if tsid.kind in DESIGN_KINDS else ()
    for name in required:
        if name not in specific:
            problems.append(Problem("asset_ids", f"design shells carry the specificAssetId '{name}' (SEM-1 §5)", path))
    if "commons" in specific and specific["commons"][0] != commons:
        problems.append(Problem("asset_ids", f"specificAssetId 'commons' must be '{commons}'", path))
    if "slug" in specific and specific["slug"][0] != tsid.slug:
        problems.append(Problem("asset_ids", f"specificAssetId 'slug' must be '{tsid.slug}'", path))
    if "tree_sha256" in specific:
        tree = specific["tree_sha256"][0]
        if not is_sha256(tree) or not tree.startswith(tsid.digest16):
            problems.append(
                Problem(
                    "asset_ids",
                    "specificAssetId 'tree_sha256' must be 64 lower-case hex starting "
                    "with the shell id's 16-hex digest",
                    path,
                )
            )


def type_environment_problems(env: dict, commons: str, base: str) -> list[Problem]:
    problems: list[Problem] = []
    submodel_owner: dict[str, str] = {}
    shell_prefix: dict[str, str] = {}
    shells = env.get("assetAdministrationShells", [])
    if not shells:
        problems.append(Problem("environment", "a type environment publishes at least one shell", base or "/"))
    for i, shell in enumerate(shells):
        sbase = f"{base}/assetAdministrationShells/{i}"
        tsid = parse_type_shell_id(shell.get("id", ""))
        if tsid is None:
            problems.append(
                Problem(
                    "id_scheme",
                    "type shell ids are https://id.madfam.io/aas/{solid|soft|material}/{slug}/{hex16} (SEM-1 §1)",
                    f"{sbase}/id",
                )
            )
            continue
        _check_type_shell(shell, tsid, commons, sbase, problems)
        shell_prefix[shell["id"]] = tsid.submodel_prefix
        for sm_id in _submodel_ref_ids(shell, sbase, problems):
            if not sm_id.startswith(tsid.submodel_prefix):
                problems.append(
                    Problem(
                        "id_scheme",
                        f"submodel '{sm_id}' is not in this shell's namespace '{tsid.submodel_prefix}'",
                        f"{sbase}/submodels",
                    )
                )
            if sm_id in submodel_owner and submodel_owner[sm_id] != shell["id"]:
                problems.append(
                    Problem("reference", f"submodel '{sm_id}' is referenced by two shells", f"{sbase}/submodels")
                )
            submodel_owner[sm_id] = shell["id"]
    present = set()
    for k, submodel in enumerate(env.get("submodels", [])):
        mbase = f"{base}/submodels/{k}"
        sm_id = submodel.get("id", "")
        present.add(sm_id)
        parts = type_submodel_parts(sm_id)
        if parts is None:
            problems.append(
                Problem(
                    "id_scheme",
                    "type submodel ids are https://id.madfam.io/sm/{solid|soft|material}/"
                    "{slug}/{hex16}/{SubmodelIdShort} (SEM-1 §1)",
                    f"{mbase}/id",
                )
            )
            continue
        if submodel.get("idShort") != parts[3]:
            problems.append(
                Problem("id_scheme", f"the submodel idShort must be '{parts[3]}' (last id segment)", f"{mbase}/idShort")
            )
        if sm_id not in submodel_owner:
            problems.append(
                Problem(
                    "orphan_submodel", "every submodel is referenced by a shell in the same environment", f"{mbase}/id"
                )
            )
    for sm_id, owner in submodel_owner.items():
        if sm_id not in present:
            problems.append(
                Problem(
                    "dangling_reference",
                    f"shell '{owner}' references submodel '{sm_id}', which is not in the environment",
                    base or "/",
                )
            )
    for c, cd in enumerate(env.get("conceptDescriptions", [])):
        if not is_concept_id(cd.get("id", "")):
            problems.append(
                Problem(
                    "id_scheme",
                    "concept descriptions are MADFAM concepts https://id.madfam.io/concept/{term}",
                    f"{base}/conceptDescriptions/{c}/id",
                )
            )
    return problems


def instance_environment_problems(
    env: dict, base: str = ""
) -> tuple[list[Problem], InstanceShellId | None, str | None]:
    """(problems, parsed instance id, derivedFrom type shell id or None)."""
    problems: list[Problem] = []
    shells = env.get("assetAdministrationShells", [])
    if len(shells) != 1:
        return [Problem("environment", "an instance publish carries exactly one shell", base or "/")], None, None
    if env.get("conceptDescriptions"):
        problems.append(
            Problem("environment", "instance publishes carry no concept descriptions", f"{base}/conceptDescriptions")
        )
    shell = shells[0]
    sbase = f"{base}/assetAdministrationShells/0"
    isid = parse_instance_shell_id(shell.get("id", ""))
    if isid is None:
        problems.append(
            Problem(
                "id_scheme",
                "instance shell ids are https://id.madfam.io/aas/instance/{uuid} (lower-case UUID, SEM-1 §1)",
                f"{sbase}/id",
            )
        )
        return problems, None, None
    info = shell.get("assetInformation", {})
    if info.get("assetKind") != "Instance":
        problems.append(
            Problem("asset_kind", "instance shells have assetKind 'Instance'", f"{sbase}/assetInformation/assetKind")
        )
    if info.get("globalAssetId") != isid.asset_id:
        problems.append(
            Problem("id_scheme", f"globalAssetId must be '{isid.asset_id}'", f"{sbase}/assetInformation/globalAssetId")
        )
    derived: str | None = None
    if "derivedFrom" in shell:
        ref = shell["derivedFrom"]
        keys = ref.get("keys", [])
        if (
            ref.get("type") != "ModelReference"
            or len(keys) != 1
            or keys[0].get("type") != "AssetAdministrationShell"
            or parse_type_shell_id(keys[0].get("value", "")) is None
        ):
            problems.append(
                Problem(
                    "derived_from", "derivedFrom is a ModelReference to one MADFAM type shell", f"{sbase}/derivedFrom"
                )
            )
        else:
            derived = keys[0]["value"]
    referenced = set(_submodel_ref_ids(shell, sbase, problems))
    present = set()
    for k, submodel in enumerate(env.get("submodels", [])):
        mbase = f"{base}/submodels/{k}"
        parts = instance_submodel_parts(submodel.get("id", ""))
        present.add(submodel.get("id", ""))
        if parts is None or parts[0] != isid.uuid:
            problems.append(
                Problem(
                    "id_scheme", f"instance submodel ids are {isid.submodel_prefix}{{SubmodelIdShort}}", f"{mbase}/id"
                )
            )
            continue
        if submodel.get("idShort") != parts[1]:
            problems.append(Problem("id_scheme", f"the submodel idShort must be '{parts[1]}'", f"{mbase}/idShort"))
        if submodel.get("id") not in referenced:
            problems.append(Problem("orphan_submodel", "every submodel is referenced by the shell", f"{mbase}/id"))
    for sm_id in sorted(referenced - present):
        problems.append(
            Problem(
                "dangling_reference",
                f"the shell references '{sm_id}', which is not in the request",
                f"{sbase}/submodels",
            )
        )
    return problems, isid, derived
