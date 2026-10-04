"""The twin graph (ASM-1 §6): ``asset_edges``, a relational node/edge mapping over the stored shells.

Nodes are the assets the shells already describe (``globalAssetId``); an edge joins two of them:

* ``has_part``    — a BillOfMaterials ``HasPart`` (an assembly to each component; a cartridge to its hardware;
                    an instance assembly to its component instances);
* ``mates_with``  — an assembly's ``Mates`` element, from component a's asset to component b's;
* ``derived_from``— an instance shell's ``derivedFrom``, from the instance asset to its type asset;
* ``same_as``     — reserved (asset-id equivalences); nothing writes it in this revision.

Edges are written in the SAME transaction as the publish that carries them, and only then: a replay that
creates no shell writes no edge. They follow the shells' rules exactly:

* ``tenant_id IS NULL`` is a type edge (public); an instance edge carries the publisher's tenant. The same
  ``READ_CHECK`` / ``WRITE_CHECK`` policies, with ``FORCE ROW LEVEL SECURITY`` (the owner is bound too).
* ``(via_shell_id, tenant_key)`` references the shell whose publish produced the edge, and
  ``(via_submodel_id, tenant_key)`` the submodel it was read from: an edge cannot point at another tenant's
  shell, the same composite-key pattern as every other child table (RLS does not apply to FK checks).
* Append-only for every role, the owner included: UPDATE and DELETE raise ``restrict_violation``.
* The runtime role gets SELECT and INSERT only, and stays a non-owner.

Revision ID: 0002_twin_graph
Revises: 0001_initial
Create Date: 2026-10-04
"""

from __future__ import annotations

import re

from alembic import context, op

from asset_shells.settings import get_settings

revision = "0002_twin_graph"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TENANT = "current_setting('app.tenant_id', true)"
WRITE_CHECK = f"(CASE WHEN tenant_id IS NULL THEN coalesce({TENANT}, '') = '' ELSE tenant_id = {TENANT} END)"
READ_CHECK = f"(tenant_id IS NULL OR tenant_id = {TENANT})"


def _app_role() -> str:
    role = context.config.attributes.get("app_db_role") or get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError("APP_DB_ROLE must be a lower-case SQL identifier")
    return role


def upgrade() -> None:
    role = _app_role()
    op.execute(
        """
        CREATE TABLE asset_edges (
          id bigserial PRIMARY KEY,
          from_asset_id text NOT NULL CHECK (length(from_asset_id) > 0),
          to_asset_id text NOT NULL CHECK (length(to_asset_id) > 0),
          kind text NOT NULL CHECK (kind IN ('has_part', 'mates_with', 'derived_from', 'same_as')),
          via_shell_id text NOT NULL,
          via_submodel_id text,
          position integer NOT NULL CHECK (position >= 0),
          props jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(props) = 'object'),
          tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) > 0),
          tenant_key text GENERATED ALWAYS AS (coalesce(tenant_id, '')) STORED,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (via_shell_id, position),
          FOREIGN KEY (via_shell_id, tenant_key) REFERENCES shells (id, tenant_key),
          FOREIGN KEY (via_submodel_id, tenant_key) REFERENCES submodels (id, tenant_key)
        );
        CREATE INDEX asset_edges_from ON asset_edges (from_asset_id, kind);
        CREATE INDEX asset_edges_to ON asset_edges (to_asset_id, kind);
        CREATE INDEX asset_edges_via_shell ON asset_edges (via_shell_id);

        CREATE TRIGGER asset_edges_append_only BEFORE UPDATE OR DELETE ON asset_edges
          FOR EACH ROW EXECUTE FUNCTION asset_shells_reject_change('asset edges are append-only');
        """
    )
    op.execute(
        f"""
        ALTER TABLE asset_edges ENABLE ROW LEVEL SECURITY;
        ALTER TABLE asset_edges FORCE ROW LEVEL SECURITY;
        CREATE POLICY asset_edges_read ON asset_edges FOR SELECT USING {READ_CHECK};
        CREATE POLICY asset_edges_insert ON asset_edges FOR INSERT WITH CHECK {WRITE_CHECK};

        REVOKE ALL ON asset_edges FROM PUBLIC;
        GRANT SELECT, INSERT ON asset_edges TO {role};
        GRANT USAGE ON SEQUENCE asset_edges_id_seq TO {role};
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_edges CASCADE")
