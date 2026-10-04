"""Initial schema: shells, submodels, asset-id index, concept descriptions, type releases,
append-only passport events, outbox; row-level security forced on every tenant-bearing table.

Tenancy model
-------------
* ``tenant_id IS NULL`` marks a TYPE row (public, tenant-less, immutable). An INSTANCE row carries the
  tenant from the publisher's token.
* One setting name: ``app.tenant_id``, set per transaction by the service (``set_config(..., true)``).
* Reads see type rows plus rows of the current tenant. Writes of type rows are allowed only in a
  transaction WITHOUT tenant context; writes of instance rows only for the current tenant.
* ``tenant_key`` (= coalesce(tenant_id, '')) lets composite foreign keys pin every child row
  (submodel, asset id, passport event) to its shell's tenant — RLS does not apply to FK checks,
  so this closes the "reference another tenant's parent" gap at the schema level.
* Triggers make shells immutable, type submodels immutable, and passport events append-only —
  for every role, the owner included.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-02
"""

from __future__ import annotations

import re

from alembic import context, op

from asset_shells.settings import get_settings

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TENANT = "current_setting('app.tenant_id', true)"
# Type rows only without tenant context; instance rows only for the current tenant.
WRITE_CHECK = f"(CASE WHEN tenant_id IS NULL THEN coalesce({TENANT}, '') = '' ELSE tenant_id = {TENANT} END)"
READ_CHECK = f"(tenant_id IS NULL OR tenant_id = {TENANT})"

TENANT_COLUMNS = """
  tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) > 0),
  tenant_key text GENERATED ALWAYS AS (coalesce(tenant_id, '')) STORED
"""


def _app_role() -> str:
    role = context.config.attributes.get("app_db_role") or get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError("APP_DB_ROLE must be a lower-case SQL identifier")
    return role


def upgrade() -> None:
    role = _app_role()
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
            RAISE EXCEPTION 'runtime role {role} does not exist; create it first (docs/operator-setup.md)';
          END IF;
        END $$;
        """
    )

    op.execute(
        f"""
        CREATE TABLE shells (
          seq bigserial UNIQUE NOT NULL,
          id text PRIMARY KEY,
          kind text NOT NULL CHECK (kind IN ('type', 'instance')),
          {TENANT_COLUMNS},
          id_short text,
          global_asset_id text,
          derived_from text REFERENCES shells (id),
          doc jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          -- instances: hash of the whole environment as first published (idempotent replays)
          publish_sha256 text CHECK (publish_sha256 ~ '^[0-9a-f]{{64}}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK ((kind = 'type') = (tenant_id IS NULL)),
          UNIQUE (id, tenant_key)
        );
        CREATE INDEX shells_id_short ON shells (id_short);
        CREATE INDEX shells_tenant_seq ON shells (tenant_key, seq);

        CREATE TABLE submodels (
          seq bigserial UNIQUE NOT NULL,
          id text PRIMARY KEY,
          shell_id text NOT NULL,
          kind text NOT NULL CHECK (kind IN ('type', 'instance')),
          {TENANT_COLUMNS},
          id_short text,
          semantic_refs text[] NOT NULL DEFAULT '{{}}',
          semantic_values text[] NOT NULL DEFAULT '{{}}',
          doc jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK ((kind = 'type') = (tenant_id IS NULL)),
          UNIQUE (id, tenant_key),
          FOREIGN KEY (shell_id, tenant_key) REFERENCES shells (id, tenant_key)
        );
        CREATE INDEX submodels_shell ON submodels (shell_id);
        CREATE INDEX submodels_id_short ON submodels (id_short);
        CREATE INDEX submodels_semantic_refs ON submodels USING gin (semantic_refs);
        CREATE INDEX submodels_semantic_values ON submodels USING gin (semantic_values);

        CREATE TABLE asset_ids (
          shell_id text NOT NULL,
          {TENANT_COLUMNS},
          position integer NOT NULL,
          name text NOT NULL,
          value text NOT NULL,
          doc jsonb NOT NULL,
          PRIMARY KEY (shell_id, position),
          FOREIGN KEY (shell_id, tenant_key) REFERENCES shells (id, tenant_key)
        );
        CREATE INDEX asset_ids_name_value ON asset_ids (name, value);

        CREATE TABLE concept_descriptions (
          seq bigserial UNIQUE NOT NULL,
          id text PRIMARY KEY,
          doc jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE type_releases (
          commons text NOT NULL,
          sha text NOT NULL CHECK (sha ~ '^[0-9a-f]{{40}}$'),
          content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          shell_ids text[] NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (commons, sha)
        );

        CREATE TABLE passport_events (
          seq bigserial UNIQUE NOT NULL,
          event_id uuid PRIMARY KEY,
          shell_id text NOT NULL,
          submodel_id text NOT NULL,
          tenant_id text NOT NULL CHECK (length(tenant_id) > 0),
          tenant_key text GENERATED ALWAYS AS (coalesce(tenant_id, '')) STORED,
          position integer NOT NULL CHECK (position >= 0),
          doc jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          received_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (submodel_id, position),
          FOREIGN KEY (shell_id, tenant_key) REFERENCES shells (id, tenant_key),
          FOREIGN KEY (submodel_id, tenant_key) REFERENCES submodels (id, tenant_key)
        );
        CREATE INDEX passport_events_shell_seq ON passport_events (shell_id, seq);

        CREATE TABLE outbox (
          id bigserial PRIMARY KEY,
          tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) > 0),
          topic text NOT NULL,
          subject_id text NOT NULL,
          payload jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz
        );
        CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
        """
    )

    # Immutability and append-only, enforced for every role (owner included).
    op.execute(
        """
        CREATE FUNCTION asset_shells_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% on % is not allowed (%)', TG_OP, TG_TABLE_NAME, TG_ARGV[0]
            USING ERRCODE = 'restrict_violation';
        END $$;

        CREATE FUNCTION asset_shells_type_submodel_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.tenant_id IS NULL OR NEW.id <> OLD.id OR NEW.shell_id <> OLD.shell_id
             OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
            RAISE EXCEPTION 'type submodels and submodel identity are immutable'
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER shells_immutable BEFORE UPDATE OR DELETE ON shells
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('shells are immutable');
        CREATE TRIGGER asset_ids_immutable BEFORE UPDATE OR DELETE ON asset_ids
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('asset ids are immutable');
        CREATE TRIGGER type_releases_immutable BEFORE UPDATE OR DELETE ON type_releases
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('type releases are immutable');
        CREATE TRIGGER passport_events_append_only BEFORE UPDATE OR DELETE ON passport_events
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('passport events are append-only');
        CREATE TRIGGER submodels_guard BEFORE UPDATE ON submodels
          FOR EACH ROW EXECUTE FUNCTION asset_shells_type_submodel_immutable();
        CREATE TRIGGER submodels_no_delete BEFORE DELETE ON submodels
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('submodels are not deleted');
        """
    )

    # Row-level security, forced (applies to the table owner too).
    for table in ("shells", "submodels", "asset_ids"):
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_read ON {table} FOR SELECT USING {READ_CHECK};
            CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK {WRITE_CHECK};
            """
        )
    op.execute(
        f"""
        CREATE POLICY submodels_update ON submodels FOR UPDATE
          USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT});

        ALTER TABLE passport_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE passport_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY passport_events_read ON passport_events FOR SELECT USING (tenant_id = {TENANT});
        CREATE POLICY passport_events_insert ON passport_events FOR INSERT WITH CHECK (tenant_id = {TENANT});

        ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
        ALTER TABLE outbox FORCE ROW LEVEL SECURITY;
        CREATE POLICY outbox_insert ON outbox FOR INSERT WITH CHECK {WRITE_CHECK};
        -- The relay (a later lane) runs as the schema owner: it reads and marks rows; the runtime
        -- role has no SELECT/UPDATE grant on the outbox at all.
        CREATE POLICY outbox_owner_read ON outbox FOR SELECT TO CURRENT_USER USING (true);
        CREATE POLICY outbox_owner_mark ON outbox FOR UPDATE TO CURRENT_USER USING (true) WITH CHECK (true);
        """
    )

    # Least privilege for the runtime role. No DELETE, no TRUNCATE, no DDL anywhere.
    op.execute(
        f"""
        REVOKE ALL ON shells, submodels, asset_ids, concept_descriptions, type_releases,
          passport_events, outbox FROM PUBLIC;
        GRANT USAGE ON SCHEMA {get_schema()} TO {role};
        GRANT SELECT, INSERT ON shells, asset_ids, type_releases, passport_events TO {role};
        GRANT SELECT, INSERT, UPDATE ON submodels, concept_descriptions TO {role};
        GRANT INSERT ON outbox TO {role};
        GRANT SELECT ON alembic_version TO {role};
        GRANT USAGE ON SEQUENCE shells_seq_seq, submodels_seq_seq, concept_descriptions_seq_seq,
          passport_events_seq_seq, outbox_id_seq TO {role};
        """
    )


def get_schema() -> str:
    return "public"


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS outbox, passport_events, type_releases, concept_descriptions,
          asset_ids, submodels, shells CASCADE;
        DROP FUNCTION IF EXISTS asset_shells_reject_change();
        DROP FUNCTION IF EXISTS asset_shells_type_submodel_immutable();
        """
    )
