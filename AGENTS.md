# AGENTS.md — asset-shells

> **Boundary note (public-safe).** This is a public repository. Operational detail that would reveal
> MADFAM's posture — cluster topology, secret paths, client identifiers, break-glass procedures, operator
> runbooks — lives in the private operations repository and is only *pointed to* from here. Agents
> working in this repo: never copy that detail in.

Read this before touching the repo. Every agent commit ends with
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR bodies end with the Claude Code line.

## What asset-shells is

The MADFAM ecosystem's store for Asset Administration Shell documents (Digital Twins MES programme,
Phase 3; contract SEM-1 §6), and its twin graph (Phase 4; ASM-1 §6). Owner decisions of 2026-10-02: codename `asset-shells`; permanent ids under
`https://id.madfam.io/`; type shells are public read; PUBLIC repository under AGPL-3.0-only; landing at
`asset-shells.madfam.io`, API at `asset-shells-api.madfam.io`.

## Doctrines (locked; the tests enforce them)

1. **Validate every write, store what was validated.** Official AAS v3.1.2 JSON Schema (undefined
   attributes rejected) + BaSyx SDK 2.2.0 strict decoder + SEM-1 §1 identifier rules. The document is
   stored as published, not as the SDK re-serialises it (the SDK reorders set-typed lists and emits
   schema-invalid empty arrays). Rejections are Part 2 `Result` objects with JSON-pointer paths.
2. **Tenancy at the target, fail-closed.** Janua RS256 (`kid`, `iss`, `aud=asset-shells-api`), scopes
   enforced here, `tenant_id` scopes instance data. Row-level security is FORCED; the runtime role is not
   the owner (the service refuses to start otherwise); one tenant setting per transaction, transaction-
   local; composite tenant foreign keys. A presented bad token is never treated as anonymous.
3. **Immutable types, append-only passports.** A type shell id carries its design-revision digest and the
   keystone projection version (`…/{hex16}/p{N}`, hyperobjects-spec 0.6.0); a changed design or a new
   projection is a new id, never an update. Passport corrections are new events. Triggers enforce both for every
   role, the owner included.
4. **No unproven claims (R85).** We implement a *subset* of IDTA-01002 read operations and test each one
   against the official OpenAPI. Never write "AAS-compliant", "conformant", "Industry 4.0" or claim a
   service profile in code, docs, the landing or `/description` (which is deliberately not implemented).
5. **Logs carry no database detail.** Database errors are logged as class + SQLSTATE only
   (`describe_db_error`); `DbErrorScrubFilter` strips anything else. `tests/test_ops.py` pins it.
6. **The keystone decides assemblies (ASM-1 §6).** An assembly shell is stored only if the pinned
   `hyperobjects-spec` validator passes it against the type shells stored here AND the published shell and
   submodels are exactly its projection; graph edges are read from that verified content in the same
   transaction. Never add an assembly rule here that belongs in the keystone, and never accept edges a
   publisher sends separately.
7. **Small footprint.** Pool max 4 (the shared Postgres has 100 connections fleet-wide); no pip in the
   runtime image; read-only root filesystem.

## What asset-shells does not own

Geometry, rendering and slicing (yantra4d, fashion-cabinet, the slicer worker); projecting manifests into
AAS (the `hyperobjects_aas` keystone in hyperobjects-spec); the semantic facts themselves (the commons
manifests and `hyperobjects_lexicon`); routing jobs and matching producers (pravara-mes); quoting
(Cotiza), selling (forj), billing (Dhanam), CFDI (Karafiel); identity (Janua); any LLM call (Selva —
this service makes none).

## Layout

- `asset_shells/app.py` — FastAPI app, `/health`, `/ready` (real DB round-trip + schema revision), request log.
- `asset_shells/api_read.py` — Part 2 read routes at `/api/v3.1` (official operationIds).
- `asset_shells/api_publish.py` — `/madfam/v1` publish routes; `publish.py` — transactional writes + outbox
  + graph edges.
- `asset_shells/api_graph.py` — `GET /madfam/v1/graph`, `/assemblies/{assetId}/validation` and
  `/assets/{assetId}/projections` (the current projection of a revision = its highest stored `p{N}`);
  `graph.py` — edge extraction and the recursive-CTE walk; `assemblies.py` — the keystone seam (type
  assembly re-validation, instance-assembly matching).
- `asset_shells/validation.py` — the two gates; `scheme.py` — SEM-1 rules; `ids.py` — id scheme, base64url.
- `asset_shells/views.py` — level/extent, Value-Only, `$metadata`, idShortPath resolution.
- `asset_shells/repository.py` — read queries and cursor pagination; `db.py` — pool, tenant transaction,
  role posture check, bounded startup retry; `auth.py` — token verification; `logging.py` — JSON logs + scrub.
- `asset_shells/migrations/` — Alembic (run as the owner by `asset-shells migrate`), RLS policies, triggers, grants.
- `asset_shells/schemas/` — the vendored official JSON Schema (CC-BY-4.0, NOTICE).
- `tests/` — fixtures in SEM-1 shapes (`aas_fixtures.py`), assemblies A and B built with the pinned keystone
  from byte-identical commons copies (`assembly_fixtures.py`, `fixtures/assembly-commons`), the twin-graph
  suite (`test_twin_graph.py`), tenancy proofs, contract tests
  (`contract/specs` = official OpenAPI, CC-BY-4.0), publish/read/auth/ops/manifest suites.
- `web/` — the landing (static, Caddy, non-root). `infra/k8s/production/` — Deployments, Services,
  NetworkPolicies, kustomization with placeholder digests. `enclii.yaml` — project + two services.
- `scripts/` — test-database roles, licence gate, manifest rules.

## Related repositories and contracts

The README's [Related repositories and contracts](README.md#related-repositories-and-contracts)
section links the keystone documents this service enforces (SEM-1 projection, ASM-1) and the
repositories that publish to it. A rule about what a valid assembly or shell is belongs in
the keystone, not here.

## Working here

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
# database tests need ASSET_SHELLS_TEST_ADMIN_URL / ASSET_SHELLS_TEST_APP_URL (README "Local development")
.venv/bin/pytest -q --cov=asset_shells --cov-fail-under=80
```

Rules: Conventional Commits; new files ≤ 600 lines (fleet gate warns > 600, fails > 800); no new runtime
dependency without an AGPL-compatible licence (`scripts/check-licenses.sh`); CI runs on GitHub-hosted
`ubuntu-24.04` (public repo — fork PRs must never reach MADFAM runners); never push to `main`, never merge,
never deploy. Schema changes are new Alembic revisions; bump `db.EXPECTED_SCHEMA_REVISION` with them.

## Deploy shape (public-safe summary)

Through the Enclii platform: one project, two services (`asset-shells` API on 80 → 8000 at
`asset-shells-api.madfam.io`; `asset-shells-web` landing on 80 → 8080 at `asset-shells.madfam.io`), a
managed Postgres database, ingress only through the tunnel. The migrate init container uses the owner
credentials; the API container only the runtime role's URL. Image digests in
`infra/k8s/production/kustomization.yaml` are all-zero placeholders until a signed build is pinned. The
owner-only steps are in `docs/operator-setup.md`; their private gotchas are in the operations repository.
