"""Provision the restricted, RLS-subject application DB role (``brain_app``).

Why this exists
---------------
The app must NOT connect to Postgres as a ``BYPASSRLS`` role (e.g. Supabase's
``postgres``), or Row-Level Security never filters and every tenant-scoped query
that trusts RLS leaks across workspaces. This script creates a dedicated login
role that is **subject to** RLS and grants it the DML it needs. The app then uses
this role for all tenant-scoped traffic (``tenant_session`` / ``run_in_tenant``)
while keeping the privileged role only for pre-tenant work (auth, oauth_states).

Idempotent: safe to re-run. Rotates the password each run and rewrites
``TENANT_DATABASE_URL`` in ``brain-api/.env``. The password is never printed.

Run: ``cd brain-api && .venv/bin/python scripts/provision_tenant_role.py``
"""
from __future__ import annotations

import asyncio
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ROLE_NAME = "brain_app"


def _read_env(key: str) -> str | None:
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def _upsert_env(key: str, value: str) -> None:
    """Write or replace ``key=value`` in brain-api/.env (value never logged)."""
    lines = ENV_PATH.read_text().splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def _tenant_url_from_admin(admin_url: str, password: str) -> str:
    """Build the brain_app DSN from the admin URL, swapping user + password.

    Preserves the +asyncpg driver marker, host, port, and dbname. For the
    Supabase pooler the username carries the project ref (``role.<ref>``); we
    keep that ref and swap the role prefix.
    """
    parts = urlsplit(admin_url)
    admin_user = parts.username or "postgres"
    # Pooler tenant format: "<role>.<project_ref>" — keep the ref, swap the role.
    ref = admin_user.split(".", 1)[1] if "." in admin_user else None
    new_user = f"{ROLE_NAME}.{ref}" if ref else ROLE_NAME
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    dbname = parts.path or "/postgres"
    scheme = parts.scheme  # e.g. postgresql+asyncpg
    return f"{scheme}://{new_user}:{password}@{host}{port}{dbname}"


async def main() -> int:
    admin_url = _read_env("DATABASE_URL")
    if not admin_url:
        print("ERROR: DATABASE_URL not found in brain-api/.env", file=sys.stderr)
        return 1
    # asyncpg wants a plain DSN (no +asyncpg driver marker).
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", admin_url)

    password = secrets.token_urlsafe(24)  # URL-safe: no @ : / in the DSN

    conn = await asyncpg.connect(dsn)
    try:
        # 1. Create or update the role (LOGIN, subject to RLS, minimal attributes).
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE_NAME
        )
        if exists:
            await conn.execute(
                f"ALTER ROLE {ROLE_NAME} LOGIN NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOBYPASSRLS PASSWORD '{password}'"
            )
        else:
            await conn.execute(
                f"CREATE ROLE {ROLE_NAME} LOGIN NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOBYPASSRLS PASSWORD '{password}'"
            )

        # 2. Grants: schema usage + DML on all current tables/sequences/functions.
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE_NAME}")
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA public TO {ROLE_NAME}"
        )
        await conn.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE_NAME}"
        )
        await conn.execute(
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {ROLE_NAME}"
        )

        # 3. Default privileges so future migrations' objects are covered.
        #    Bound to the creating role (postgres runs alembic).
        admin_user = urlsplit(admin_url).username or "postgres"
        creator = admin_user.split(".", 1)[0]  # strip pooler ref
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE_NAME}"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {ROLE_NAME}"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {creator} IN SCHEMA public "
            f"GRANT EXECUTE ON FUNCTIONS TO {ROLE_NAME}"
        )

        # 4. Confirm the role is genuinely subject to RLS.
        attrs = await conn.fetchrow(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = $1",
            ROLE_NAME,
        )
        assert attrs and not attrs["rolbypassrls"] and not attrs["rolsuper"], (
            "brain_app must not have BYPASSRLS/SUPERUSER"
        )
    finally:
        await conn.close()

    tenant_url = _tenant_url_from_admin(admin_url, password)
    _upsert_env("TENANT_DATABASE_URL", tenant_url)

    # 5. Self-test: connect AS brain_app and verify RLS now filters. Uses the
    #    pooler host, so a lag in Supavisor picking up the role would surface here.
    test_dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", tenant_url)
    tconn = await asyncpg.connect(test_dsn)
    try:
        who = await tconn.fetchrow(
            "SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        # Bogus workspace GUC -> RLS should hide every source_connection.
        await tconn.execute(
            "SELECT set_config('app.current_workspace_id','__none__',false),"
            "set_config('app.current_user_id','__none__',false),"
            "set_config('app.current_role','admin',false)"
        )
        leaked = await tconn.fetchval("SELECT count(*) FROM source_connections")
    finally:
        await tconn.close()

    print(f"role={who['current_user']} bypassrls={who['rolbypassrls']}")
    print(f"source_connections visible under bogus workspace GUC: {leaked}")
    if leaked != 0:
        print("FAIL: RLS still not filtering for brain_app", file=sys.stderr)
        return 2
    print("OK: brain_app is subject to RLS; TENANT_DATABASE_URL written to .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
