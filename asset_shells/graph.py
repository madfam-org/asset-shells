"""The twin graph (ASM-1 §6): edges read from a published environment, written with it, walked by a
recursive CTE.

Edge sources, all read from what the publish stored (never from anything the caller says separately):

* ``has_part`` — every HSEBoM ``HasPart`` RelationshipElement of a ``BillOfMaterials`` submodel. The edge runs
  from the nearest asset-bearing entity on the ``first`` path (the EntryNode names the shell's own asset) to
  the ``second`` entity's ``globalAssetId``; a CoManaged entity with a ``Url`` (an external design) is
  identified by that URL. An entity with neither (a cartridge's mode node) contributes no edge.
* ``mates_with`` — every element of a ``Mates`` submodel (assemblies): from the asset of the ``first`` entity
  to the asset of the ``second``, the mate's annotations as ``props``.
* ``derived_from`` — an instance shell's ``derivedFrom``: from the instance asset to the type shell's asset.

Visibility is the database's: every query here runs in a transaction whose tenant context row-level security
applies, so a walk sees the public type graph plus the caller's own instance edges and nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

import psycopg

KINDS = ("has_part", "mates_with", "derived_from", "same_as")
MAX_DEPTH = 8
#: A walk returns at most this many edges and says so (``truncated``) when it stops early.
MAX_EDGES = 5000

BOM = "BillOfMaterials"
MATES = "Mates"
#: Entity statements copied onto a has_part edge (when present), so a walk is readable without the submodel.
_NODE_FACTS = ("ComponentId", "SourceType", "Slug", "Mode", "Part", "Key", "InstanceId", "HardwareId", "SizeKey")


@dataclass
class Edge:
    from_asset_id: str
    to_asset_id: str
    kind: str
    via_submodel_id: str | None
    props: dict = field(default_factory=dict)


def _children(element: dict) -> list[dict]:
    if "submodelElements" in element:
        value = element.get("submodelElements")
    elif element.get("modelType") == "Entity":
        value = element.get("statements")
    else:
        value = element.get("value")
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def _child(element: dict | None, id_short: str) -> dict | None:
    if element is None:
        return None
    return next((c for c in _children(element) if c.get("idShort") == id_short), None)


def _property(element: dict | None, id_short: str) -> str | None:
    found = _child(element, id_short)
    return found.get("value") if found and found.get("modelType") == "Property" else None


def _resolve_path(submodel: dict, reference: dict) -> list[dict] | None:
    """The chain of elements a ModelReference into ``submodel`` names (outermost first), or None."""
    keys = (reference or {}).get("keys") or []
    if not keys or keys[0].get("type") != "Submodel" or keys[0].get("value") != submodel.get("id"):
        return None
    chain, current = [], submodel
    for key in keys[1:]:
        current = _child(current, key.get("value", ""))
        if current is None:
            return None
        chain.append(current)
    return chain


def _node_asset(entity: dict | None) -> str | None:
    if entity is None or entity.get("modelType") != "Entity":
        return None
    return entity.get("globalAssetId") or _property(entity, "Url")


def _nearest_asset(chain: list[dict]) -> str | None:
    for element in reversed(chain):
        asset = _node_asset(element)
        if asset:
            return asset
    return None


def _node_props(entity: dict) -> dict:
    props = {"entity": entity.get("idShort")}
    for short in _NODE_FACTS:
        value = _property(entity, short)
        if value is not None:
            props[short[0].lower() + short[1:]] = value
    derived = _child(entity, "DerivedFrom")
    keys = ((derived or {}).get("value") or {}).get("keys") or []
    if keys and keys[-1].get("type") == "AssetAdministrationShell":
        props["typeShell"] = keys[-1].get("value")
    return props


def _relationships(element: dict) -> Iterable[dict]:
    for child in _children(element):
        if child.get("modelType") in ("RelationshipElement", "AnnotatedRelationshipElement"):
            yield child
        yield from _relationships(child)


HAS_PART = "https://admin-shell.io/idta/HierarchicalStructures/HasPart/1/0"


def _is_has_part(rel: dict) -> bool:
    keys = (rel.get("semanticId") or {}).get("keys") or []
    if keys:
        return keys[0].get("value") == HAS_PART
    return str(rel.get("idShort", "")).startswith("HasPart")


def bom_edges(submodel: dict) -> list[Edge]:
    out = []
    for rel in _relationships(submodel):
        if not _is_has_part(rel):
            continue
        first, second = _resolve_path(submodel, rel.get("first")), _resolve_path(submodel, rel.get("second"))
        if not first or not second:
            continue
        source, target = _nearest_asset(first), _node_asset(second[-1])
        if source and target and source != target:
            out.append(Edge(source, target, "has_part", submodel["id"], _node_props(second[-1])))
    return out


def mate_edges(submodel: dict, bom: dict | None) -> list[Edge]:
    if bom is None:
        return []
    out = []
    for element in _children(submodel):
        if element.get("modelType") not in ("RelationshipElement", "AnnotatedRelationshipElement"):
            continue
        first, second = _resolve_path(bom, element.get("first")), _resolve_path(bom, element.get("second"))
        source = _node_asset(first[-1]) if first else None
        target = _node_asset(second[-1]) if second else None
        if not source or not target:
            continue
        props = {
            a["idShort"][0].lower() + a["idShort"][1:]: a.get("value")
            for a in element.get("annotations") or []
            if isinstance(a, dict) and a.get("modelType") == "Property" and a.get("idShort")
        }
        props["mateElement"] = element.get("idShort")
        out.append(Edge(source, target, "mates_with", submodel["id"], props))
    return out


def environment_edges(shell: dict, submodels: Iterable[dict], derived_asset: str | None = None) -> list[Edge]:
    """Every edge one published shell carries (its submodels as stored)."""
    by_short = {sm.get("idShort"): sm for sm in submodels}
    edges: list[Edge] = []
    bom = by_short.get(BOM)
    if bom is not None:
        edges += bom_edges(bom)
    if by_short.get(MATES) is not None:
        edges += mate_edges(by_short[MATES], bom)
    asset = (shell.get("assetInformation") or {}).get("globalAssetId")
    if derived_asset and asset and derived_asset != asset:
        derived = ((shell.get("derivedFrom") or {}).get("keys") or [{}])[-1].get("value")
        edges.append(Edge(asset, derived_asset, "derived_from", None, {"typeShell": derived}))
    return edges


def insert_edges(cur: psycopg.Cursor, shell_id: str, tenant: str | None, edges: list[Edge]) -> int:
    for position, edge in enumerate(edges):
        cur.execute(
            """
            INSERT INTO asset_edges (from_asset_id, to_asset_id, kind, via_shell_id, via_submodel_id, position,
                                     props, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                edge.from_asset_id,
                edge.to_asset_id,
                edge.kind,
                shell_id,
                edge.via_submodel_id,
                position,
                json.dumps(edge.props, sort_keys=True),
                tenant,
            ),
        )
    return len(edges)


# ---------------------------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------------------------


_WALK = """
WITH RECURSIVE reach(asset_id, depth) AS (
    SELECT %(root)s::text, 0
  UNION
    SELECT CASE WHEN %(down)s THEN e.to_asset_id ELSE e.from_asset_id END, r.depth + 1
    FROM reach r
    JOIN asset_edges e
      ON (CASE WHEN %(down)s THEN e.from_asset_id ELSE e.to_asset_id END) = r.asset_id
    WHERE r.depth < %(depth)s AND e.kind = ANY(%(kinds)s)
),
nodes AS (SELECT asset_id, min(depth) AS depth FROM reach GROUP BY asset_id)
SELECT e.id, e.from_asset_id, e.to_asset_id, e.kind, e.via_shell_id, e.via_submodel_id, e.props,
       e.tenant_id IS NOT NULL AS instance, n.depth + 1 AS depth
FROM nodes n
JOIN asset_edges e ON (CASE WHEN %(down)s THEN e.from_asset_id ELSE e.to_asset_id END) = n.asset_id
WHERE n.depth < %(depth)s AND e.kind = ANY(%(kinds)s)
ORDER BY depth, e.id
LIMIT %(limit)s
"""


def walk(cur: psycopg.Cursor, root: str, direction: str, depth: int, kinds: list[str]) -> dict:
    """The sub-graph within ``depth`` hops of ``root`` (``down`` follows edges forward, ``up`` backward).

    The recursive CTE keeps one row per (asset, depth) with ``UNION``, so a cycle (two parts that mate both
    ways, or a closed loop of mates) costs at most ``depth`` rows per asset instead of one row per path."""
    down = direction == "down"
    cur.execute(_WALK, {"root": root, "down": down, "depth": depth, "kinds": kinds, "limit": MAX_EDGES + 1})
    rows = cur.fetchall()
    truncated = len(rows) > MAX_EDGES
    rows = rows[:MAX_EDGES]
    depth_of: dict[str, int] = {root: 0}
    edges = []
    for r in rows:
        far = r["to_asset_id"] if down else r["from_asset_id"]
        depth_of.setdefault(far, r["depth"])
        edges.append(
            {
                "from": r["from_asset_id"],
                "to": r["to_asset_id"],
                "kind": r["kind"],
                "depth": r["depth"],
                "instance": r["instance"],
                "viaShellId": r["via_shell_id"],
                "viaSubmodelId": r["via_submodel_id"],
                "props": r["props"],
            }
        )
    cur.execute(
        "SELECT global_asset_id, id, kind FROM shells WHERE global_asset_id = ANY(%s) ORDER BY seq",
        (list(depth_of),),
    )
    shells: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        shells.setdefault(r["global_asset_id"], []).append({"id": r["id"], "kind": r["kind"]})
    nodes = [
        {"assetId": asset, "depth": d, "shells": shells.get(asset, [])}
        for asset, d in sorted(depth_of.items(), key=lambda item: (item[1], item[0]))
    ]
    return {"nodes": nodes, "edges": edges, "truncated": truncated}


def root_is_known(cur: psycopg.Cursor, root: str) -> bool:
    """True iff the caller can see the root as a shell's asset or as either end of an edge."""
    cur.execute(
        """
        SELECT EXISTS (SELECT 1 FROM shells WHERE global_asset_id = %(r)s)
            OR EXISTS (SELECT 1 FROM asset_edges WHERE from_asset_id = %(r)s OR to_asset_id = %(r)s) AS known
        """,
        {"r": root},
    )
    return bool(cur.fetchone()["known"])
