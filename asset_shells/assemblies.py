"""Assemblies (ASM-1 §6): the keystone validator re-run over the type shells this store holds.

A type assembly is accepted only when the keystone says so, here, against what asset-shells itself stores:

1. its document is read back from the ``AssemblyDocument`` Blob (``hyperobjects_aas.assembly``);
2. every cartridge component is resolved from the STORED type shell its BillOfMaterials node names
   (``DerivedFrom``) — ``ParametricModel``, ``GeometryProvision``, ``MatingInterfaces``, ``RequirementProfile``
   (``hyperobjects_aas.resolver``); standard parts from the catalog bundled with the pinned keystone; external
   designs from their inline facts;
3. ``validate_assembly`` runs (schema, resolution, mating rule, placement, closure of every mate, reachability,
   digest); any error is a 422 that carries the whole report;
4. the keystone then re-projects the document from that report, and the published shell and submodels must be
   exactly that projection (same digest, same BoM, same mates, same placements). The edges the publish writes
   are therefore read from content the service derived itself, not from whatever a publisher claimed.

Projection versions (hyperobjects-spec 0.6.0): the keystone projects at ITS ``PROJECTION_VERSION``, so a publish
must carry that version (another one is a 422 ``assembly_projection_version``: build with the service's pin). A
STORED shell of an older version is still re-validated on read (``/validation``): the document, resolution and
mating checks run as always, and the byte comparison is reported as not applicable (``projection.compared`` is
false) because the keystone no longer writes that version's bytes.

An INSTANCE assembly (Phase 5) is an instance shell whose ``derivedFrom`` is a type assembly shell. It must
match its type: its BillOfMaterials names exactly the type's components (``ComponentId``); a cartridge
component is an instance asset of this tenant whose shell is ``derivedFrom`` the type component's revision;
a standard or external component is referenced, not serialised, so it names the same asset (or URL) as the
type. Matching is by design REVISION (kind, slug, digest16): an instance derived from any projection version of
the type component's revision matches. Its ``mates_with`` edges are the type's mates, mapped onto the instance
components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache

import psycopg
from hyperobjects_aas import ids as keystone_ids
from hyperobjects_aas.assembly import (
    AssemblyProjectionError,
    assembly_document_from_environment,
    build_assembly_environment,
    component_type_shells,
)
from hyperobjects_aas.resolver import EnvironmentCartridgeResolver, bundled_standard_parts_dir
from y4d_spec.assembly import CompositeResolver, StandardPartsResolver, validate_assembly
from y4d_spec.assembly.cli import report_as_dict

from .errors import Problem
from .graph import BOM, MATES, Edge, _child, _children, _node_asset, _property, _resolve_path
from .ids import ASSEMBLY_KIND, parse_instance_shell_id, parse_type_shell_id
from .validation import canonical_json

#: pending (same-request) shells first, then stored type rows: shell id -> (shell, {idShort: submodel}).
Bundle = tuple[dict, dict[str, dict]]


@cache
def _standard_parts() -> StandardPartsResolver:
    return StandardPartsResolver(bundled_standard_parts_dir())


def keystone_version() -> str:
    from importlib.metadata import version

    return version("hyperobjects-spec")


def keystone_projection_version() -> int:
    """The projection version the pinned keystone writes (read at call time)."""
    return keystone_ids.PROJECTION_VERSION


def is_assembly_shell(shell_id: str) -> bool:
    parsed = parse_type_shell_id(shell_id)
    return parsed is not None and parsed.kind == ASSEMBLY_KIND


def type_fetcher(cur: psycopg.Cursor, pending: dict[str, Bundle] | None = None):
    """``fetch(shell id)`` for the resolver: a shell of the same request, else a stored TYPE shell."""

    def fetch(shell_id: str) -> Bundle | None:
        if pending and shell_id in pending:
            return pending[shell_id]
        cur.execute("SELECT doc FROM shells WHERE id = %s AND kind = 'type'", (shell_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("SELECT id_short, doc FROM submodels WHERE shell_id = %s ORDER BY seq", (shell_id,))
        return row["doc"], {r["id_short"]: r["doc"] for r in cur.fetchall()}

    return fetch


@dataclass
class AssemblyCheck:
    shell_id: str
    problems: list[Problem] = field(default_factory=list)
    report: dict | None = None
    #: Whether the stored bytes were compared with the keystone's projection (False for an older version).
    compared: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems

    def body(self) -> dict:
        parsed = parse_type_shell_id(self.shell_id)
        return {
            "shellId": self.shell_id,
            "ok": self.ok,
            "keystone": keystone_version(),
            "projection": {
                "shell": parsed.projection if parsed else None,
                "keystone": keystone_projection_version(),
                "compared": self.compared,
            },
            "problems": [{"code": p.code, "text": p.text, "path": p.path} for p in self.problems],
            "report": self.report,
        }


def check_type_assembly(
    shell: dict, submodels: list[dict], fetch, base: str = "", *, publishing: bool = True
) -> AssemblyCheck:
    """Re-validate one type assembly shell with the keystone and require it to be the keystone's projection.

    ``publishing``: a publish must carry the keystone's projection version (else a 422); a read of a stored shell
    of another version re-validates the assembly and skips only the byte comparison."""
    check = AssemblyCheck(shell["id"])
    parsed = parse_type_shell_id(shell["id"])
    current = keystone_projection_version()
    if publishing and parsed is not None and parsed.projection != current:
        check.problems.append(
            Problem(
                "assembly_projection_version",
                f"the shell is projection version {parsed.projection}; this service's keystone "
                f"({keystone_version()}) projects version {current}: build with the same keystone pin",
                base or "/",
            )
        )
        return check
    env = {"assetAdministrationShells": [shell], "submodels": submodels}
    try:
        doc = assembly_document_from_environment(env)
    except AssemblyProjectionError as exc:
        check.problems.append(Problem("assembly_document", str(exc), base or "/"))
        return check
    resolver = CompositeResolver(
        cartridge=EnvironmentCartridgeResolver(component_type_shells(env), fetch),
        standard=_standard_parts(),
    )
    report = validate_assembly(doc, resolver)
    check.report = report_as_dict(doc, report)
    if not report.ok:
        for finding in report.errors:
            where = f"[{finding.subject}] " if finding.subject else ""
            check.problems.append(Problem("assembly_invalid", f"{finding.code}: {where}{finding.message}", base or "/"))
        return check
    if parsed is not None and parsed.projection != current:
        return check  # a stored older projection: valid as an assembly; its bytes are not this keystone's to judge
    check.compared = True
    expected = build_assembly_environment(doc, report)
    (expected_shell,) = expected["assetAdministrationShells"]
    if canonical_json(expected_shell) != canonical_json(shell):
        check.problems.append(
            Problem(
                "assembly_projection",
                f"the shell is not the keystone projection of its document (expected id '{expected_shell['id']}')",
                base or "/",
            )
        )
    wanted = {sm["id"]: sm for sm in expected.get("submodels", [])}
    got = {sm["id"]: sm for sm in submodels}
    for sm_id in sorted(set(wanted) | set(got)):
        if canonical_json(wanted.get(sm_id)) != canonical_json(got.get(sm_id)):
            state = "missing" if sm_id not in got else ("unexpected" if sm_id not in wanted else "different")
            check.problems.append(
                Problem(
                    "assembly_projection",
                    f"submodel '{sm_id}' is {state}: it is not the keystone projection of the document",
                    base or "/",
                )
            )
    return check


# ---------------------------------------------------------------------------------------------
# instance assemblies
# ---------------------------------------------------------------------------------------------


def _component_nodes(bom: dict | None) -> tuple[dict | None, dict[str, dict]]:
    entry = _child(bom, "EntryNode") if bom else None
    nodes = {}
    for node in _children(entry) if entry else []:
        cid = _property(node, "ComponentId") if node.get("modelType") == "Entity" else None
        if cid is not None:
            nodes[cid] = node
    return entry, nodes


def _type_shell_of(node: dict) -> str | None:
    keys = ((_child(node, "DerivedFrom") or {}).get("value") or {}).get("keys") or []
    return keys[-1].get("value") if keys and keys[-1].get("type") == "AssetAdministrationShell" else None


def check_instance_assembly(
    cur: psycopg.Cursor, shell: dict, submodels: list[dict], type_bundle: Bundle
) -> tuple[list[Problem], list[Edge]]:
    """(problems, mates_with edges) for an instance whose derivedFrom is a type assembly."""
    problems: list[Problem] = []
    type_shell, type_sms = type_bundle
    by_short = {sm.get("idShort"): sm for sm in submodels}
    bom = by_short.get(BOM)
    if bom is None:
        return [Problem("instance_assembly", f"an instance of '{type_shell['id']}' carries a {BOM} submodel", "/")], []
    entry, nodes = _component_nodes(bom)
    asset = (shell.get("assetInformation") or {}).get("globalAssetId")
    if entry is None or entry.get("globalAssetId") != asset:
        problems.append(Problem("instance_assembly", f"{BOM}/EntryNode names the instance asset '{asset}'", "/"))
    _type_entry, type_nodes = _component_nodes(type_sms.get(BOM))
    for missing in sorted(set(type_nodes) - set(nodes)):
        problems.append(Problem("instance_assembly", f"component '{missing}' of the type assembly is missing", "/"))
    for extra in sorted(set(nodes) - set(type_nodes)):
        problems.append(Problem("instance_assembly", f"component '{extra}' is not in the type assembly", "/"))
    linked = set()
    for rel in _children(entry) if entry else []:
        second = _resolve_path(bom, rel.get("second")) if rel.get("modelType") == "RelationshipElement" else None
        if second:
            linked.add(_property(second[-1], "ComponentId"))
    for cid in sorted(set(nodes) & set(type_nodes)):
        node, type_node = nodes[cid], type_nodes[cid]
        if cid not in linked:
            problems.append(Problem("instance_assembly", f"component '{cid}' has no HasPart from the EntryNode", "/"))
        type_revision = _type_shell_of(type_node)
        if type_revision is not None:
            problems += _instance_component_problems(cur, cid, node, type_revision)
        elif _node_asset(node) != _node_asset(type_node):
            problems.append(
                Problem(
                    "instance_assembly",
                    f"component '{cid}' is a {_property(type_node, 'SourceType')} part: it names the type's "
                    f"'{_node_asset(type_node)}', not '{_node_asset(node)}'",
                    "/",
                )
            )
    if problems:
        return problems, []
    return [], _instance_mates(type_sms, bom, nodes, type_shell["id"])


def _instance_component_problems(cur: psycopg.Cursor, cid: str, node: dict, type_revision: str) -> list[Problem]:
    asset = node.get("globalAssetId") or ""
    uuid = asset.rsplit("/", 1)[-1]
    isid = parse_instance_shell_id(f"https://id.madfam.io/aas/instance/{uuid}")
    if isid is None or isid.asset_id != asset:
        return [Problem("instance_assembly", f"component '{cid}' names '{asset}', not an instance asset", "/")]
    cur.execute(
        "SELECT derived_from FROM shells WHERE id = %s AND kind = 'instance'",
        (f"https://id.madfam.io/aas/instance/{isid.uuid}",),
    )
    row = cur.fetchone()
    if row is None:
        return [Problem("instance_assembly", f"component '{cid}': instance '{asset}' is not published (here)", "/")]
    if not _same_revision(row["derived_from"], type_revision):
        return [
            Problem(
                "instance_assembly",
                f"component '{cid}': instance '{asset}' is derivedFrom '{row['derived_from']}', "
                f"not a projection of the type component's revision '{type_revision}'",
                "/",
            )
        ]
    return []


def _same_revision(a: str | None, b: str | None) -> bool:
    """Two type shell ids name the same design revision (any projection version)."""
    pa, pb = parse_type_shell_id(a or ""), parse_type_shell_id(b or "")
    return pa is not None and pb is not None and pa.revision == pb.revision


def _instance_mates(type_sms: dict[str, dict], bom: dict, nodes: dict[str, dict], type_shell_id: str) -> list[Edge]:
    out = []
    for element in _children(type_sms.get(MATES) or {}):
        notes = {a.get("idShort"): a.get("value") for a in element.get("annotations") or [] if isinstance(a, dict)}
        a, b = nodes.get(notes.get("ComponentA")), nodes.get(notes.get("ComponentB"))
        if a is None or b is None or not _node_asset(a) or not _node_asset(b):
            continue
        props = {k[0].lower() + k[1:]: v for k, v in notes.items() if k}
        props.update({"mateElement": element.get("idShort"), "typeAssembly": type_shell_id})
        out.append(Edge(_node_asset(a), _node_asset(b), "mates_with", bom["id"], props))
    return out
