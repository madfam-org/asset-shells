"""Writes: type releases, instance shells, passport events. Each one is a single transaction that
also writes its outbox rows and its twin-graph edges (``asset_edges``, ASM-1 §6), so a change, its
notification and its edges commit or roll back together. Assembly shells are re-validated by the pinned
keystone before anything is written (``assemblies.py``).

Idempotency and immutability:
* A type release ``(commons, sha)`` is immutable: re-sending the same content is a no-op (200);
  different content for the same pair is a 409. Shells and submodels are immutable per id (the id
  carries the design-revision digest); concept descriptions follow the lexicon and may be updated.
* An instance publish is replayable: the same environment again is a 200; anything else for an
  existing id is a 409. Ids owned by another tenant are indistinguishable from "taken" (409).
* A passport event is identified by its ``eventId``; replaying it is a 200, reusing it is a 409.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

import psycopg

from . import assemblies, db, graph
from .errors import ApiError, Problem, bad_request, conflict, not_found, unprocessable
from .ids import COMMONS_KINDS, ID_SHORT, InstanceShellId, is_commit_sha
from .repository import GLOBAL_ASSET_ID, canonical_reference
from .scheme import instance_environment_problems, type_environment_problems
from .validation import content_sha256, validate_element, validate_environment

MAX_PROBLEMS = 200
EVENTS_LIST = "Events"
_RE_ID_SHORT = re.compile(rf"^{ID_SHORT}$")


def _raise_if(problems: list[Problem]) -> None:
    if problems:
        if len(problems) > MAX_PROBLEMS:
            problems = [*problems[:MAX_PROBLEMS], Problem("truncated", f"{len(problems) - MAX_PROBLEMS} more problems")]
        raise unprocessable(problems)


def _semantic_index(submodel: dict) -> tuple[list[str], list[str]]:
    refs = [r for r in [submodel.get("semanticId"), *submodel.get("supplementalSemanticIds", [])] if r]
    return [canonical_reference(r) for r in refs], [r["keys"][-1]["value"] for r in refs if r.get("keys")]


def _outbox(cur: psycopg.Cursor, tenant: str | None, topic: str, subject: str, payload: dict) -> None:
    cur.execute(
        "INSERT INTO outbox (tenant_id, topic, subject_id, payload) VALUES (%s, %s, %s, %s)",
        (tenant, topic, subject, json.dumps(payload)),
    )


def _insert_shell(cur, shell: dict, kind: str, tenant: str | None, derived: str | None, publish_sha: str | None):
    info = shell.get("assetInformation", {})
    cur.execute(
        """
        INSERT INTO shells (id, kind, tenant_id, id_short, global_asset_id, derived_from, doc, content_sha256,
                            publish_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            shell["id"],
            kind,
            tenant,
            shell.get("idShort"),
            info.get("globalAssetId"),
            derived,
            json.dumps(shell),
            content_sha256(shell),
            publish_sha,
        ),
    )
    links = []
    if info.get("globalAssetId"):
        links.append({"name": GLOBAL_ASSET_ID, "value": info["globalAssetId"]})
    links += info.get("specificAssetIds", [])
    for position, link in enumerate(links):
        cur.execute(
            "INSERT INTO asset_ids (shell_id, tenant_id, position, name, value, doc) VALUES (%s, %s, %s, %s, %s, %s)",
            (shell["id"], tenant, position, link["name"], link["value"], json.dumps(link)),
        )


def _insert_submodel(cur, submodel: dict, shell_id: str, kind: str, tenant: str | None) -> None:
    refs, values = _semantic_index(submodel)
    cur.execute(
        """
        INSERT INTO submodels (id, shell_id, kind, tenant_id, id_short, semantic_refs, semantic_values, doc,
                               content_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            submodel["id"],
            shell_id,
            kind,
            tenant,
            submodel.get("idShort"),
            refs,
            values,
            json.dumps(submodel),
            content_sha256(submodel),
        ),
    )


# ---------------------------------------------------------------------------------------------
# type releases
# ---------------------------------------------------------------------------------------------


@dataclass
class ReleaseSummary:
    commons: str
    sha: str
    content_sha256: str
    created: bool
    shells: dict[str, list[str]] = field(default_factory=lambda: {"created": [], "unchanged": []})
    submodels: dict[str, list[str]] = field(default_factory=lambda: {"created": [], "unchanged": []})
    concept_descriptions: dict[str, list[str]] = field(
        default_factory=lambda: {"created": [], "updated": [], "unchanged": []}
    )
    edges: int = 0

    def body(self) -> dict:
        return {
            "commons": self.commons,
            "sha": self.sha,
            "contentSha256": self.content_sha256,
            "created": self.created,
            "shells": self.shells,
            "submodels": self.submodels,
            "conceptDescriptions": self.concept_descriptions,
            "edges": self.edges,
        }


def _collect(items: list[tuple[str, dict]], kind: str, problems: list[Problem]) -> dict[str, dict]:
    """De-duplicate identifiables across environments; the same id with different content is an error."""
    out: dict[str, dict] = {}
    for path, doc in items:
        existing = out.get(doc["id"])
        if existing is not None and content_sha256(existing) != content_sha256(doc):
            problems.append(Problem("duplicate_id", f"{kind} '{doc['id']}' appears twice with different content", path))
        out.setdefault(doc["id"], doc)
    return out


def publish_type_release(commons: str, sha: str, body: object) -> ReleaseSummary:
    if commons not in COMMONS_KINDS:
        raise bad_request("unknown_commons", f"commons must be one of {', '.join(sorted(COMMONS_KINDS))}")
    if not is_commit_sha(sha):
        raise bad_request("bad_sha", "sha is the 40-hex git commit of the published commons pin")
    if not isinstance(body, dict) or not isinstance(body.get("environments"), list) or not body["environments"]:
        raise unprocessable([Problem("body", 'the body is {"environments": [<AAS Environment>, ...]}', "/")])

    problems: list[Problem] = []
    shells, submodels, cds = [], [], []
    for i, env in enumerate(body["environments"]):
        base = f"/environments/{i}"
        validated, env_problems = validate_environment(env, base)
        problems += env_problems
        if validated is None:
            continue
        problems += type_environment_problems(env, commons, base)
        shells += [(f"{base}/assetAdministrationShells/{j}", s) for j, s in enumerate(validated.shells)]
        submodels += [(f"{base}/submodels/{j}", s) for j, s in enumerate(validated.submodels)]
        cds += [(f"{base}/conceptDescriptions/{j}", c) for j, c in enumerate(validated.concept_descriptions)]
    _raise_if(problems)
    shell_map = _collect(shells, "shell", problems)
    submodel_map = _collect(submodels, "submodel", problems)
    cd_map = _collect(cds, "concept description", problems)
    _raise_if(problems)

    owner = {}
    for shell in shell_map.values():
        for ref in shell.get("submodels", []):
            owner[ref["keys"][0]["value"]] = shell["id"]
    bundles = {
        ident: (shell, [submodel_map[ref["keys"][0]["value"]] for ref in shell.get("submodels", [])])
        for ident, shell in shell_map.items()
    }
    manifest = {
        "shells": {k: content_sha256(v) for k, v in sorted(shell_map.items())},
        "submodels": {k: content_sha256(v) for k, v in sorted(submodel_map.items())},
        "conceptDescriptions": {k: content_sha256(v) for k, v in sorted(cd_map.items())},
    }
    release_sha = content_sha256(manifest)
    summary = ReleaseSummary(commons, sha, release_sha, created=False)
    try:
        return _store_release(summary, shell_map, submodel_map, cd_map, owner, manifest, bundles)
    except psycopg.errors.UniqueViolation:
        raise conflict("concurrent_publish", "a concurrent publish wrote the same identifiers; retry") from None


def _check_assemblies(cur, created: list[str], bundles: dict[str, tuple[dict, list[dict]]]) -> None:
    """ASM-1 §6: every assembly shell this release creates passes the keystone against the stored type shells
    (and the ones this release carries), and is exactly the keystone's projection. Otherwise a 422 with the
    reports, and the transaction (nothing written yet) rolls back."""
    pending = {ident: (shell, {sm["idShort"]: sm for sm in sms}) for ident, (shell, sms) in bundles.items()}
    fetch = assemblies.type_fetcher(cur, pending)
    checks = [
        assemblies.check_type_assembly(bundles[ident][0], bundles[ident][1], fetch, f"/shells/{ident}")
        for ident in created
        if assemblies.is_assembly_shell(ident)
    ]
    failed = [c for c in checks if not c.ok]
    if failed:
        problems = [p for c in failed for p in c.problems][:MAX_PROBLEMS]
        raise ApiError(422, problems, extra={"assemblyReports": [c.body() for c in failed]})


def _store_release(
    summary: ReleaseSummary,
    shell_map: dict,
    submodel_map: dict,
    cd_map: dict,
    owner: dict,
    manifest: dict,
    bundles: dict[str, tuple[dict, list[dict]]],
) -> ReleaseSummary:
    commons, sha, release_sha = summary.commons, summary.sha, summary.content_sha256
    with db.transaction(None) as cur:
        cur.execute("SELECT content_sha256 FROM type_releases WHERE commons = %s AND sha = %s", (commons, sha))
        row = cur.fetchone()
        if row is not None:
            if row["content_sha256"] != release_sha:
                raise conflict("release_conflict", f"release {commons}@{sha} exists with different content")
            summary.shells["unchanged"] = sorted(shell_map)
            summary.submodels["unchanged"] = sorted(submodel_map)
            summary.concept_descriptions["unchanged"] = sorted(cd_map)
            return summary

        conflicts: list[Problem] = []
        for table, mapping, bucket in (
            ("shells", shell_map, summary.shells),
            ("submodels", submodel_map, summary.submodels),
        ):
            cur.execute(
                f"SELECT id, content_sha256, kind FROM {table} WHERE id = ANY(%s)",  # noqa: S608
                (list(mapping),),
            )
            existing = {r["id"]: r for r in cur.fetchall()}
            for ident in sorted(mapping):
                found = existing.get(ident)
                if found is None:
                    continue
                if found["kind"] != "type" or found["content_sha256"] != manifest[table][ident]:
                    conflicts.append(
                        Problem(
                            "immutable",
                            f"'{ident}' already exists with different content; "
                            "a changed design gets a new tree digest and so a new id",
                        )
                    )
                else:
                    bucket["unchanged"].append(ident)
        if conflicts:
            raise ApiError(409, conflicts[:MAX_PROBLEMS])
        _check_assemblies(cur, [i for i in sorted(shell_map) if i not in summary.shells["unchanged"]], bundles)

        for ident, shell in sorted(shell_map.items()):
            if ident in summary.shells["unchanged"]:
                continue
            _insert_shell(cur, shell, "type", None, None, None)
            summary.shells["created"].append(ident)
            _outbox(
                cur,
                None,
                "type_shell.published",
                ident,
                {"id": ident, "commons": commons, "sha": sha, "contentSha256": manifest["shells"][ident]},
            )
        for ident, submodel in sorted(submodel_map.items()):
            if ident in summary.submodels["unchanged"]:
                continue
            _insert_submodel(cur, submodel, owner[ident], "type", None)
            summary.submodels["created"].append(ident)
        # The twin graph (ASM-1 §6), in this same transaction: the edges of every shell this release creates.
        for ident in summary.shells["created"]:
            shell, submodels = bundles[ident]
            summary.edges += graph.insert_edges(cur, ident, None, graph.environment_edges(shell, submodels))

        cur.execute("SELECT id, content_sha256 FROM concept_descriptions WHERE id = ANY(%s)", (list(cd_map),))
        existing_cds = {r["id"]: r["content_sha256"] for r in cur.fetchall()}
        for ident, cd in sorted(cd_map.items()):
            digest = manifest["conceptDescriptions"][ident]
            if existing_cds.get(ident) == digest:
                summary.concept_descriptions["unchanged"].append(ident)
                continue
            if ident in existing_cds:
                cur.execute(
                    "UPDATE concept_descriptions SET doc = %s, content_sha256 = %s, updated_at = now() WHERE id = %s",
                    (json.dumps(cd), digest, ident),
                )
                summary.concept_descriptions["updated"].append(ident)
            else:
                cur.execute(
                    "INSERT INTO concept_descriptions (id, doc, content_sha256) VALUES (%s, %s, %s)",
                    (ident, json.dumps(cd), digest),
                )
                summary.concept_descriptions["created"].append(ident)
            _outbox(cur, None, "concept_description.changed", ident, {"id": ident, "contentSha256": digest})

        cur.execute(
            "INSERT INTO type_releases (commons, sha, content_sha256, shell_ids) VALUES (%s, %s, %s, %s)",
            (commons, sha, release_sha, sorted(shell_map)),
        )
        _outbox(
            cur,
            None,
            "type_release.published",
            f"{commons}@{sha}",
            {"commons": commons, "sha": sha, "contentSha256": release_sha, "shells": sorted(shell_map)},
        )
        summary.created = True
    return summary


# ---------------------------------------------------------------------------------------------
# instances
# ---------------------------------------------------------------------------------------------


def publish_instance(tenant: str, body: object) -> tuple[bool, dict]:
    """(created, response body)."""
    validated, problems = validate_environment(body)
    _raise_if(problems)
    problems, isid, derived = instance_environment_problems(body)
    _raise_if(problems)
    if isid is None or validated is None:  # unreachable: both are set when there are no problems
        raise unprocessable([Problem("environment", "the instance environment could not be read", "/")])
    publish_sha = content_sha256(body)
    shell = validated.shells[0]
    try:
        return _store_instance(tenant, shell, validated.submodels, derived, publish_sha)
    except psycopg.errors.UniqueViolation:
        # Another tenant holds the id (RLS hid it from the SELECT) or a concurrent publish won.
        raise conflict("identifier_unavailable", "this instance id is already published") from None


def _store_instance(
    tenant: str, shell: dict, submodels: list[dict], derived: str | None, publish_sha: str
) -> tuple[bool, dict]:
    with db.transaction(tenant) as cur:
        cur.execute("SELECT kind, publish_sha256 FROM shells WHERE id = %s", (shell["id"],))
        row = cur.fetchone()
        if row is not None:
            if row["kind"] == "instance" and row["publish_sha256"] == publish_sha:
                return False, _instance_body(shell["id"], publish_sha)
            raise conflict("identifier_unavailable", "this instance id is already published with other content")
        derived_asset, extra_edges = None, []
        if derived is not None:
            cur.execute("SELECT kind, global_asset_id FROM shells WHERE id = %s", (derived,))
            found = cur.fetchone()
            if found is None or found["kind"] != "type":
                raise unprocessable(
                    [
                        Problem(
                            "unknown_derived_from",
                            f"type shell '{derived}' is not published",
                            "/assetAdministrationShells/0/derivedFrom",
                        )
                    ]
                )
            derived_asset = found["global_asset_id"]
            if assemblies.is_assembly_shell(derived):
                # ASM-1 §6: an instance assembly matches its type assembly, component by component.
                type_bundle = assemblies.type_fetcher(cur)(derived)
                problems, extra_edges = assemblies.check_instance_assembly(cur, shell, submodels, type_bundle)
                _raise_if(problems)
        _insert_shell(cur, shell, "instance", tenant, derived, publish_sha)
        for submodel in submodels:
            _insert_submodel(cur, submodel, shell["id"], "instance", tenant)
        edges = graph.environment_edges(shell, submodels, derived_asset) + extra_edges
        graph.insert_edges(cur, shell["id"], tenant, edges)
        _outbox(
            cur,
            tenant,
            "instance_shell.published",
            shell["id"],
            {
                "id": shell["id"],
                "derivedFrom": derived,
                "publishSha256": publish_sha,
                "submodels": [s["id"] for s in submodels],
            },
        )
    return True, _instance_body(shell["id"], publish_sha)


def _instance_body(shell_id: str, publish_sha: str) -> dict:
    return {"id": shell_id, "publishSha256": publish_sha}


# ---------------------------------------------------------------------------------------------
# passport events
# ---------------------------------------------------------------------------------------------


def _parse_event_body(body: object) -> tuple[str, str, dict]:
    problems: list[Problem] = []
    if not isinstance(body, dict):
        raise unprocessable([Problem("body", 'the body is {"eventId", "submodelIdShort", "event"}', "/")])
    unknown = sorted(set(body) - {"eventId", "submodelIdShort", "event"})
    if unknown:
        problems.append(Problem("body", f"unknown members: {', '.join(unknown)}", "/"))
    event_id = body.get("eventId")
    try:
        if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
            raise ValueError
    except ValueError:
        problems.append(
            Problem("event_id", "eventId is a lower-case canonical UUID chosen by the publisher", "/eventId")
        )
    target = body.get("submodelIdShort")
    if not isinstance(target, str) or not _RE_ID_SHORT.match(target):
        problems.append(Problem("submodel", "submodelIdShort names a submodel of the instance", "/submodelIdShort"))
    event = body.get("event")
    if not isinstance(event, dict) or event.get("modelType") != "SubmodelElementCollection":
        problems.append(Problem("event", "event is a SubmodelElementCollection", "/event"))
    elif "idShort" in event:
        problems.append(Problem("event", "list items carry no idShort (AASd-120)", "/event/idShort"))
    else:
        problems += validate_element(event, "/event")
    _raise_if(problems)
    return event_id, target, event


def append_passport_event(tenant: str, isid: InstanceShellId, body: object) -> tuple[bool, dict]:
    event_id, target, event = _parse_event_body(body)
    try:
        return _store_event(tenant, isid, event_id, target, event)
    except psycopg.errors.UniqueViolation:
        # The eventId belongs to another tenant (RLS hid it) or a concurrent append won.
        raise conflict("event_id_reused", "this eventId is already used") from None


def _store_event(tenant: str, isid: InstanceShellId, event_id: str, target: str, event: dict) -> tuple[bool, dict]:
    shell_id = f"https://id.madfam.io/aas/instance/{isid.uuid}"
    submodel_id = f"{isid.submodel_prefix}{target}"
    digest = content_sha256(event)
    with db.transaction(tenant) as cur:
        cur.execute("SELECT kind FROM shells WHERE id = %s", (shell_id,))
        row = cur.fetchone()
        if row is None or row["kind"] != "instance":
            raise not_found("The instance shell does not exist or belongs to another tenant")
        cur.execute(
            "SELECT submodel_id, position, content_sha256, received_at FROM passport_events WHERE event_id = %s",
            (event_id,),
        )
        prior = cur.fetchone()
        if prior is not None:
            if prior["submodel_id"] == submodel_id and prior["content_sha256"] == digest:
                return False, _event_body(event_id, submodel_id, prior["position"], digest, prior["received_at"])
            raise conflict("event_id_reused", "this eventId was already used for a different event")
        cur.execute("SELECT doc FROM submodels WHERE id = %s FOR UPDATE", (submodel_id,))
        sm_row = cur.fetchone()
        if sm_row is None:
            raise not_found(f"The instance has no submodel '{target}'")
        submodel = sm_row["doc"]
        events = next((e for e in submodel.get("submodelElements", []) if e.get("idShort") == EVENTS_LIST), None)
        if (
            events is None
            or events.get("modelType") != "SubmodelElementList"
            or events.get("typeValueListElement") != "SubmodelElementCollection"
        ):
            raise unprocessable(
                [
                    Problem(
                        "no_events_list",
                        f"submodel '{target}' has no top-level "
                        f"SubmodelElementList '{EVENTS_LIST}' of SubmodelElementCollection",
                        "/submodelIdShort",
                    )
                ]
            )
        position = len(events.get("value", []))
        events.setdefault("value", []).append(event)
        _validated, problems = validate_environment({"submodels": [submodel]}, "/event")
        _raise_if(problems)
        refs, values = _semantic_index(submodel)
        cur.execute(
            "UPDATE submodels SET doc = %s, content_sha256 = %s, semantic_refs = %s, semantic_values = %s, "
            "updated_at = now() WHERE id = %s",
            (json.dumps(submodel), content_sha256(submodel), refs, values, submodel_id),
        )
        cur.execute(
            """
            INSERT INTO passport_events (event_id, shell_id, submodel_id, tenant_id, position, doc, content_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (event_id, shell_id, submodel_id, tenant, position, json.dumps(event), digest),
        )
        cur.execute("SELECT received_at FROM passport_events WHERE event_id = %s", (event_id,))
        received = cur.fetchone()["received_at"]
        _outbox(
            cur,
            tenant,
            "passport_event.appended",
            shell_id,
            {
                "shell": shell_id,
                "submodel": submodel_id,
                "eventId": event_id,
                "position": position,
                "contentSha256": digest,
            },
        )
    return True, _event_body(event_id, submodel_id, position, digest, received)


def _event_body(event_id: str, submodel_id: str, position: int, digest: str, received) -> dict:
    return {
        "eventId": event_id,
        "submodelId": submodel_id,
        "idShortPath": f"{EVENTS_LIST}[{position}]",
        "position": position,
        "contentSha256": digest,
        "receivedAt": received.isoformat(),
    }
