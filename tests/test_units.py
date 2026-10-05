"""Unit tests without a database: identifiers, views, validation gates, repository helpers."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest
from aas_fixtures import instance_env, material_type_env, passport_event, solid_type_env

from asset_shells import ids, repository, validation, views
from asset_shells.errors import ApiError

# ---------------------------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["https://id.madfam.io/aas/solid/fan-duct/0123456789abcdef/p1", "ñandú/ü", "a"])
def test_b64url_round_trip_without_padding(text):
    encoded = ids.b64url_encode(text)
    assert "=" not in encoded and ids.b64url_decode(encoded) == text
    assert ids.b64url_decode(encoded + "=" * (-len(encoded) % 4)) == text


@pytest.mark.parametrize("bad", ["", "***", "a b", "_w", "gICA"])
def test_b64url_rejects_garbage(bad):
    with pytest.raises(ids.InvalidEncodedId):
        ids.b64url_decode(bad)


def test_identifier_parsers():
    t = ids.parse_type_shell_id("https://id.madfam.io/aas/soft/wrap-dress/0123456789abcdef/p1")
    assert t == ids.TypeShellId("soft", "wrap-dress", "0123456789abcdef", 1)
    assert t.asset_id == "https://id.madfam.io/asset/soft/wrap-dress"
    assert t.submodel_prefix == "https://id.madfam.io/sm/soft/wrap-dress/0123456789abcdef/p1/"
    t12 = ids.parse_type_shell_id("https://id.madfam.io/aas/soft/wrap-dress/0123456789abcdef/p12")
    assert t12.projection == 12 and t12.revision == t.revision and t12 != t
    assert ids.parse_type_shell_id("https://id.madfam.io/aas/solid/Fan/0123456789abcdef/p1") is None
    assert ids.parse_type_shell_id("https://id.madfam.io/aas/machine/x/0123456789abcdef/p1") is None
    # Unversioned (pre-0.6.0) ids, and malformed versions, are not type shell ids.
    for bad in ("", "/p0", "/p01", "/v1", "/p1/"):
        assert ids.parse_type_shell_id(f"https://id.madfam.io/aas/solid/fan/0123456789abcdef{bad}") is None
    i = ids.parse_instance_shell_id("https://id.madfam.io/aas/instance/1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d")
    assert i is not None and i.asset_id.endswith("/asset/instance/1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d")
    assert ids.parse_instance_shell_id("https://id.madfam.io/aas/instance/1B4E28BA-2FA1-4D2B-9E6B-1C2F3A4B5C6D") is None
    assert ids.type_submodel_parts("https://id.madfam.io/sm/solid/a-b/0123456789abcdef/p3/Nameplate") == (
        "solid",
        "a-b",
        "0123456789abcdef",
        3,
        "Nameplate",
    )
    assert ids.type_submodel_parts("https://id.madfam.io/sm/solid/a-b/0123456789abcdef/Nameplate") is None
    assert ids.shell_projection_version({"extensions": [{"name": "ProjectionVersion", "value": "2"}]}) == 2
    assert ids.shell_projection_version({"extensions": [{"name": "ProjectionVersion", "value": "02"}]}) is None
    assert ids.shell_projection_version({}) is None
    assert ids.instance_submodel_parts("https://id.madfam.io/sm/instance/x/Nameplate") is None
    assert ids.is_type_asset_id("https://id.madfam.io/asset/material/pla-basic")
    assert ids.is_instance_asset_id("https://id.madfam.io/asset/instance/1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d")
    assert ids.is_concept_id("https://id.madfam.io/concept/wall_loops")
    assert not ids.is_concept_id("https://id.madfam.io/concept/Wall Loops")
    assert ids.is_template_id("https://id.madfam.io/smt/mating-interfaces/1/0")
    assert not ids.is_template_id("https://id.madfam.io/smt/mating-interfaces/01/0")
    assert ids.looks_like_instance_id("https://id.madfam.io/sm/instance/anything")
    assert ids.is_commit_sha("a" * 40) and not ids.is_commit_sha("A" * 40)
    assert ids.is_valid_tenant("org_1:x-y.z") and not ids.is_valid_tenant("") and not ids.is_valid_tenant("a b")


# ---------------------------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------------------------


def _sm(*elements) -> dict:
    return {"modelType": "Submodel", "id": "urn:sm", "submodelElements": list(elements)}


def test_value_only_covers_every_element_type():
    ref = {"type": "ExternalReference", "keys": [{"type": "GlobalReference", "value": "urn:r"}]}
    sm = _sm(
        {"modelType": "Property", "idShort": "S", "valueType": "xs:string", "value": "x"},
        {"modelType": "Property", "idShort": "D", "valueType": "xs:double", "value": "1.5"},
        {"modelType": "Property", "idShort": "E", "valueType": "xs:double", "value": "1e3"},
        {"modelType": "Property", "idShort": "Inf", "valueType": "xs:double", "value": "INF"},
        {"modelType": "Property", "idShort": "Bad", "valueType": "xs:int", "value": "x"},
        {"modelType": "Property", "idShort": "BadDec", "valueType": "xs:decimal", "value": "x"},
        {"modelType": "Property", "idShort": "B0", "valueType": "xs:boolean", "value": "0"},
        {"modelType": "Property", "idShort": "Bx", "valueType": "xs:boolean", "value": "maybe"},
        {"modelType": "Property", "idShort": "NoValue", "valueType": "xs:string"},
        {"modelType": "MultiLanguageProperty", "idShort": "M", "value": [{"language": "es", "text": "hola"}]},
        {"modelType": "File", "idShort": "F", "contentType": "application/pdf", "value": "/a.pdf"},
        {"modelType": "ReferenceElement", "idShort": "R", "value": ref},
        {
            "modelType": "AnnotatedRelationshipElement",
            "idShort": "A",
            "first": ref,
            "second": ref,
            "annotations": [{"modelType": "Property", "idShort": "Note", "valueType": "xs:string", "value": "n"}],
        },
        {"modelType": "BasicEventElement", "idShort": "Ev", "observed": ref, "direction": "output", "state": "on"},
        {
            "modelType": "Entity",
            "idShort": "En",
            "entityType": "CoManagedEntity",
            "statements": [{"modelType": "Property", "idShort": "Q", "valueType": "xs:int", "value": "2"}],
        },
        {"modelType": "Capability", "idShort": "Cap"},
        {"modelType": "Operation", "idShort": "Op"},
    )
    value = views.submodel_value(sm, "deep", "withoutBlobValue")
    assert value["S"] == "x" and value["D"] == 1.5 and value["E"] == 1000.0 and value["Inf"] == "INF"
    assert value["Bad"] == "x" and value["BadDec"] == "x" and value["B0"] is False and value["Bx"] == "maybe"
    assert "NoValue" not in value and "Cap" not in value and "Op" not in value
    assert value["M"] == [{"es": "hola"}]
    assert value["F"] == {"contentType": "application/pdf", "value": "/a.pdf"}
    assert value["R"] == ref and value["A"]["annotations"] == {"Note": "n"}
    assert value["Ev"] == {"observed": ref}
    assert value["En"] == {"statements": {"Q": 2}, "entityType": "CoManagedEntity"}
    core = views.element_value(sm["submodelElements"][14], "core", "withoutBlobValue")
    assert core == {"statements": {"Q": 2}, "entityType": "CoManagedEntity"}
    with pytest.raises(ApiError):
        views.element_value({"modelType": "Capability", "idShort": "Cap"}, "deep", "withoutBlobValue")


def test_core_level_on_element_keeps_direct_children_and_empties_containers():
    element = solid_type_env()["submodels"][2]["submodelElements"][0]
    value = views.element_value(element, "core", "withoutBlobValue")
    assert value["Frame"] == {} and value["Polarity"] == "female"
    normal = views.apply_modifiers(element, "core", "withoutBlobValue")
    assert "value" not in next(e for e in normal["value"] if e["idShort"] == "Frame")
    assert normal["value"][0]["value"] == "bolt_pattern"


def test_modifiers_do_not_mutate_stored_documents():
    sm = solid_type_env()["submodels"][1]
    before = copy.deepcopy(sm)
    views.apply_modifiers(sm, "core", "withoutBlobValue")
    views.metadata_of(sm)
    assert sm == before


def test_metadata_strips_values():
    prop = {"modelType": "Property", "idShort": "P", "valueType": "xs:int", "value": "1"}
    assert views.metadata_of(prop) == {"modelType": "Property", "idShort": "P", "valueType": "xs:int"}


def test_resolve_path_rules():
    sm = solid_type_env()["submodels"][1]
    assert views.resolve_path(sm, ["Parameters", 0, "Unit"])["value"] == "mm"
    assert views.resolve_path(sm, ["Parameters", "x"]) is None  # list items by index only
    assert views.resolve_path(sm, [0]) is None
    assert views.resolve_path(sm, ["Presets", 0]) is None


# ---------------------------------------------------------------------------------------------
# validation gates
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env", [solid_type_env(), material_type_env(), instance_env("1b4e28ba-2fa1-4d2b-9e6b-1c2f3a4b5c6d", None)]
)
def test_fixtures_pass_both_gates(env):
    validated, problems = validation.validate_environment(env)
    assert problems == [] and validated is not None


def _official_validator():
    raw = json.loads((validation.resources.files("asset_shells") / "schemas/aas-v3.1.2.json").read_text())
    return jsonschema.Draft201909Validator(
        {"$schema": raw["$schema"], "$ref": "#/definitions/Environment", "definitions": raw["definitions"]}
    )


def _variants() -> list[dict]:
    out = [solid_type_env(), material_type_env()]
    bad_type = solid_type_env()
    bad_type["submodels"][1]["submodelElements"][0]["value"][0]["modelType"] = "NotAType"
    missing = solid_type_env()
    del missing["submodels"][2]["submodelElements"][0]["value"][5]["second"]
    wrong_kind = solid_type_env()
    wrong_kind["submodels"][0]["submodelElements"][0]["value"] = "plain string"
    entity = solid_type_env()
    entity["submodels"][3]["submodelElements"][0]["entityType"] = "Robot"
    return [*out, bad_type, missing, wrong_kind, entity]


@pytest.mark.parametrize("doc", _variants())
def test_choice_dispatch_accepts_exactly_what_the_official_oneof_accepts(doc):
    official = not list(_official_validator().iter_errors(doc))
    dispatched = not validation.schema_problems(doc, "Environment")
    assert official == dispatched


def test_unknown_attributes_are_reported_with_pointers():
    env = solid_type_env()
    env["assetAdministrationShells"][0]["assetInformation"]["extra"] = 1
    env["submodels"][2]["submodelElements"][0]["value"][5]["first"]["keys"][0]["x~/y"] = 1
    problems = validation.unknown_attribute_problems(env, "Environment", "/environments/0")
    assert [p.path for p in problems] == [
        "/environments/0/assetAdministrationShells/0/assetInformation/extra",
        "/environments/0/submodels/2/submodelElements/0/value/5/first/keys/0/x~0~1y",
    ]


def test_schema_error_cap(monkeypatch):
    monkeypatch.setattr(validation, "MAX_SCHEMA_ERRORS", 2)
    env = {"submodels": [{"modelType": "Submodel"} for _ in range(5)]}
    problems = validation.schema_problems(env, "Environment")
    assert len(problems) == 3 and problems[-1].text.startswith("too many")


def test_element_validation():
    assert validation.validate_element(passport_event()) == []
    assert validation.validate_element("x")[0].code == "schema"
    assert validation.validate_element({"modelType": "Property"})[0].code == "schema"
    assert validation.validate_environment([])[1][0].code == "schema"


def test_content_hash_is_formatting_independent():
    a = {"b": 1, "a": [1, {"y": 2, "x": 1}]}
    b = json.loads(json.dumps(a, indent=4, sort_keys=False))
    assert validation.content_sha256(a) == validation.content_sha256(b)


# ---------------------------------------------------------------------------------------------
# repository helpers
# ---------------------------------------------------------------------------------------------


def test_cursor_round_trip_and_rejections():
    assert repository.decode_cursor(repository.encode_cursor(42)) == 42
    assert repository.decode_cursor(None) == 0
    for bad in ("", "!!", repository.encode_cursor(1).replace("c", "d") + "x"):
        with pytest.raises(ApiError):
            repository.decode_cursor(bad)
    import base64

    with pytest.raises(ApiError):
        repository.decode_cursor(base64.urlsafe_b64encode(b"q:1").decode().rstrip("="))
    with pytest.raises(ApiError):
        repository.decode_cursor(base64.urlsafe_b64encode(b"p:-1").decode().rstrip("="))


def test_page_list_and_reference_canonicalisation():
    page = repository.page_list([1, 2, 3], None, 2)
    assert page.items == [1, 2] and page.next_cursor
    assert repository.page_list([1, 2, 3], page.next_cursor, 2).body() == {"paging_metadata": {}, "result": [3]}
    a = {"type": "ExternalReference", "keys": [{"value": "v", "type": "GlobalReference"}], "referredSemanticId": {}}
    b = {"keys": [{"type": "GlobalReference", "value": "v"}], "type": "ExternalReference"}
    assert repository.canonical_reference(a) == repository.canonical_reference(b)
    assert repository.normalise_asset_name("GlobalAssetID") == "globalAssetId"
    assert repository.shell_references_submodel({"submodels": [{"keys": []}]}, "x") is False
