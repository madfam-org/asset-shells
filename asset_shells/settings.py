"""Runtime configuration, read from the environment.

Every field states where its value comes from in production (``json_schema_extra.source``):
``env`` is a plain environment variable in the Deployment, ``secret`` arrives through a Kubernetes
Secret written by the platform, ``addon`` is written by the platform's Postgres addon, and ``code``
never comes from the cluster (tests and local development only)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVIRONMENTS = frozenset({"local", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    asset_shells_env: str = Field("production", json_schema_extra={"source": "env"})

    # Identity: Janua RS256 service tokens only. The issuer is configurable; the audience is the
    # one reserved for this service.
    janua_issuer: str = Field("https://auth.madfam.io", json_schema_extra={"source": "env"})
    janua_jwks_url: str = Field("", json_schema_extra={"source": "env"})  # default: <issuer>/.well-known/jwks.json
    janua_audience: str = Field("asset-shells-api", json_schema_extra={"source": "env"})
    jwks_cache_seconds: int = Field(3600, json_schema_extra={"source": "env"})
    # A local JWKS file (scripts/dev_token.py writes one). Honoured only in local/test environments.
    jwks_path: str = Field("", json_schema_extra={"source": "code"})

    # Runtime connection: a NON-OWNER role that is subject to row-level security.
    app_database_url: str = Field("", json_schema_extra={"source": "secret"})
    # Owner connection, used ONLY by migrations (alembic, the init container).
    database_url: str = Field("", json_schema_extra={"source": "addon"})
    # The role the migration grants runtime privileges to.
    app_db_role: str = Field("asset_shells_app", json_schema_extra={"source": "env"})

    # The shared Postgres serves the whole fleet (100 connections): keep the pool small.
    db_pool_min: int = Field(1, json_schema_extra={"source": "env"})
    db_pool_max: int = Field(4, json_schema_extra={"source": "env"})
    db_statement_timeout_ms: int = Field(15000, json_schema_extra={"source": "env"})
    db_startup_retry_seconds: float = Field(30.0, json_schema_extra={"source": "env"})

    # Publish limits.
    max_publish_bytes: int = Field(32 * 1024 * 1024, json_schema_extra={"source": "env"})
    max_page_limit: int = Field(1000, json_schema_extra={"source": "env"})
    default_page_limit: int = Field(100, json_schema_extra={"source": "env"})

    log_level: str = Field("INFO", json_schema_extra={"source": "env"})

    @property
    def is_local(self) -> bool:
        return self.asset_shells_env in LOCAL_ENVIRONMENTS

    @property
    def effective_jwks_url(self) -> str:
        return self.janua_jwks_url or f"{self.janua_issuer.rstrip('/')}/.well-known/jwks.json"

    def validate_runtime(self) -> None:
        if self.jwks_path and not self.is_local:
            raise RuntimeError("JWKS_PATH is honoured only when ASSET_SHELLS_ENV is local or test")
        if self.db_pool_max < 1 or self.db_pool_min < 0 or self.db_pool_min > self.db_pool_max:
            raise RuntimeError("DB_POOL_MIN/DB_POOL_MAX are inconsistent")
        if self.db_pool_max > 10:
            raise RuntimeError("DB_POOL_MAX above 10 breaks the fleet connection budget; change it deliberately")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.validate_runtime()
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
