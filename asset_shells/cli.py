"""Command line: ``migrate`` (init container, owner credentials) and ``dev-token`` (local only).

``asset-shells migrate`` runs ``alembic upgrade head`` with DATABASE_URL (the schema owner) and grants
the runtime privileges to APP_DB_ROLE. ``asset-shells dev-token`` writes an RSA key pair and a JWKS
file into a directory and prints an RS256 token signed with it — for local development against
``JWKS_PATH`` (honoured only when ASSET_SHELLS_ENV is local or test). It never talks to Janua.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from importlib import resources
from pathlib import Path


def migrate(database_url: str | None = None, app_role: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(str(resources.files("asset_shells"))).parent
    ini = root / "alembic.ini"
    if not ini.exists():
        raise SystemExit("alembic.ini not found next to the package")
    cfg = Config(str(ini))
    if database_url:
        cfg.attributes["database_url"] = database_url
    if app_role:
        cfg.attributes["app_db_role"] = app_role
    command.upgrade(cfg, "head")


def _keypair(directory: Path) -> tuple[object, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key_file = directory / "dev-signing-key.pem"
    if key_file.exists():
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        key_file.chmod(0o600)
    return key, "dev-key-1"


def dev_token(directory: str, scopes: list[str], tenant: str | None, issuer: str, audience: str) -> str:
    import jwt
    from jwt.algorithms import RSAAlgorithm

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    key, kid = _keypair(path)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    (path / "jwks.json").write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    now = dt.datetime.now(dt.UTC)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": f"service-account:dev-{uuid.uuid4().hex[:8]}",
        "iat": now,
        "exp": now + dt.timedelta(hours=1),
        "scope": " ".join(scopes),
        "token_use": "client_credentials",
    }
    if tenant:
        claims["tenant_id"] = tenant
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asset-shells")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head (DATABASE_URL = schema owner)")
    tok = sub.add_parser("dev-token", help="local-only RS256 token + JWKS file")
    tok.add_argument("--dir", default=".dev-keys")
    tok.add_argument("--scope", action="append", default=[])
    tok.add_argument("--tenant")
    tok.add_argument("--issuer", default="https://auth.madfam.io")
    tok.add_argument("--audience", default="asset-shells-api")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0
    print(dev_token(args.dir, args.scope, args.tenant, args.issuer, args.audience))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
