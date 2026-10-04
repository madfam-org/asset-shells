"""Operations: liveness/readiness, lifespan, role posture, bounded startup retry, log scrubbing,
settings guards, CLI."""

from __future__ import annotations

import io
import json
import logging
import os
import time

import psycopg
import pytest
from conftest import ADMIN_URL_ENV, APP_URL_ENV
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from asset_shells import auth, db, repository, settings
from asset_shells.cli import dev_token, main
from asset_shells.ids import b64url_encode as enc
from asset_shells.logging import DbErrorScrubFilter, JsonFormatter, configure_logging, describe_db_error

SECRET = "hunter2-do-not-log"


@pytest.fixture
def captured() -> io.StringIO:
    """A handler wired exactly like production: JSON formatter + DB scrub filter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(DbErrorScrubFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old)


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200


def test_ready_fails_on_schema_mismatch_and_closed_pool(client, monkeypatch):
    monkeypatch.setattr(db, "EXPECTED_SCHEMA_REVISION", "9999_future")
    assert client.get("/ready").json() == {"status": "schema revision mismatch"}
    monkeypatch.undo()
    monkeypatch.setattr(db, "_pool", None)
    response = client.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "database pool not open"}


def test_ready_scrubs_database_errors(client, monkeypatch, captured):
    def boom(*_a, **_k):
        raise psycopg.OperationalError(f"connection to server failed: password {SECRET}")

    monkeypatch.setattr(db, "schema_revision", boom)
    response = client.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "database round-trip failed"}
    assert SECRET not in captured.getvalue() and "OperationalError" in captured.getvalue()


def test_request_path_db_errors_are_scrubbed(client, monkeypatch, captured):
    def boom(*_a, **_k):
        raise psycopg.errors.UniqueViolation(f'duplicate key value violates "x" DETAIL: Key (id)=({SECRET})')

    monkeypatch.setattr(repository, "get_shell", boom)
    response = client.get(f"/api/v3.1/shells/{enc('urn:x')}")
    assert response.status_code == 500
    assert response.json()["messages"][0]["code"] == "database_error"
    assert SECRET not in response.text and SECRET not in captured.getvalue()
    assert "UniqueViolation" in captured.getvalue()

    def gone(*_a, **_k):
        raise psycopg.OperationalError(SECRET)

    monkeypatch.setattr(repository, "get_shell", gone)
    assert client.get(f"/api/v3.1/shells/{enc('urn:x')}").status_code == 503

    def unavailable(*_a, **_k):
        raise db.DatabaseUnavailable("pool closed")

    monkeypatch.setattr(db, "transaction", unavailable)
    response = client.get("/api/v3.1/shells")
    assert response.status_code == 503 and response.json()["messages"][0]["code"] == "database_unavailable"


def test_scrub_filter_and_formatter(captured):
    log = logging.getLogger("asset_shells.test")
    try:
        try:
            raise psycopg.errors.UniqueViolation(SECRET)
        except psycopg.Error as exc:
            raise RuntimeError("wrapped") from exc
    except RuntimeError:
        log.exception("publish failed")
    logging.getLogger("psycopg.pool").warning("error connecting: %s", SECRET)
    log.info("plain", extra={"request_id": "r1", "status": 200})
    lines = [json.loads(line) for line in captured.getvalue().splitlines()]
    assert SECRET not in captured.getvalue()
    assert "db_error UniqueViolation" in lines[0]["msg"] and "exc" not in lines[0]
    assert lines[1]["msg"].endswith("(detail scrubbed)")
    assert lines[2]["request_id"] == "r1" and lines[2]["status"] == 200
    assert describe_db_error(ValueError("x")) == "ValueError sqlstate=-"


def test_non_db_exceptions_keep_their_traceback(captured):
    try:
        raise KeyError("ordinary")
    except KeyError:
        logging.getLogger("asset_shells.test").exception("ordinary failure")
    assert json.loads(captured.getvalue().splitlines()[0])["exc"].startswith("Traceback")


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO")
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_asset_shells_handler", False)]
    assert len(marked) == 1
    assert all(any(isinstance(f, DbErrorScrubFilter) for f in h.filters) for h in logging.getLogger().handlers)


def test_lifespan_opens_checks_and_closes_its_pool(clean_db, monkeypatch, captured):
    from asset_shells.app import app

    monkeypatch.setattr(db, "_pool", None)
    with TestClient(app) as client:
        assert db._pool is not None
        assert client.get("/ready").status_code == 200
        assert client.get("/api/v3.1/shells").status_code == 200
    assert db._pool is None
    text = captured.getvalue()
    assert "asset-shells 0.1.0 started" in text and '"path": "/api/v3.1/shells"' in text
    assert '"/ready"' not in text  # probes are not access-logged


def test_owner_role_is_refused(migrated):
    pool = ConnectionPool(
        os.environ[ADMIN_URL_ENV], min_size=1, max_size=1, open=True, kwargs={"row_factory": psycopg.rows.dict_row}
    )
    try:
        with pytest.raises(db.UnsafeDatabaseRole):
            db.check_role_posture(pool)
    finally:
        pool.close()


def test_runtime_role_passes_posture_and_reads_revision(pool):
    db.check_role_posture()
    assert db.schema_revision() == db.EXPECTED_SCHEMA_REVISION


def test_startup_retry_is_bounded_and_scrubbed(monkeypatch, captured):
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("DB_STARTUP_RETRY_SECONDS", "3")
    settings.reset_settings_cache()
    started = time.monotonic()
    try:
        with pytest.raises(db.DatabaseUnavailable):
            db.open_pool(f"postgresql://nobody:{SECRET}@127.0.0.1:1/none?connect_timeout=1")
    finally:
        settings.reset_settings_cache()
    assert time.monotonic() - started < 20
    text = captured.getvalue()
    assert "retrying" in text and "unreachable after startup retry window" in text
    assert SECRET not in text and "nobody" not in text


def test_open_pool_requires_a_url(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("APP_DATABASE_URL", "")
    settings.reset_settings_cache()
    try:
        with pytest.raises(db.DatabaseUnavailable):
            db.open_pool()
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()
    with pytest.raises(db.DatabaseUnavailable):
        monkeypatch.setattr(db, "_pool", None)
        db.get_pool()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ASSET_SHELLS_ENV": "production", "JWKS_PATH": "/tmp/jwks.json"}, "JWKS_PATH"),
        ({"DB_POOL_MIN": "5", "DB_POOL_MAX": "2"}, "inconsistent"),
        ({"DB_POOL_MAX": "50"}, "connection budget"),
    ],
)
def test_settings_guards(monkeypatch, env, message):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    settings.reset_settings_cache()
    try:
        with pytest.raises(RuntimeError, match=message):
            settings.get_settings()
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()


def test_dev_token_round_trip(tmp_path, monkeypatch, capsys):
    token = dev_token(str(tmp_path), ["asset-shells:read"], "org-dev", "https://auth.madfam.io", "asset-shells-api")
    monkeypatch.setenv("JWKS_PATH", str(tmp_path / "jwks.json"))
    settings.reset_settings_cache()
    auth.reset_key_cache()
    try:
        principal = auth.verify_token(token)
        assert principal.tenant_id == "org-dev" and principal.has("asset-shells:read")
        again = dev_token(str(tmp_path), [], None, "https://auth.madfam.io", "asset-shells-api")  # key reused
        assert auth.verify_token(again).tenant_id is None
        assert main(["dev-token", "--dir", str(tmp_path), "--scope", "asset-shells:read"]) == 0
        assert capsys.readouterr().out.count(".") == 2
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()
        auth.reset_key_cache()


def test_cli_migrate_is_idempotent(migrated, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ[ADMIN_URL_ENV])
    settings.reset_settings_cache()
    try:
        assert main(["migrate"]) == 0
    finally:
        settings.reset_settings_cache()
    assert os.environ[APP_URL_ENV]
