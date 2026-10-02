"""Serialisation modifiers over stored AAS JSON (IDTA-01002 "Basic Operation Parameters").

* ``level``: ``deep`` (default) returns the whole hierarchy; ``core`` returns the requested object
  and its direct children only — a child container keeps its own attributes but loses its children.
* ``extent``: ``withoutBlobValue`` (default) omits Blob values; ``withBlobValue`` keeps them.
* ``$value``: the Value-Only serialisation of IDTA-01001 v3.1 (mappings, "Format Value").
* ``$metadata``: the object without its value-bearing attributes.
* ``$reference``: a ModelReference to the object.

Interpretation choices where the specification leaves room (documented in README):
* Value-Only with ``level=core``: containers among the direct children serialise as ``{}`` (collection,
  entity statements) or ``[]`` (list), as IDTA-01002 "Modifier Constraints" states; leaf children keep
  their value.
* MultiLanguageProperty Value-Only: an array of one-key objects ``[{"en": "…"}]`` (Part 1 text and
  example).
* Numbers: integral XSD types become JSON integers; decimal/double/float become JSON numbers;
  values that JSON cannot carry (INF, NaN, malformed) stay strings rather than being dropped.
"""

from __future__ import annotations

import copy
import math
import re
from decimal import Decimal, InvalidOperation

from .errors import bad_request

LEVELS = ("deep", "core")
EXTENTS = ("withoutBlobValue", "withBlobValue")

CONTAINER_KEYS = {
    "SubmodelElementCollection": "value",
    "SubmodelElementList": "value",
    "Entity": "statements",
    "AnnotatedRelationshipElement": "annotations",
}

_INTEGER_TYPES = {
    "xs:integer",
    "xs:int",
    "xs:long",
    "xs:short",
    "xs:byte",
    "xs:nonNegativeInteger",
    "xs:positiveInteger",
    "xs:nonPositiveInteger",
    "xs:negativeInteger",
    "xs:unsignedLong",
    "xs:unsignedInt",
    "xs:unsignedShort",
    "xs:unsignedByte",
}
_DECIMAL_TYPES = {"xs:decimal", "xs:double", "xs:float"}

_ID_SHORT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_INDEX = re.compile(r"\[(0|[1-9][0-9]*)\]")


def check_level(level: str) -> str:
    if level not in LEVELS:
        raise bad_request("bad_parameter", f"level must be one of {', '.join(LEVELS)}")
    return level


def check_extent(extent: str) -> str:
    if extent not in EXTENTS:
        raise bad_request("bad_parameter", f"extent must be one of {', '.join(EXTENTS)}")
    return extent


# ---------------------------------------------------------------------------------------------
# idShortPath
# ---------------------------------------------------------------------------------------------


def parse_id_short_path(path: str) -> list[str | int]:
    """``a.b[0].c`` -> ['a', 'b', 0, 'c'] (IDTA-01001 idShortPath grammar). 400 on malformed input."""
    tokens: list[str | int] = []
    m = _ID_SHORT.match(path)
    if not m:
        raise bad_request("bad_id_short_path", "an idShortPath starts with an idShort")
    tokens.append(m.group(0))
    pos = m.end()
    while pos < len(path):
        if path[pos] == ".":
            m = _ID_SHORT.match(path, pos + 1)
            if not m:
                raise bad_request("bad_id_short_path", f"expected an idShort at position {pos + 1}")
            tokens.append(m.group(0))
        elif path[pos] == "[":
            m = _INDEX.match(path, pos)
            if not m:
                raise bad_request("bad_id_short_path", f"expected [index] at position {pos}")
            tokens.append(int(m.group(1)))
        else:
            raise bad_request("bad_id_short_path", f"unexpected character at position {pos}")
        pos = m.end()
    return tokens


def _children(element: dict) -> list[dict]:
    key = CONTAINER_KEYS.get(element.get("modelType", ""))
    return element.get(key, []) if key else []


def resolve_path(submodel: dict, tokens: list[str | int]) -> dict | None:
    """Walk an idShortPath from a submodel. Indexes address SubmodelElementList items only."""
    current: list[dict] = submodel.get("submodelElements", [])
    node: dict | None = None
    parent_type = "Submodel"
    for token in tokens:
        if isinstance(token, int):
            if parent_type != "SubmodelElementList" or token >= len(current):
                return None
            node = current[token]
        else:
            if parent_type == "SubmodelElementList":
                return None  # list items are addressed by index only
            node = next((e for e in current if e.get("idShort") == token), None)
            if node is None:
                return None
        parent_type = node.get("modelType", "")
        current = _children(node)
    return node


# ---------------------------------------------------------------------------------------------
# level / extent on the normal serialisation
# ---------------------------------------------------------------------------------------------


def _strip_blobs(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _strip_blobs(item)
    elif isinstance(node, dict):
        if node.get("modelType") == "Blob":
            node.pop("value", None)
        for key in ("submodelElements", "value", "statements", "annotations"):
            if isinstance(node.get(key), list):
                _strip_blobs(node[key])


def _truncate_children(element: dict) -> None:
    key = CONTAINER_KEYS.get(element.get("modelType", ""))
    if key:
        element.pop(key, None)


def apply_modifiers(obj: dict, level: str, extent: str) -> dict:
    """``obj`` is a Submodel or a SubmodelElement (never mutated)."""
    out = copy.deepcopy(obj)
    if level == "core":
        children = out.get("submodelElements") if out.get("modelType") == "Submodel" else _children(out)
        for child in children or []:
            _truncate_children(child)
    if extent == "withoutBlobValue":
        _strip_blobs(out)
    return out


# ---------------------------------------------------------------------------------------------
# Value-Only
# ---------------------------------------------------------------------------------------------


def _typed(value: str, value_type: str) -> object:
    if value_type == "xs:boolean":
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
        return value
    if value_type in _INTEGER_TYPES:
        try:
            return int(value)
        except ValueError:
            return value
    if value_type in _DECIMAL_TYPES:
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        if not number.is_finite():
            return value
        if number == number.to_integral_value() and "e" not in value.lower() and "." not in value:
            return int(number)
        f = float(number)
        return f if math.isfinite(f) else value
    return value


def _value_of(element: dict, extent: str, depth: int, max_depth: int | None) -> tuple[bool, object]:
    """(has_value, value) for one element. ``max_depth`` limits container expansion (level=core)."""
    mt = element.get("modelType")
    expand = max_depth is None or depth < max_depth
    if mt == "Property":
        return ("value" in element, _typed(element.get("value", ""), element.get("valueType", "xs:string")))
    if mt == "MultiLanguageProperty":
        values = element.get("value")
        return (bool(values), [{v["language"]: v["text"]} for v in values or []])
    if mt == "Range":
        vt = element.get("valueType", "xs:string")
        out = {k: _typed(element[k], vt) for k in ("min", "max") if k in element}
        return (bool(out), out)
    if mt in ("File", "Blob"):
        out = {}
        if "contentType" in element:
            out["contentType"] = element["contentType"]
        if "value" in element and (mt == "File" or extent == "withBlobValue"):
            out["value"] = element["value"]
        return (bool(out), out)
    if mt == "ReferenceElement":
        return ("value" in element, element.get("value"))
    if mt in ("RelationshipElement", "AnnotatedRelationshipElement"):
        out = {k: element[k] for k in ("first", "second") if k in element}
        if mt == "AnnotatedRelationshipElement" and element.get("annotations"):
            out["annotations"] = (
                collection_value(element["annotations"], extent, depth + 1, max_depth) if expand else {}
            )
        return (bool(out), out)
    if mt == "BasicEventElement":
        return ("observed" in element, {"observed": element.get("observed")})
    if mt == "SubmodelElementCollection":
        return (True, collection_value(element.get("value", []), extent, depth + 1, max_depth) if expand else {})
    if mt == "SubmodelElementList":
        if not expand:
            return (True, [])
        items = []
        for item in element.get("value", []):
            has, value = _value_of(item, extent, depth + 1, max_depth)
            if has:
                items.append(value)
        return (True, items)
    if mt == "Entity":
        out: dict = {}
        if element.get("statements") is not None:
            out["statements"] = collection_value(element["statements"], extent, depth + 1, max_depth) if expand else {}
        for key in ("entityType", "globalAssetId", "specificAssetIds"):
            if key in element:
                out[key] = element[key]
        return (True, out)
    return (False, None)  # Capability, Operation: not part of the Value-Only scope


def collection_value(elements: list[dict], extent: str, depth: int = 0, max_depth: int | None = None) -> dict:
    out: dict = {}
    for element in elements:
        has, value = _value_of(element, extent, depth, max_depth)
        if has and element.get("idShort"):
            out[element["idShort"]] = value
    return out


def submodel_value(submodel: dict, level: str, extent: str) -> dict:
    # The submodel is the requested object; its elements are the direct children (not expanded in core).
    return collection_value(submodel.get("submodelElements", []), extent, 0, 0 if level == "core" else None)


def element_value(element: dict, level: str, extent: str) -> object:
    has, value = _value_of(element, extent, 0, 1 if level == "core" else None)
    if not has:
        raise bad_request("no_value", "this element type has no Value-Only serialisation")
    return value


# ---------------------------------------------------------------------------------------------
# $metadata and $reference
# ---------------------------------------------------------------------------------------------

_VALUE_ATTRIBUTES = {
    "Submodel": ("submodelElements",),
    "Property": ("value", "valueId"),
    "MultiLanguageProperty": ("value", "valueId"),
    "Range": ("min", "max"),
    "File": ("value",),
    "Blob": ("value",),
    "ReferenceElement": ("value",),
    "RelationshipElement": ("first", "second"),
    "AnnotatedRelationshipElement": ("first", "second", "annotations"),
    "SubmodelElementCollection": ("value",),
    "SubmodelElementList": ("value",),
    "Entity": ("statements", "globalAssetId", "specificAssetIds"),
    "BasicEventElement": ("observed",),
}


def metadata_of(obj: dict) -> dict:
    out = copy.deepcopy(obj)
    for key in _VALUE_ATTRIBUTES.get(out.get("modelType", ""), ()):
        out.pop(key, None)
    return out


def shell_reference(shell_id: str) -> dict:
    return {"type": "ModelReference", "keys": [{"type": "AssetAdministrationShell", "value": shell_id}]}


def submodel_reference(submodel_id: str) -> dict:
    return {"type": "ModelReference", "keys": [{"type": "Submodel", "value": submodel_id}]}
