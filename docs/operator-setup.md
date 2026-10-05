# Operator setup — the steps only the owner can take

> **Boundary note (public-safe).** These are the public steps and their order. Host names of internal
> services, store paths, client identifiers and every credential stay in the private operations
> repository; placeholders in `<angle brackets>` stand for them. Never paste a secret into a chat, an
> issue, a PR or this file.

Every credential in this service is generated and filed by the platform: the database owner password, the
runtime role's password and each publisher's Janua client secret. No step below has anyone generate, type, copy
or store a credential, and none prints one. Run each command on its own; never paste a dry run and its real run
as one block.

## 1. Publish the repository — done

`madfam-org/asset-shells` is public and its CI runs on GitHub-hosted runners. Recommended: protect `main`
(require the CI checks and a review).

## 2. Janua — audience, scopes and one client per edge — done

Janua reserves the audience `asset-shells-api` and the scopes `asset-shells:read`,
`asset-shells:publish-types` and `asset-shells:publish-instances`. The platform provisioner (Enclii's ecosystem
OIDC registry) created the publisher clients and filed each id and secret into its consumer's runtime secrets;
nobody saw a secret. Re-runs are idempotent and print no secret:

```bash
enclii secrets provision oidc --profile admin --platform yantra4d-asset-shells-publisher
enclii secrets provision oidc --profile admin --platform fashion-cabinet-asset-shells-publisher
enclii secrets provision oidc --profile admin --platform pravara-asset-shells-publisher-<organisation slug>
```

### One client per producing organisation

Instance shells and passport events are tenant-scoped under FORCE row-level security. The tenant of every
instance write and read is the token's `tenant_id` claim, which Janua sets from the client's organisation
binding. No request field can choose it (`asset_shells/auth.py`, `require_tenant`), and a token without the
claim is refused with 403 `missing_tenant` (`tests/test_instances.py::test_instance_auth`,
`tests/test_tenancy.py`). So:

- **Each producing organisation gets its own org-bound publisher client.** The name is
  `pravara-asset-shells-publisher.<organisation slug>`, with the scopes `asset-shells:publish-instances` and
  `asset-shells:read`. A client is never shared between organisations, and an organisation never publishes
  through another's client. One client, one tenant: an organisation's audit trail is its client's.
- To add an organisation: add its edge to Enclii's ecosystem OIDC registry (with the organisation's id), run
  `enclii secrets provision oidc --profile admin --platform pravara-asset-shells-publisher-<slug>`, and map the
  tenant to the new credential in pravara-mes's configuration. Nothing changes in this repository.
- Type publishers (`yantra4d-…`, `fashion-cabinet-…`) are not org-bound: type shells are tenant-less.

Client ids and organisation ids are never committed to a public repository.

## 3. Enclii — project, database, roles, domains

One command. Switchyard creates everything and generates every value server-side:

```bash
enclii onboard --repo madfam-org/asset-shells --project asset-shells \
  --manifest-path infra/k8s/production --preflight \
  --db-name asset_shells --generate-db-password --db-connection-limit 2 \
  --app-role asset_shells_app --app-role-connection-limit 6
```

This creates:
- the namespace (default-deny);
- the database `asset_shells` and its owner role with a generated password (`CONNECTION LIMIT 2`: only the
  one-connection migrate init container uses it), the pooler entry, and the owner URL in
  `asset-shells-credentials` under `DATABASE_URL`;
- the runtime role `asset_shells_app`: no superuser, no `BYPASSRLS`, `CONNECTION LIMIT 6` (pool maximum 4 plus
  headroom for a rollout), a generated password, its pooler line and its URL under `APP_DATABASE_URL`. The
  migration grants it exactly what it needs on first deploy and fails visibly if the role is missing;
- from `enclii.yaml`, the tunnel routes and DNS records for `asset-shells.madfam.io` and
  `asset-shells-api.madfam.io`.

To preview, run the same line with `--dry-run` instead of `--preflight` **as a separate step**; it lists role
and key names, never values. Repairs go through `enclii onboard ensure` with the same flags: it keeps every
existing role and value, and only a `--rotate-*` flag replaces one. The shared Postgres has a 100-connection
budget; raise a limit deliberately, with `ensure`.

## 4. The runtime database role — folded into step 3

Step 3 creates `asset_shells_app` and its pooler entry. No `psql` step remains.

## 5. Build and deploy

`.github/workflows/build-deploy.yml` calls Enclii's reusable workflow (pinned by commit SHA) for both images,
signs them and commits the digest pins to `infra/k8s/production/kustomization.yaml`; the GitOps sync then rolls
them out. It runs **only when dispatched** while `autoDeploy` stays `false` (promotion not yet ruled):

```bash
gh workflow run build-deploy.yml --repo madfam-org/asset-shells
```

Push-on-main is a separate pull request once promotion is ruled.

## 6. Smoke after the first deploy

```bash
curl -fsS https://asset-shells.madfam.io/health
curl -fsS https://asset-shells-api.madfam.io/health
curl -fsS https://asset-shells-api.madfam.io/ready          # 200 once the role, pooler and migration are in place
curl -fsS https://asset-shells-api.madfam.io/api/v3.1/shells
```

A first type publish uses the yantra4d client's token (audience `asset-shells-api`, scope
`asset-shells:publish-types`) against `PUT /madfam/v1/type-environments/solid-hyperobjects/<pin sha>`.

## 7. Open items that affect operation

- The outbox relay (webhooks/SSE) is not built; subscribers get nothing until it is.
