# Operator setup — the steps only the owner can take

> **Boundary note (public-safe).** These are the public steps and their order. Host names of internal
> services, store paths, client identifiers and every credential stay in the private operations
> repository; placeholders in `<angle brackets>` stand for them. Never paste a secret into a chat, an
> issue, a PR or this file.

Nothing below has been done yet. Order matters: 1 → 2 → 3 → 4 → 5 → 6; step 7 is the go-live decision.

## 1. Publish the repository

The repository `madfam-org/asset-shells` exists, is public and empty. From the local clone that holds
`main` (one skeleton commit) and `feat/service-v1`:

```bash
git push -u origin main
git push -u origin feat/service-v1
gh pr create --repo madfam-org/asset-shells --base main --head feat/service-v1 --draft \
  --title "feat: asset-shells v1 — AAS store, Part 2 read API subset, publish API, tenancy" \
  --body-file <report-or-PR-body file>
```

Then wait for CI on the draft PR (GitHub-hosted `ubuntu-24.04`; nothing deploys). Recommended: protect
`main` (require the `CI` checks and a review).

## 2. Janua — audience, scopes and one client per edge

A Janua PR (platform-admin territory; the registry is `apps/api/app/core/reserved_oauth_boundaries.py`):

```diff
 ECOSYSTEM_SERVICE_AUDIENCES: frozenset[str] = frozenset(
-    {"karafiel-api", "dhanam-api", "yantra4d-api"}
+    {"karafiel-api", "dhanam-api", "yantra4d-api", "asset-shells-api"}
 )
 ...
         "yantra4d:render",
+        "asset-shells:read",
+        "asset-shells:publish-types",
+        "asset-shells:publish-instances",
     }
```

plus rows in `docs/service-tokens.md`, the pinned members in
`tests/unit/core/test_reserved_oauth_boundaries.py`, and three entries in `SERVICE_CLIENTS` of
`apps/api/scripts/seed_service_clients.py`:

```jsonc
{ "name": "yantra4d-asset-shells-publisher", "audience": "asset-shells-api",
  "allowed_scopes": ["asset-shells:publish-types"], "grant_types": ["client_credentials"],
  "redirect_uris": [], "is_confidential": true }

{ "name": "fashion-cabinet-asset-shells-publisher", "audience": "asset-shells-api",
  "allowed_scopes": ["asset-shells:publish-types"], "grant_types": ["client_credentials"],
  "redirect_uris": [], "is_confidential": true }

// ORG-BOUND: its tokens carry tenant_id = the producing organisation. A second producing
// organisation needs its own client (Janua: "a second tenant needs its own client").
{ "name": "pravara-asset-shells-publisher", "audience": "asset-shells-api",
  "allowed_scopes": ["asset-shells:publish-instances", "asset-shells:read"],
  "grant_types": ["client_credentials"], "redirect_uris": [], "is_confidential": true,
  "organization_id": "<producing organisation uuid>" }
```

After that PR is merged and Janua is promoted, as a platform admin:

```bash
cd apps/api && python scripts/seed_service_clients.py   # DATABASE_URL per the private Janua recipe
```

Each `client_secret` is printed once. Hand each pair to its consumer's runtime through the secret
intake, never through chat (target names: `enclii secrets intake targets`):

```bash
enclii secrets intake submit <yantra4d target> --reason "asset-shells publish-types client"
enclii secrets intake submit <fashion-cabinet target> --reason "asset-shells publish-types client"
enclii secrets intake submit <pravara-mes target> --reason "asset-shells publish-instances client"
# keys: ASSET_SHELLS_CLIENT_ID, ASSET_SHELLS_CLIENT_SECRET (masked prompt)
```

Client ids are never committed to a public repository (yantra4d and pravara-mes are public).

## 3. Enclii — project, database, domains

```bash
enclii onboard --repo madfam-org/asset-shells --project asset-shells \
  --manifest-path infra/k8s/production --preflight --dry-run
```

Prepare the two database passwords and the secrets file in a private, short-lived location:

```bash
umask 077
OWNER_PW=$(openssl rand -hex 24); APP_PW=$(openssl rand -hex 24)
cat > ./asset-shells.env <<EOF
DATABASE_URL=postgresql://<owner role>:${OWNER_PW}@<pooler host>:<port>/asset_shells
APP_DATABASE_URL=postgresql://asset_shells_app:${APP_PW}@<pooler host>:<port>/asset_shells
EOF
enclii onboard --repo madfam-org/asset-shells --project asset-shells \
  --manifest-path infra/k8s/production --db-name asset_shells --db-password "$OWNER_PW" \
  --secrets-file ./asset-shells.env --preflight
rm -f ./asset-shells.env
```

This creates the namespace (default-deny), the database and its owner role, the pooler entry, the Secret
`asset-shells-credentials` (keys `DATABASE_URL`, `APP_DATABASE_URL` — the names the Deployment reads),
and the tunnel routes and DNS records for `asset-shells.madfam.io` and `asset-shells-api.madfam.io` from
`enclii.yaml`. Onboarding is not idempotent; repairs go through `enclii onboard ensure`.

## 4. The runtime database role

The service connects as a NON-owner role that row-level security applies to; the migration grants it
exactly what it needs and fails visibly if it does not exist. Through the platform's database
administration path, connected to `asset_shells` as an administrator, with `APP_PW` from step 3:

```bash
psql "<admin connection to asset_shells>" -v ON_ERROR_STOP=1 -v app_pw="$APP_PW" <<'SQL'
CREATE ROLE asset_shells_app LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE
  CONNECTION LIMIT 6 PASSWORD :'app_pw';
GRANT CONNECT ON DATABASE asset_shells TO asset_shells_app;
SQL
unset OWNER_PW APP_PW
```

Then add `asset_shells_app` to the connection pooler's user list (platform step; until then `/ready`
answers 503 — that is the signal, not a bug). `CONNECTION LIMIT 6` = pool maximum 4 plus headroom.

## 5. Build and deploy wiring (owner decision)

This repository has no build-and-deploy workflow on purpose. To enable deploys, add
`.github/workflows/build-deploy.yml` calling the shared Enclii reusable workflow at a pinned tag, as
tlacuilo does, with two services:

```yaml
services: |
  [
    {"name":"asset-shells-api", "dockerfile":"Dockerfile", "paths":"asset_shells pyproject.toml Dockerfile"},
    {"name":"asset-shells-web", "dockerfile":"web/Dockerfile", "paths":"web"}
  ]
```

It builds, signs (keyless cosign) and pins the digests in `infra/k8s/production/kustomization.yaml`,
replacing the all-zero placeholders; ArgoCD then syncs. Known bootstrap gotcha: the first push has no
previous commit, so the change detection skips the build — a second commit touching
`infra/k8s/production/` triggers it. Flip `autoDeploy` in `enclii.yaml` only when promotion is ruled.

## 6. Smoke after the first deploy

```bash
curl -fsS https://asset-shells.madfam.io/health
curl -fsS https://asset-shells-api.madfam.io/health
curl -fsS https://asset-shells-api.madfam.io/ready          # 200 once the role, pooler and migration are in place
curl -fsS https://asset-shells-api.madfam.io/api/v3.1/shells
```

A first type publish uses the yantra4d client's token (audience `asset-shells-api`, scope
`asset-shells:publish-types`) against `PUT /madfam/v1/type-environments/solid-hyperobjects/<pin sha>`.

## 7. Open rulings that affect operation

- Producing organisations beyond the first need their own org-bound pravara client (step 2), or a
  delegated-tenant design — an owner ruling.
- The outbox relay (webhooks/SSE) is not built; subscribers get nothing until it is.
