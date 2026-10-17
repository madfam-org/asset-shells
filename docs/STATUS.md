# asset-shells: status as of 2026-10-05

A dated snapshot for someone resuming from a fresh clone. **The
[open-PR list](https://github.com/madfam-org/asset-shells/pulls) is
authoritative**; when this file and GitHub disagree, GitHub wins. Operator
runbooks are kept privately; the public operator steps are
[`operator-setup.md`](operator-setup.md).

## Where it stands

- `main` is `5a89c085`. It accepts projection versions 1–3 and pins the
  keystone at `32cb58c` (hyperobjects-spec 0.10.0, `PROJECTION_VERSION` 3) in
  `pyproject.toml`.
- **Not deployed.** `main` has no build-and-deploy workflow; #8 adds one that
  runs only on manual dispatch. The store is empty, so no stored shell has had
  to move across projection versions yet.

## Landed recently

| PR | What it did |
|---|---|
| [#1](https://github.com/madfam-org/asset-shells/pull/1) | v1: the AAS store, a Part 2 read API subset, the publish API, tenancy under row-level security |
| [#2](https://github.com/madfam-org/asset-shells/pull/2) | The twin graph: `asset_edges`, publish-time assembly validation, graph and validation API (ASM-1 §6) |
| [#3](https://github.com/madfam-org/asset-shells/pull/3) | Projection-versioned type shells: a new projection is a new shell, not a 409 |
| [#4](https://github.com/madfam-org/asset-shells/pull/4) | Slug grammar aligned with the keystone's |
| [#5](https://github.com/madfam-org/asset-shells/pull/5), [#6](https://github.com/madfam-org/asset-shells/pull/6), [#7](https://github.com/madfam-org/asset-shells/pull/7) | Projection versions 2 (kinematics submodel) and 3 (canonical projection); keystone repins to `142db18`, then `32cb58c` |

## Open PRs, in merge order

| PR | Purpose | Precondition | Deploys |
|---|---|---|---|
| [#8](https://github.com/madfam-org/asset-shells/pull/8) | Dispatch-only build-and-deploy workflow; onboarding with generated secrets; drops the unused Postgres addon | CI green | No (the workflow runs only when dispatched) |
| [#9](https://github.com/madfam-org/asset-shells/pull/9) | Docs: related contracts, and this status file | CI green; any order | No |

## Next steps

1. Merge #8.
2. Operator onboarding with generated secrets (database owner, the
   `asset_shells_app` runtime role with a connection limit), then the first
   dispatched deploy and a smoke test through the read API. Nobody types or
   stores a credential.
3. **Keystone repin** with the other consumers after hyperobjects-spec#57 and
   #58: `pyproject.toml` and the fixture-A digest in
   `tests/assembly_fixtures.py`. A publisher must build with the same keystone
   SHA as this service, or its assembly shells get a 422.
4. **Publishers fill the store** (queued): yantra4d and Fashion Cabinet publish
   type shells, one machine client per producing organisation; pravara-mes
   publishes instance shells and passport events (pravara-mes#55).

Known gaps, not built yet: the outbox relay (webhooks and SSE), the concept
description read API, and `same_as` edges (allowed, written by nothing). A graph
walk lists an asset-to-asset edge once per stored projection version
(documented, not filtered). The test fixture text under
`tests/fixtures/assembly-commons` describes an older, smaller assembly A; it is
test data and need not track the solid commons.

## Cross-repo contracts

See the README's [Related repositories and contracts](../README.md#related-repositories-and-contracts):
the SEM-1 projection and ASM-1 (§5, §6, §9) in hyperobjects-spec, the assemblies
in solid-hyperobjects, GOC-1 digests in yantra4d, and instance shells and
passports from pravara-mes.
