"""Database access: one small pool, one transaction per request, tenant context per transaction.

Tenancy contract (pravara's lessons applied from day one):

* The runtime connects as a NON-OWNER role. ``check_role_posture`` refuses to start when that role
  owns a table, is a superuser, or bypasses row-level security.
* Every transaction sets exactly one setting, ``app.tenant_id``, with ``set_config(name, value, true)``
  — the parameterised form of ``SET LOCAL``: it lasts until the transaction ends and is never
  interpolated into SQL text. Anonymous and type-publishing transactions set it to the empty string,
  which matches no tenant (``tenant_id`` can never be empty).
* The tables carry ``FORCE ROW LEVEL SECURITY``; the policies live in the migration.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .logging import describe_db_error
from .settings import get_settings

log = logging.getLogger(__name__)

TENANT_SETTING = "app.tenant_id"
_pool: ConnectionPool | None = None

# The migration's head revision; /ready fails while the schema is older or newer than the code.
EXPECTED_SCHEMA_REVISION = "0001_initial"


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached within the startup bound."""


class UnsafeDatabaseRole(RuntimeError):
    """The runtime role would not be subject to row-level security."""


def _configure_connection(conn: psycopg.Connection) -> None:
    timeout = int(get_settings().db_statement_timeout_ms)
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (str(timeout),))
    conn.execute("SELECT set_config('application_name', 'asset-shells', false)")
    conn.commit()


def open_pool(url: str | None = None) -> ConnectionPool:
    """Open the pool (idempotent). Raises DatabaseUnavailable after the bounded startup retry."""
    global _pool
    if _pool is not None:
        return _pool
    s = get_settings()
    conninfo = url or s.app_database_url
    if not conninfo:
        raise DatabaseUnavailable("APP_DATABASE_URL is not set")

    def new_pool() -> ConnectionPool:
        return ConnectionPool(
            conninfo,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=_configure_connection,
            open=False,
            name="asset-shells",
        )

    pool = new_pool()
    deadline = time.monotonic() + s.db_startup_retry_seconds
    delay = 0.5
    while True:
        try:
            # Several attempts fit in the window: each waits at most a third of it (1-5 s).
            pool.open(wait=True, timeout=min(5.0, max(1.0, s.db_startup_retry_seconds / 3)))
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except (psycopg.OperationalError, TimeoutError) as exc:
            # Connection-level failures only; SQL errors are never retried.
            pool.close()
            if time.monotonic() >= deadline:
                log.error("database unreachable after startup retry window: %s", describe_db_error(exc))
                raise DatabaseUnavailable("database unreachable within DB_STARTUP_RETRY_SECONDS") from None
            log.warning("database not reachable yet, retrying: %s", describe_db_error(exc))
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
            pool = new_pool()
        except psycopg.Error:
            pool.close()
            raise
    _pool = pool
    return pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise DatabaseUnavailable("the database pool is not open")
    return _pool


@contextmanager
def transaction(tenant_id: str | None) -> Iterator[psycopg.Cursor]:
    """One transaction with the tenant context set. ``tenant_id=None`` means no tenant: only
    tenant-less (type) rows are visible and only tenant-less rows may be written."""
    with get_pool().connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, tenant_id or ""))
        yield cur


def check_role_posture(pool: ConnectionPool | None = None) -> None:
    """Fail closed if the runtime role could see past row-level security."""
    pool = pool or get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user")
        row = cur.fetchone()
        if row is None or row["rolsuper"] or row["rolbypassrls"]:
            raise UnsafeDatabaseRole("the runtime role is a superuser or bypasses row-level security")
        cur.execute(
            """
            SELECT count(*) AS owned FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
              AND pg_has_role(current_user, c.relowner, 'USAGE')
            """
        )
        owned = cur.fetchone()["owned"]
        conn.rollback()
    if owned:
        raise UnsafeDatabaseRole("the runtime role owns (or inherits ownership of) tables in its schema")


def schema_revision(pool: ConnectionPool | None = None) -> str | None:
    pool = pool or get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        conn.rollback()
    return row["version_num"] if row else None
