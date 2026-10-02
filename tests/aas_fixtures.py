"""Hand-built AAS v3.1 environments in the SEM-1 shapes (contract §1 identifiers, §5 submodels).

Small but realistic: a solid cartridge (fan-duct) with Nameplate, ParametricModel, MatingInterfaces
and BillOfMaterials submodels; a material card; an instance shell for a printed part with a
ManufacturingRecord whose `Events` list receives passport events. Built independently of the
projection library on purpose. Semantic ids are MADFAM template/concept ids only — no IDTA
template id is claimed here (SEM-1 §1, R85)."""

from __future__ import annotations

import copy
import hashlib

BASE = "https://id.madfam.io/"
SOLID_COMMONS = "solid-hyperobjects"
COMMONS_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
COMMONS_SHA_2 = "9c1e3f0a5b7d2e4c6a8b0d1f3e5a7c9b1d3f5e7a"


def tree_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def ext_ref(value: str) -> dict:
    return {"type": "ExternalReference", "keys": [{"type": "GlobalReference", "value": value}]}


def sm_ref(submodel_id: str) -> dict:
    return {"type": "ModelReference", "keys": [{"type": "Submodel", "value": submodel_id}]}


def aas_ref(shell_id: str) -> dict:
    return {"type": "ModelReference", "keys": [{"type": "AssetAdministrationShell", "value": shell_id}]}


def concept(term: str) -> dict:
    return ext_ref(f"{BASE}concept/{term}")


def prop(id_short: str, value: str, value_type: str = "xs:string", term: str | None = None) -> dict:
    p = {"modelType": "Property", "idShort": id_short, "valueType": value_type, "value": value}
    if term:
        p["semanticId"] = concept(term)
    return p


def mlp(id_short: str, en: str, es: str) -> dict:
    return {
        "modelType": "MultiLanguageProperty",
        "idShort": id_short,
        "value": [{"language": "en", "text": en}, {"language": "es", "text": es}],
    }


def parameter(param_id: str, default: str, lo: str, hi: str, unit: str) -> dict:
    return {
        "modelType": "SubmodelElementCollection",
        "semanticId": concept("parameter"),
        "value": [
            prop("ParameterId", param_id),
            prop("Default", default, "xs:double"),
            {"modelType": "Range", "idShort": "Range", "valueType": "xs:double", "min": lo, "max": hi},
            prop("Unit", unit, term="unit"),
        ],
    }


def solid_type_env(slug: str = "fan-duct", seed: str = "fan-duct@1.0.0", version: str = "1.0.0") -> dict:
    tree = tree_sha(seed)
    t16 = tree[:16]
    shell_id = f"{BASE}aas/solid/{slug}/{t16}"
    sm = f"{BASE}sm/solid/{slug}/{t16}/"
    major, minor, _patch = version.split(".")
    nameplate = {
        "modelType": "Submodel",
        "id": sm + "Nameplate",
        "idShort": "Nameplate",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/nameplate/1/0"),
        "submodelElements": [
            mlp("ManufacturerProductDesignation", "Fan-to-Duct Adapter", "Adaptador de ventilador a ducto"),
            prop("ManufacturerName", "Innovaciones MADFAM"),
            prop("DesignLicence", "CERN-OHL-W-2.0", term="design-licence"),
            prop("ManifestVersion", version),
        ],
    }
    parametric = {
        "modelType": "Submodel",
        "id": sm + "ParametricModel",
        "idShort": "ParametricModel",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/parametric-model/1/0"),
        "submodelElements": [
            {
                "modelType": "SubmodelElementList",
                "idShort": "Parameters",
                "typeValueListElement": "SubmodelElementCollection",
                "orderRelevant": True,
                "value": [
                    parameter("fan_size", "120", "40", "140", "mm"),
                    parameter("outlet_dia", "50", "20", "100", "mm"),
                ],
            },
            {
                "modelType": "SubmodelElementCollection",
                "idShort": "Presets",
                "value": [prop("Default", "fan_size=120;outlet_dia=50")],
            },
            {
                "modelType": "Blob",
                "idShort": "PreviewIcon",
                "contentType": "image/svg+xml",
                "value": "PHN2Zy8+",
            },
            {"modelType": "Property", "idShort": "WallLoopsMin", "valueType": "xs:int", "value": "3"},
            {"modelType": "Property", "idShort": "Printable", "valueType": "xs:boolean", "value": "true"},
        ],
    }
    mating = {
        "modelType": "Submodel",
        "id": sm + "MatingInterfaces",
        "idShort": "MatingInterfaces",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/mating-interfaces/1/0"),
        "submodelElements": [
            {
                "modelType": "SubmodelElementCollection",
                "idShort": "fan_screw_pattern",
                "value": [
                    prop("GeometryType", "bolt_pattern"),
                    prop("Polarity", "female", term="polarity"),
                    prop("SizeKey", "fan-120-corner-holes"),
                    prop("Symmetry", "4", "xs:int"),
                    {
                        "modelType": "SubmodelElementCollection",
                        "idShort": "Frame",
                        "value": [
                            prop("Part", "body"),
                            prop("Origin", "[0, 0, 0]"),
                            prop("Normal", "[0, 0, -1]"),
                        ],
                    },
                    {
                        "modelType": "RelationshipElement",
                        "idShort": "CompatibleWith",
                        "first": aas_ref(shell_id),
                        "second": ext_ref(f"{BASE}asset/solid/fan-adapter"),
                    },
                    {
                        "modelType": "ReferenceElement",
                        "idShort": "Standard",
                        "value": concept("pc-fan-corner-holes"),
                    },
                ],
            }
        ],
    }
    bom = {
        "modelType": "Submodel",
        "id": sm + "BillOfMaterials",
        "idShort": "BillOfMaterials",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/bill-of-materials/1/0"),
        "submodelElements": [
            {
                "modelType": "Entity",
                "idShort": "M4Screw",
                "entityType": "SelfManagedEntity",
                "globalAssetId": f"{BASE}asset/standard/m4x16-socket-head",
                "statements": [prop("Quantity", "4", "xs:int")],
            }
        ],
    }
    shell = {
        "modelType": "AssetAdministrationShell",
        "id": shell_id,
        "idShort": slug,
        "administration": {"version": major, "revision": minor},
        "assetInformation": {
            "assetKind": "Type",
            "globalAssetId": f"{BASE}asset/solid/{slug}",
            "specificAssetIds": [
                {"name": "commons", "value": SOLID_COMMONS},
                {"name": "slug", "value": slug},
                {"name": "tree_sha256", "value": tree},
            ],
        },
        "submodels": [sm_ref(s["id"]) for s in (nameplate, parametric, mating, bom)],
    }
    cds = [
        {
            "modelType": "ConceptDescription",
            "id": f"{BASE}concept/{term}",
            "idShort": term.replace("-", "_"),
            "description": [{"language": "en", "text": f"MADFAM lexicon term {term}"}],
        }
        for term in ("parameter", "unit", "polarity", "design-licence")
    ]
    return {
        "assetAdministrationShells": [shell],
        "submodels": [nameplate, parametric, mating, bom],
        "conceptDescriptions": cds,
    }


def material_type_env(slug: str = "bambu-tpu-95a", seed: str = "bambu-tpu-95a-card") -> dict:
    c16 = tree_sha(seed)[:16]
    shell_id = f"{BASE}aas/material/{slug}/{c16}"
    data = {
        "modelType": "Submodel",
        "id": f"{BASE}sm/material/{slug}/{c16}/MaterialData",
        "idShort": "MaterialData",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/material-data/1/0"),
        "submodelElements": [
            prop("MaterialClass", "tpu-95a", term="material-class"),
            prop("ShoreHardness", "95A"),
            prop("NozzleTemperatureMax", "240", "xs:int"),
        ],
    }
    shell = {
        "modelType": "AssetAdministrationShell",
        "id": shell_id,
        "idShort": slug,
        "assetInformation": {
            "assetKind": "Type",
            "globalAssetId": f"{BASE}asset/material/{slug}",
            "specificAssetIds": [{"name": "slug", "value": slug}],
        },
        "submodels": [sm_ref(data["id"])],
    }
    return {"assetAdministrationShells": [shell], "submodels": [data]}


def instance_env(uuid: str, derived_from: str | None, serial: str = "FD-0001") -> dict:
    shell_id = f"{BASE}aas/instance/{uuid}"
    record = {
        "modelType": "Submodel",
        "id": f"{BASE}sm/instance/{uuid}/ManufacturingRecord",
        "idShort": "ManufacturingRecord",
        "kind": "Instance",
        "semanticId": ext_ref(f"{BASE}smt/manufacturing-record/1/0"),
        "submodelElements": [
            prop("Goc1InstanceId", f"inst-{uuid[:8]}"),
            prop("VariablesSha256", tree_sha(f"vars-{uuid}")),
            prop("GcodeSha256", tree_sha(f"gcode-{uuid}")),
            {
                "modelType": "SubmodelElementList",
                "idShort": "Events",
                "typeValueListElement": "SubmodelElementCollection",
                "orderRelevant": True,
            },
        ],
    }
    shell = {
        "modelType": "AssetAdministrationShell",
        "id": shell_id,
        "idShort": "printed_part",
        "assetInformation": {
            "assetKind": "Instance",
            "globalAssetId": f"{BASE}asset/instance/{uuid}",
            "specificAssetIds": [{"name": "serial", "value": serial}],
        },
        "submodels": [sm_ref(record["id"])],
    }
    if derived_from:
        shell["derivedFrom"] = aas_ref(derived_from)
    return {"assetAdministrationShells": [shell], "submodels": [record]}


def passport_event(event_type: str = "print_completed", at: str = "2026-10-02T12:00:00Z") -> dict:
    return {
        "modelType": "SubmodelElementCollection",
        "semanticId": concept("passport-event"),
        "value": [
            prop("EventType", event_type),
            prop("OccurredAt", at, "xs:dateTime"),
            prop("MachineShell", f"{BASE}aas/instance/00000000-0000-4000-8000-0000000000aa"),
        ],
    }


def clone(doc: dict) -> dict:
    return copy.deepcopy(doc)
