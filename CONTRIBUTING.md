# Contributing

asset-shells is AGPL-3.0-only: if you run a modified version as a network service, you owe your users
the source. Contributions are accepted under the same licence.

- Read [AGENTS.md](./AGENTS.md) first — it states the doctrines a change may not break.
- Every new behaviour ships with tests; database behaviour is tested against PostgreSQL, never mocked
  (README, "Local development"). A tenancy-relevant change adds to `tests/test_tenancy.py`.
- A new Part 2 operation carries its official `operationId` and is added to `IMPLEMENTED` and to the
  request list in `tests/test_contract.py`, so its route and response are checked against the official
  OpenAPI. Do not describe the service as conforming to a profile it does not fully implement.
- Never add a dependency whose licence is not AGPL-compatible; `scripts/check-licenses.sh` fails the build.
- Conventional Commits; keep PRs small; CI must be green (lint, tests with coverage ≥ 80 %, licence gate,
  manifest rules, image build).
