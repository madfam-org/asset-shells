"""Validation of published AAS documents — two gates, both must pass.

1. **Official JSON Schema** (``schemas/aas-v3.1.2.json``, IDTA, CC-BY-4.0). Its ``*_choice`` oneOfs
   are evaluated as a modelType dispatch (same acceptance, linear instead of exponential cost). The
   schema leaves objects open, so a second, schema-guided walk rejects any attribute the metamodel
   does not define instead of storing and serving it. Errors carry a JSON pointer into the body.
2. **BaSyx Python SDK 2.2.0, strict decoder** — the metamodel constraints a schema cannot express
   (unique idShorts within a namespace, value parsing per ``valueType``, list element typing, …).

What is stored is the publisher's document as published (after both gates pass), not the SDK's
re-serialisation: the SDK reorders set-typed lists and, for an Entity, emits ``specificAssetIds: []``,
which the official schema rejects (minItems 1). Content hashes are taken over canonical JSON
(sorted keys, no whitespace), so a re-publish that differs only in formatting is a no-op.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import jsonschema
from basyx.aas import model as aas_model
from basyx.aas.adapter.json import json_deserialization

from .errors import Problem

MAX_SCHEMA_ERRORS = 50


def canonical_json(doc: object) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_sha256(doc: object) -> str:
    return hashlib.sha256(canonical_json(doc)).hexdigest()


def _model_type_const(definitions: dict, name: str) -> str | None:
    definition = definitions[name]
    for part in [definition, *definition.get("allOf", [])]:
        const = part.get("properties", {}).get("modelType", {}).get("const")
        if const:
            return const
    return None


def _dispatch_choices(definitions: dict) -> None:
    """Rewrite each ``*_choice`` oneOf as a modelType dispatch (``if``/``then`` per class).

    Every concrete class pins ``modelType`` with a ``const``, so exactly one branch can match and the
    dispatch accepts exactly what the oneOf accepts — but it validates the children once instead of
    once per candidate branch (the oneOf is exponential in nesting depth)."""
    for name, definition in definitions.items():
        branches = definition.get("oneOf")
        if not name.endswith("_choice") or not branches:
            continue
        targets = [b["$ref"].removeprefix("#/definitions/") for b in branches]
        consts = [_model_type_const(definitions, t) for t in targets]
        if any(c is None for c in consts):
            continue  # not dispatchable; keep the original oneOf
        definitions[name] = {
            "type": "object",
            "required": ["modelType"],
            "properties": {"modelType": {"enum": consts}},
            "allOf": [
                {"if": {"properties": {"modelType": {"const": c}}}, "then": {"$ref": f"#/definitions/{t}"}}
                for c, t in zip(consts, targets, strict=True)
            ],
        }


@lru_cache(maxsize=1)
def strict_schema() -> dict:
    """The official schema with the ``*_choice`` dispatch applied (same acceptance, linear cost)."""
    raw = resources.files("asset_shells").joinpath("schemas/aas-v3.1.2.json").read_text(encoding="utf-8")
    strict = copy.deepcopy(json.loads(raw))
    _dispatch_choices(strict["definitions"])
    return strict


def _ref_name(prop_schema: dict) -> str | None:
    ref = prop_schema.get("$ref")
    if ref is None and prop_schema.get("type") == "array":
        ref = prop_schema.get("items", {}).get("$ref")
    return ref.removeprefix("#/definitions/") if ref else None


@lru_cache(maxsize=1)
def _class_properties() -> dict[str, dict[str, str | None]]:
    """For every definition: its allowed property names (allOf flattened) -> referenced definition."""
    definitions = strict_schema()["definitions"]
    out: dict[str, dict[str, str | None]] = {}

    def collect(name: str, seen: frozenset[str]) -> dict[str, str | None]:
        definition = definitions[name]
        props: dict[str, str | None] = {}
        for part in [definition, *definition.get("allOf", [])]:
            target = part.get("$ref", "").removeprefix("#/definitions/")
            if target and target not in seen:
                props.update(collect(target, seen | {name}))
            for key, sub in part.get("properties", {}).items():
                props[key] = _ref_name(sub) or props.get(key)
        return props

    for name in definitions:
        out[name] = collect(name, frozenset())
    return out


@lru_cache(maxsize=1)
def _choice_targets() -> dict[str, dict[str, str]]:
    definitions = strict_schema()["definitions"]
    out: dict[str, dict[str, str]] = {}
    for name, definition in definitions.items():
        if name.endswith("_choice") and "allOf" in definition:
            out[name] = {
                b["if"]["properties"]["modelType"]["const"]: b["then"]["$ref"].removeprefix("#/definitions/")
                for b in definition["allOf"]
            }
    return out


def unknown_attribute_problems(doc: object, definition: str, base: str = "") -> list[Problem]:
    """Attributes the metamodel does not define. Runs after the schema passed, so shapes are sane.

    The official schema leaves ``additionalProperties`` open; storing and serving undefined
    attributes would make the service emit non-metamodel JSON, so they are rejected here."""
    classes, choices = _class_properties(), _choice_targets()
    problems: list[Problem] = []

    def walk(node: object, name: str, path: list) -> None:
        if len(problems) >= MAX_SCHEMA_ERRORS:
            return
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, name, [*path, i])
            return
        if name in choices:
            if not isinstance(node, dict):
                return
            name = choices[name].get(node.get("modelType"), "")
            if not name:
                return
        if not isinstance(node, dict) or name not in classes:
            return
        allowed = classes[name]
        if not allowed:
            return  # a non-object or open definition
        for key, value in node.items():
            if key not in allowed:
                problems.append(
                    Problem("unknown_attribute", f"'{key}' is not an attribute of {name}", _pointer(base, [*path, key]))
                )
            elif allowed[key]:
                walk(value, allowed[key], [*path, key])

    walk(doc, definition, [])
    return problems


@lru_cache(maxsize=4)
def _validator(definition: str) -> jsonschema.Draft201909Validator:
    schema = strict_schema()
    wrapper = {
        "$schema": schema["$schema"],
        "$ref": f"#/definitions/{definition}",
        "definitions": schema["definitions"],
    }
    return jsonschema.Draft201909Validator(wrapper)


def _pointer(base: str, path) -> str:
    parts = [base.rstrip("/")] if base else [""]
    parts += [str(p).replace("~", "~0").replace("/", "~1") for p in path]
    return "/".join(parts) or "/"


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    """For oneOf/anyOf failures, report the most specific branch error instead of 'is not valid'."""
    best = error
    while best.context:
        candidates = sorted(best.context, key=lambda e: len(e.absolute_path), reverse=True)
        nxt = candidates[0]
        if len(nxt.absolute_path) < len(best.absolute_path):
            break
        best = nxt
    return best


def schema_problems(doc: object, definition: str, base: str = "") -> list[Problem]:
    problems: list[Problem] = []
    for error in _validator(definition).iter_errors(doc):
        e = _deepest(error)
        problems.append(Problem("schema", e.message[:500], _pointer(base, e.absolute_path)))
        if len(problems) >= MAX_SCHEMA_ERRORS:
            problems.append(Problem("schema", "too many schema errors; output truncated", base or "/"))
            break
    return problems


def sdk_problems(environment: dict, base: str = "") -> list[Problem]:
    """Strict BaSyx decode of a whole Environment. Returns problems; never raises for bad input."""
    store: aas_model.DictIdentifiableStore = aas_model.DictIdentifiableStore()
    try:
        json_deserialization.read_aas_json_file_into(store, io.StringIO(json.dumps(environment)), failsafe=False)
    except (aas_model.AASConstraintViolation, KeyError, ValueError, TypeError, AttributeError) as exc:
        text = str(exc) or type(exc).__name__
        return [Problem("metamodel", f"{type(exc).__name__}: {text}"[:800], base or "/")]
    return []


def sdk_element_problems(element: dict, base: str = "") -> list[Problem]:
    """Strict BaSyx decode of a single SubmodelElement (wrapped in a throw-away submodel)."""
    probe = {
        "submodels": [
            {
                "modelType": "Submodel",
                "id": "urn:asset-shells:validation-probe",
                "submodelElements": [dict(element, idShort=element.get("idShort") or "Probe")],
            }
        ]
    }
    return sdk_problems(probe, base)


@dataclass(frozen=True)
class ValidatedEnvironment:
    shells: list[dict]
    submodels: list[dict]
    concept_descriptions: list[dict]


def validate_environment(environment: object, base: str = "") -> tuple[ValidatedEnvironment | None, list[Problem]]:
    if not isinstance(environment, dict):
        return None, [Problem("schema", "an AAS Environment must be a JSON object", base or "/")]
    problems = schema_problems(environment, "Environment", base) or unknown_attribute_problems(
        environment, "Environment", base
    )
    if problems:
        return None, problems
    problems = sdk_problems(environment, base)
    if problems:
        return None, problems
    return (
        ValidatedEnvironment(
            shells=list(environment.get("assetAdministrationShells", [])),
            submodels=list(environment.get("submodels", [])),
            concept_descriptions=list(environment.get("conceptDescriptions", [])),
        ),
        [],
    )


def validate_element(element: object, base: str = "") -> list[Problem]:
    if not isinstance(element, dict):
        return [Problem("schema", "a SubmodelElement must be a JSON object", base or "/")]
    problems = schema_problems(element, "SubmodelElement_choice", base) or unknown_attribute_problems(
        element, "SubmodelElement_choice", base
    )
    if problems:
        return problems
    return sdk_element_problems(element, base)
