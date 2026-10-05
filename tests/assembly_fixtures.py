"""Assemblies A and B as a commons publisher builds them, with the pinned keystone (ASM-1 §5).

The inputs are the byte-identical commons copies in ``fixtures/assembly-commons`` (NOTICE beside them). Type
environments are built with ``hyperobjects_aas`` exactly as ``y4d-spec aas build`` does, so what the tests
publish is what a real release publishes, and the digests are the commons CI's.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path

from hyperobjects_aas import build_solid_environment
from hyperobjects_aas.assembly import build_assembly_environment
from hyperobjects_aas.resolver import bundled_standard_parts_dir
from y4d_spec.assembly import CompositeResolver, validate_assembly

COMMONS = Path(__file__).parent / "fixtures" / "assembly-commons"
A = "voron-2-4-class-350-motion-frame"
B = "fpv-5in-freestyle"
DIGESTS = {
    # A (15 components, solid 00e1765) on this keystone's catalog. Catalog digests enter A's identity:
    # hyperobjects-spec#50 gave A's catalog parts their collision envelopes (35867ffd… before #50), #49 gave
    # extrusion-2020's slots their travel (296caa36… before #49), #45 gave gt2-pulley-20t-5mm its
    # belt_engagement (24322cc0… before #45), #44 the blind joint (58caf081… before).
    A: "8172f814a71dfbe3e117cd1fb8a51523e4dbff4526a38e096f3eade208b1a389",
    # B with the camera cage (solid #136, 13 mates) on this keystone's catalog; f0db7bdb… at the commons'
    # SPEC_PIN 8c12194, before hyperobjects-spec#41 changed the fpv-frame entry, whose digest enters B's identity.
    B: "96166430930bbe5817f394ef382221f257f0f2de20b67cb38bec02c142e8f23c",
}
CARTRIDGES = (
    "tslot-corner",
    "tslot-2020",
    "endstop-mount",
    "chain-mount",
    "nema-bracket",
    "motor-soft-mount",
    "pcb-standoff",
    "battery-pad",
    "fpv-camera-cage",
)
BASE = "https://id.madfam.io/"
HAS_PART = "https://admin-shell.io/idta/HierarchicalStructures/HasPart/1/0"


def assembly_doc(slug: str) -> dict:
    return json.loads((COMMONS / "assemblies" / slug / "assembly.json").read_text("utf-8"))


@cache
def _cartridge_envs() -> dict[str, dict]:
    return {slug: build_solid_environment(COMMONS / slug) for slug in CARTRIDGES}


@cache
def _assembly_env(slug: str) -> dict:
    doc = assembly_doc(slug)
    report = validate_assembly(doc, CompositeResolver.for_directories(COMMONS, bundled_standard_parts_dir()))
    assert report.ok and report.digest == DIGESTS[slug], [str(f) for f in report.errors]
    return build_assembly_environment(doc, report)


def cartridge_envs() -> dict[str, dict]:
    return copy.deepcopy(_cartridge_envs())


def assembly_env(slug: str) -> dict:
    return copy.deepcopy(_assembly_env(slug))


def shell_id(env: dict) -> str:
    return env["assetAdministrationShells"][0]["id"]


def asset_id(env: dict) -> str:
    return env["assetAdministrationShells"][0]["assetInformation"]["globalAssetId"]


def submodel(env: dict, id_short: str) -> dict:
    return next(sm for sm in env["submodels"] if sm["idShort"] == id_short)


def child(element: dict, id_short: str) -> dict:
    kids = element.get("submodelElements") or element.get("statements") or element.get("value") or []
    return next(k for k in kids if k.get("idShort") == id_short)


def component_nodes(env: dict) -> dict[str, dict]:
    entry = child(submodel(env, "BillOfMaterials"), "EntryNode")
    out = {}
    for node in entry["statements"]:
        if node["modelType"] == "Entity":
            out[child(node, "ComponentId")["value"]] = node
    return out


def instance_assembly_env(uuid: str, type_env: dict, components: dict[str, dict]) -> dict:
    """A Phase-5 instance of a type assembly: one node per component (``ComponentId``), each naming the
    instance asset (cartridges) or the type's asset/URL (standard and external parts), HasPart from the
    EntryNode to each, ``derivedFrom`` the type assembly shell."""
    sm_id = f"{BASE}sm/instance/{uuid}/BillOfMaterials"
    entry_path = [{"type": "Submodel", "value": sm_id}, {"type": "Entity", "value": "EntryNode"}]
    statements, links = [], []
    for cid, node in components.items():
        statements.append(node | {"idShort": cid})
        links.append(
            {
                "modelType": "RelationshipElement",
                "idShort": f"HasPart_{cid}",
                "semanticId": {"type": "ExternalReference", "keys": [{"type": "GlobalReference", "value": HAS_PART}]},
                "first": {"type": "ModelReference", "keys": entry_path},
                "second": {"type": "ModelReference", "keys": [*entry_path, {"type": "Entity", "value": cid}]},
            }
        )
    bom = {
        "modelType": "Submodel",
        "id": sm_id,
        "idShort": "BillOfMaterials",
        "kind": "Instance",
        "submodelElements": [
            {
                "modelType": "Entity",
                "idShort": "EntryNode",
                "entityType": "SelfManagedEntity",
                "globalAssetId": f"{BASE}asset/instance/{uuid}",
                "statements": [*statements, *links],
            }
        ],
    }
    shell = {
        "modelType": "AssetAdministrationShell",
        "id": f"{BASE}aas/instance/{uuid}",
        "idShort": "assembled_product",
        "assetInformation": {
            "assetKind": "Instance",
            "globalAssetId": f"{BASE}asset/instance/{uuid}",
            "specificAssetIds": [{"name": "serial", "value": f"SN-{uuid[:8]}"}],
        },
        "derivedFrom": {
            "type": "ModelReference",
            "keys": [{"type": "AssetAdministrationShell", "value": shell_id(type_env)}],
        },
        "submodels": [{"type": "ModelReference", "keys": [{"type": "Submodel", "value": sm_id}]}],
    }
    return {"assetAdministrationShells": [shell], "submodels": [bom]}


def instance_node(cid: str, asset: str | None = None, url: str | None = None) -> dict:
    statements = [{"modelType": "Property", "idShort": "ComponentId", "valueType": "xs:string", "value": cid}]
    if url:
        statements.append({"modelType": "Property", "idShort": "Url", "valueType": "xs:anyURI", "value": url})
    node = {"modelType": "Entity", "idShort": cid, "statements": statements}
    if asset:
        node |= {"entityType": "SelfManagedEntity", "globalAssetId": asset}
    else:
        node |= {"entityType": "CoManagedEntity"}
    return node
