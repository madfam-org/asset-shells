# asset-shells

MADFAM's asset-shell service: it stores and serves Asset Administration Shell (AAS) documents, metamodel
v3.1, for the designs in the MADFAM commons (type shells) and for the parts made from them (instance
shells and product passports).

> **Boundary note (public-safe).** This is a public repository. Operational detail — cluster topology,
> secret paths, client identifiers, operator runbooks — lives in MADFAM's private operations repository
> and is only pointed to from here. Never copy that detail into this repository.

Free software under the [GNU AGPL-3.0-only](./LICENSE). Landing: <https://asset-shells.madfam.io>.
API: <https://asset-shells-api.madfam.io>. Agent rules: [AGENTS.md](./AGENTS.md). Security:
[SECURITY.md](./SECURITY.md). Owner-only setup steps: [docs/operator-setup.md](./docs/operator-setup.md).

## Status

Version 1, in development, **not deployed**. What exists: the service, its migration, tests (including
tenant-isolation proofs and contract tests against the official IDTA OpenAPI), the landing page and
the deployment manifests (with placeholder image digests). There is no build-and-deploy workflow yet;
deploying is an owner decision (docs/operator-setup.md).

Not in v1: the outbox relay (webhooks/SSE — changes are recorded in the `outbox` table, nothing reads it
yet), the Concept Description repository read API, `/description`, `/serialization`, attachments and
thumbnails, `$path`, and the `$metadata`/`$value`/`$reference` variants not listed below.

## What it stores

| Kind | Who publishes | Who can read |
|---|---|---|
| **Type shells** — one per design revision (solid cartridge, garment) or material card; immutable | the platforms (yantra4d, fashion-cabinet) with `asset-shells:publish-types` | everyone, anonymously |
| **Instance shells** — one per manufactured part (`derivedFrom` its type shell) | pravara-mes with `asset-shells:publish-instances` and an organisation-bound token | the same organisation only (`asset-shells:read`) |
| **Passport events** — appended to a list in an instance submodel; append-only | as above | as above |
| **Assembly shells** — one per assembly digest (ASM-1 §5), re-validated by the keystone before they are stored | as type shells (solid commons) | everyone, anonymously |
| **Twin-graph edges** (`asset_edges`) — `has_part`, `mates_with`, `derived_from` between assets, written with each publish; append-only | derived from what is published, never sent separately | type edges everyone; instance edges the same organisation only |

## Read API — `/api/v3.1`

A subset of the read operations of IDTA-01002 v3.1.3 (AAS Repository SSP-002, Submodel Repository
SSP-002, Discovery). Route, query parameters and response bodies are checked against the official
OpenAPI documents (`tests/test_contract.py`). This is **not** a claim of conformance to any complete
service profile.

| Operation (official operationId) | Route |
|---|---|
| GetAllAssetAdministrationShells | `GET /shells?assetIds=&idShort=&limit=&cursor=` |
| GetAllAssetAdministrationShells-Reference | `GET /shells/$reference` |
| GetAssetAdministrationShellById | `GET /shells/{aasIdentifier}` |
| GetAssetAdministrationShellById-Reference_AasRepository | `GET /shells/{aasIdentifier}/$reference` |
| GetAssetInformation_AasRepository | `GET /shells/{aasIdentifier}/asset-information` |
| GetAllSubmodelReferences_AasRepository | `GET /shells/{aasIdentifier}/submodel-refs` |
| GetSubmodelById_AasRepository | `GET /shells/{aasIdentifier}/submodels/{submodelIdentifier}` |
| GetSubmodelById-ValueOnly_AasRepository | `GET …/submodels/{submodelIdentifier}/$value` |
| GetAllSubmodelElements_AasRepository | `GET …/submodels/{submodelIdentifier}/submodel-elements` |
| GetSubmodelElementByPath_AasRepository | `GET …/submodel-elements/{idShortPath}` |
| GetSubmodelElementByPath-ValueOnly_AasRepository | `GET …/submodel-elements/{idShortPath}/$value` |
| GetAllSubmodels | `GET /submodels?semanticId=&idShort=&limit=&cursor=&level=&extent=` |
| GetAllSubmodels-Reference | `GET /submodels/$reference` |
| GetSubmodelById | `GET /submodels/{submodelIdentifier}` |
| GetSubmodelById-Metadata | `GET /submodels/{submodelIdentifier}/$metadata` |
| GetSubmodelById-ValueOnly | `GET /submodels/{submodelIdentifier}/$value` |
| GetSubmodelById-Reference | `GET /submodels/{submodelIdentifier}/$reference` |
| GetAllSubmodelElements | `GET /submodels/{submodelIdentifier}/submodel-elements` |
| GetSubmodelElementByPath_SubmodelRepo | `GET /submodels/{submodelIdentifier}/submodel-elements/{idShortPath}` |
| GetSubmodelElementByPath-ValueOnly_SubmodelRepo | `GET …/submodel-elements/{idShortPath}/$value` |
| GetAllAssetAdministrationShellIdsByAssetLink (Discovery SSP-001, read-only) | `GET /lookup/shells?assetIds=` |
| SearchAllAssetAdministrationShellIdsByAssetLink | `POST /lookup/shellsByAssetLink` |
| GetAllAssetLinksById | `GET /lookup/shells/{aasIdentifier}` |

Conventions (IDTA-01002 "HTTP/REST API"):

- Identifiers in paths are UTF-8, base64url-encoded **without padding**; idShortPaths are URL-encoded
  (`Events%5B0%5D`).
- `assetIds` is base64url(JSON `{"name", "value"}`) — repeated, comma-separated, or one encoded JSON
  array; all pairs must match. The name `globalAssetId` (any case) matches the shell's global asset id.
- `semanticId` is base64url(JSON Reference) and matches `semanticId` or `supplementalSemanticIds`. A
  base64url plain IRI is also accepted and matches the last key's value.
- Lists return `{"paging_metadata": {"cursor"?}, "result": [...]}`; no `cursor` means the last page.
  Default page size 100, maximum 1000.
- Errors are Part 2 `Result` objects: `{"messages": [{"code", "messageType", "text", "timestamp",
  "correlationId"}]}`; `correlationId` equals the `X-Request-Id` response header.

Interpretation choices where the specification leaves room: `level=core` keeps the requested object
and its direct children, and a container child keeps its own attributes but not its children (in
Value-Only it becomes `{}` or `[]`); a MultiLanguageProperty Value-Only is `[{"en": "…"}]`; numeric XSD
types become JSON numbers, and values JSON cannot carry (INF, NaN) stay strings; Blob values are omitted
unless `extent=withBlobValue`.

## Publish API — `/madfam/v1`

| Route | Scope | Body | Result |
|---|---|---|---|
| `PUT /type-environments/{commons}/{sha}` | `asset-shells:publish-types` | `{"environments": [<AAS Environment>, …]}` | 201 created / 200 identical replay / 409 conflict / 422 invalid |
| `POST /instances` | `asset-shells:publish-instances` + `tenant_id` | one AAS Environment with exactly one instance shell | 201 + `Location` / 200 replay / 409 / 422 |
| `POST /instances/{uuid}/passport-events` | `asset-shells:publish-instances` + `tenant_id` | `{"eventId": <uuid>, "submodelIdShort": "<submodel>", "event": <SubmodelElementCollection>}` | 201 `{eventId, position, idShortPath, …}` / 200 replay / 409 reused id |
| `GET /instances/{uuid}/passport-events` | `asset-shells:read` + `tenant_id` | — | paged events with sequence data |

`{commons}` is `solid-hyperobjects` or `soft-hyperobjects`; `{sha}` is the 40-hex commit of the published
commons pin. A release `(commons, sha)` is immutable; shells and submodels are immutable per id (the id
carries the design-revision digest **and the projection version**, `…/{hex16}/p{N}`); concept descriptions may
change with the lexicon. So a new keystone projection of a stored revision is a **new shell** beside the old one
(201), while different bytes under the same id stay a 409. A new projection reaches the store through a new
release: the keystone bump and the commons repin give a new `{sha}`, and the same `(commons, sha)` with other
content is still a 409. A passport event
is appended to the top-level `Events` SubmodelElementList (of SubmodelElementCollection, no idShort on
items) of the named instance submodel, then the whole submodel is re-validated.

An **assembly** environment (shell `aas/assembly/{slug}/{digest16}/p{N}`, published in a `solid-hyperobjects`
release) passes a third gate, the pinned keystone (`hyperobjects-spec`, ASM-1 §6):

1. its document is read back from the `AssemblyDocument` Blob;
2. every cartridge component is resolved from the **stored** type shell its BillOfMaterials node names
   (`DerivedFrom`: `ParametricModel`, `GeometryProvision`, `MatingInterfaces`, `RequirementProfile`), or
   from a shell published in the same request; standard parts from the catalog bundled with the pinned
   keystone; external designs from their inline facts;
3. `validate_assembly` runs: an unresolved component or any failing check (mating rule, closure of every
   mate, reachability) is a **422 whose body adds `assemblyReports`** — the keystone's full report
   (components, placements, every mate with its residuals, errors);
4. the published shell and submodels must be exactly the keystone's projection of that document and
   report (same digest, BoM, mates, placements), else a 422 naming the submodel that differs.

So publishers must build assembly environments with the **same keystone pin** as this service (the solid
commons' `SPEC_PIN`); a different projection is refused rather than stored. A new assembly shell whose projection
version is not the keystone's (`hyperobjects_aas.PROJECTION_VERSION`) is a 422 `assembly_projection_version`
before anything else runs. Its BoM names the type shells of the same version, so publish those first.

An **instance assembly** (Phase 5: `POST /instances` with `derivedFrom` a type assembly shell) must match
its type: its `BillOfMaterials` EntryNode (the instance asset) holds one entity per type component
(`ComponentId`), each with a HSEBoM `HasPart` from the EntryNode; a cartridge component names an instance
asset of the same organisation whose shell is `derivedFrom` the type component's revision (matched by
`{kind, slug, digest16}`, so an instance derived from any projection version of that revision matches); a
standard or external component names the type's asset (or URL). Otherwise 422.

Every write passes two gates before anything is stored: the official AAS v3.1.2 JSON Schema (vendored in
`asset_shells/schemas`, with attributes the metamodel does not define rejected) and the BaSyx Python SDK
2.2.0 strict decoder; then the identifier rules of SEM-1 §1. Rejections are `Result` objects whose
messages carry a JSON-pointer `path` into the request body. The document is stored as published.

## Twin graph — `/madfam/v1` (ASM-1 §6)

| Route | Who | Result |
|---|---|---|
| `GET /graph?root={assetId}&direction=down\|up&depth=1..8&kinds=has_part,mates_with,derived_from,same_as` | anyone (type graph); `asset-shells:read` + `tenant_id` adds that tenant's instance graph | `{root, direction, depth, kinds, nodes: [{assetId, depth, shells}], edges: [{from, to, kind, depth, instance, viaShellId, viaSubmodelId, props}], truncated}` / 400 / 401 / 403 / 404 |
| `GET /assemblies/{assetId}/validation[?revision={digest16}][&projection={N}]` | anyone for a type assembly; `asset-shells:read` + `tenant_id` for an instance assembly | the keystone's verdict re-run now over the stored shells, by default on the latest revision at its current projection: `{assetId, shellId, revisions, ok, keystone, projection: {shell, keystone, compared}, problems, report}`; for an instance also `{instance: {ok, problems}, type: {…}}` / 404 |
| `GET /assets/{assetId}/projections` | anyone (type assets only) | every stored revision of the asset, oldest first: `{assetId, keystoneProjection, revisions: [{revision, projections: [{version, shellId}], current}]}` / 400 (not a type asset) / 404 |

`assetId` is UTF8-BASE64-URL-encoded as in Part 2; `root` also accepts a plain `https://…` IRI. Defaults:
`direction=down`, `depth=1`, every kind. The walk is one recursive CTE over `asset_edges` (one row per asset
and depth, so cycles of mates cost at most `depth` rows per asset), at most 5000 edges (`truncated` says
when it stopped). Row-level security decides what it sees, exactly as for shells: another organisation's
instance is a 404.

Edges, written in the same transaction as the publish that carries them (a replay writes none):

| Kind | From → to | Read from |
|---|---|---|
| `has_part` | the nearest asset on the `first` path → the `second` entity's `globalAssetId` (an external design: its `Url`) | every HSEBoM `HasPart` of a `BillOfMaterials` submodel |
| `mates_with` | component a's asset → component b's asset; `props` = the mate's annotations (interfaces, rotation, residuals, `validated`) | an assembly's `Mates` submodel; an instance assembly gets its type's mates mapped onto its components |
| `derived_from` | instance asset → its type asset (`props.typeShell` = the exact revision) | an instance shell's `derivedFrom` |
| `same_as` | reserved | — |

A cartridge component's `has_part` edge carries `props.typeShell`, the revision it was resolved at (a versioned
shell id).

**Projection versions** (hyperobjects-spec 0.6.0, owner decision 2026-10-04). Every type shell and submodel id
carries the keystone projection version, `…/{hex16}/p{N}`, and the shell repeats it in its `ProjectionVersion`
extension, which must agree with the id (else 422 `projection_version`).

**Version 2** (hyperobjects-spec 0.7.0, ASM-1 §9; pinned here at 0.8.0) adds the `Kinematics` submodel to every assembly shell (MADFAM
`smt/assembly-kinematics/1/0`: joints, machine-axis bindings, belt paths, the pose sweep). It is stored and served
like any other submodel; no allowlist names submodels, and a template id is accepted by its pattern. The service
re-validates a published assembly, sweep included, with its pinned keystone, and stores p1 and p2 shells of one
revision side by side.

**Version 3** (hyperobjects-spec 0.10.0) is one content hash, one projection. A content-addressed id hashes canonical
JSON (GOC-1 §3.1, where `220.0` and `220` are one number): a material card's `content16` and an assembly's
`digest16`. The keystone now projects the input in that same canonical form, so a respelt input republishes as
*unchanged* instead of a 409. A whole number's untyped value is `xs:integer`, and a fractional one stays `xs:double`.
Assembly bytes did not move. A card's whole-float values did (`xs:double` → `xs:integer`), so the version is 3. An
assembly the service re-projects from its stored `AssemblyDocument` blob, which is canonical JSON, now matches what
its publisher projected from its own document. The service needs no code change: p1, p2 and p3 shells of one
revision are stored side by side.

- **The current projection** of a revision is its stored shell with the **highest** `N`. Versions only grow, so
  that is the newest. `GET /assets/{assetId}/projections` lists every version and marks the current one.
  `/validation` uses the current one unless `projection=` asks for another.
- **Reading an older version.** `/validation` re-runs the document, resolution and mating checks on an older
  version as usual. Its bytes are not compared with this keystone's projection (`projection.compared: false`),
  because the keystone no longer writes that version.
- **The graph.** Edges stay asset-to-asset. Each projection of an assembly writes its own rows, so a walk shows the
  same asset-to-asset edges once per stored version, each with `viaShellId`/`viaSubmodelId` naming its versioned row,
  just as two revisions of an assembly each contribute their edges. Filter on `viaShellId` (the current one from
  `/projections`) for one version.

## Identifiers (SEM-1 §1)

| Object | Identifier |
|---|---|
| Type asset | `https://id.madfam.io/asset/{solid\|soft}/{slug}` |
| Type shell | `https://id.madfam.io/aas/{solid\|soft}/{slug}/{tree16}/p{N}` (`N` = projection version, also the `ProjectionVersion` extension) |
| Type submodel | `https://id.madfam.io/sm/{solid\|soft}/{slug}/{tree16}/p{N}/{SubmodelIdShort}` |
| Material card asset / shell | `https://id.madfam.io/asset/material/{slug}` / `https://id.madfam.io/aas/material/{slug}/{content16}/p{N}` |
| Material submodel (not in SEM-1 §1; analogous, accepted) | `https://id.madfam.io/sm/material/{slug}/{content16}/p{N}/{SubmodelIdShort}` |
| Assembly asset / shell (ASM-1 §5) | `https://id.madfam.io/asset/assembly/{slug}` / `https://id.madfam.io/aas/assembly/{slug}/{digest16}/p{N}` (specificAssetIds `commons`, `slug`, `assembly_digest`) |
| Assembly submodel | `https://id.madfam.io/sm/assembly/{slug}/{digest16}/p{N}/{SubmodelIdShort}` |
| Standard (COTS) part asset | `https://id.madfam.io/asset/standard/{key}` (no shell is stored for it; it is a graph node) |
| Instance asset / shell | `https://id.madfam.io/asset/instance/{uuid}` / `https://id.madfam.io/aas/instance/{uuid}` |
| Instance submodel | `https://id.madfam.io/sm/instance/{uuid}/{SubmodelIdShort}` |
| Concept description | `https://id.madfam.io/concept/{term}` or, for a submodel template, `https://id.madfam.io/smt/{template}/{major}/{minor}` |

## Identity and tenancy

- Janua RS256 tokens only: `kid` required, issuer `https://auth.madfam.io` (configurable,
  `JANUA_ISSUER`), audience `asset-shells-api`; `exp`, `iat`, `sub` required. Scopes from the `scope` claim.
- Anonymous requests may read type shells. A presented token that does not verify is a 401 even on public
  routes. Instance reads need `asset-shells:read` and a `tenant_id`; anonymous requests for an instance id
  get 401, tokens without the scope or tenant 403, and another tenant's instance 404.
- The database enforces the same: the runtime role is not the owner, every tenant-bearing table has
  `FORCE ROW LEVEL SECURITY`, each transaction sets one setting (`app.tenant_id`, transaction-local),
  composite foreign keys pin child rows (including graph edges) to their shell's tenant, and triggers make
  shells and type submodels immutable and passport events and graph edges append-only. The service refuses to start if its database role
  owns tables, is a superuser or bypasses RLS.

## Local development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# A throwaway PostgreSQL (TCP only), the two roles and the test database:
PG=/usr/local/opt/postgresql@14/bin; D=$(mktemp -d)
$PG/initdb -D $D/data -U postgres --auth=trust
$PG/pg_ctl -D $D/data -o "-p 55471 -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" -l $D/log -w start
$PG/psql -h 127.0.0.1 -p 55471 -U postgres -f scripts/test-db-setup.sql
export ASSET_SHELLS_TEST_ADMIN_URL=postgresql://asset_shells_owner@127.0.0.1:55471/asset_shells_test
export ASSET_SHELLS_TEST_APP_URL=postgresql://asset_shells_app@127.0.0.1:55471/asset_shells_test

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest -q --cov=asset_shells --cov-fail-under=80

# Run the service against the same database with a local signing key:
DATABASE_URL=$ASSET_SHELLS_TEST_ADMIN_URL .venv/bin/asset-shells migrate
TOKEN=$(.venv/bin/asset-shells dev-token --dir .dev-keys --scope asset-shells:publish-types)
ASSET_SHELLS_ENV=local JWKS_PATH=.dev-keys/jwks.json APP_DATABASE_URL=$ASSET_SHELLS_TEST_APP_URL \
  .venv/bin/uvicorn asset_shells.app:app --port 8000
curl -s localhost:8000/api/v3.1/shells | jq .paging_metadata

$PG/pg_ctl -D $D/data -m fast stop && rm -rf $D
```

The database tests ERROR (they are never skipped) when the two URLs are missing.

## Configuration

| Variable | Default | Source in production |
|---|---|---|
| `APP_DATABASE_URL` | — | Secret: the non-owner runtime role |
| `DATABASE_URL` | — | Secret: the schema owner, migrate init container only |
| `APP_DB_ROLE` | `asset_shells_app` | env (migration grants) |
| `JANUA_ISSUER` / `JANUA_JWKS_URL` / `JANUA_AUDIENCE` | `https://auth.madfam.io` / `<issuer>/.well-known/jwks.json` / `asset-shells-api` | env |
| `DB_POOL_MIN` / `DB_POOL_MAX` | 1 / 4 (refuses > 10) | env |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_STARTUP_RETRY_SECONDS` | 15000 / 30 | env |
| `MAX_PUBLISH_BYTES` | 33554432 | env |
| `ASSET_SHELLS_ENV`, `JWKS_PATH` | `production`, — | `JWKS_PATH` only in `local`/`test` |

## Dependencies of note

- **`hyperobjects-spec`** (the keystone, Apache-2.0), pinned by full commit SHA in `pyproject.toml`: the
  assembly validator, the AAS projection and the stored-shell resolver run here exactly as in the commons
  CI. Repin it together with the solid commons' `SPEC_PIN`. The image's build stage installs `git` to fetch
  it; the runtime image has no git and no pip.

## Third-party material

- `asset_shells/schemas/aas-v3.1.2.json` — IDTA, CC-BY-4.0 (see the NOTICE next to it).
- `tests/contract/specs/` — IDTA-01002 v3.1.3 OpenAPI documents, CC-BY-4.0 (see the README there).
- `tests/fixtures/assembly-commons/` — assemblies A and B and their nine cartridges, byte-identical copies
  from the solid commons, CERN-OHL-W-2.0, test inputs only (`tests/fixtures/NOTICE-assembly-commons.md`).
