"""Provision a DML-only application role after migrations, without exposing secrets."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql


def secret(name: str) -> str:
    file = os.getenv(name+'_FILE')
    return Path(file).read_text(encoding='utf-8').strip() if file else os.environ.get(name, '')


def main() -> None:
    owner = os.environ['POSTGRES_USER']
    runtime = os.environ.get('APP_DB_USER', 'modus_runtime')
    password = secret('APP_DB_PASSWORD')
    if runtime == owner or not password:
        raise SystemExit('A distinct APP_DB_USER and APP_DB_PASSWORD are required.')
    if os.getenv('DEBUG', '1') == '0' and (len(password) < 24 or password.startswith(('CHANGE_ME', 'change-'))):
        raise SystemExit('Production APP_DB_PASSWORD must be a private random value of at least 24 characters.')
    owner_password = secret('POSTGRES_PASSWORD')
    if os.getenv('DEBUG', '1') == '0' and (len(owner_password) < 24 or owner_password.startswith(('CHANGE_ME', 'change-'))):
        raise SystemExit('Production POSTGRES_PASSWORD must be a private random value of at least 24 characters.')
    database = os.environ['POSTGRES_DB']
    with psycopg.connect(host=os.getenv('POSTGRES_HOST', 'db'), port=os.getenv('POSTGRES_PORT', '5432'),
                         dbname=database, user=owner, password=owner_password, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT oid,rolbypassrls FROM pg_roles WHERE rolname=%s', [runtime])
            existing = cur.fetchone()
            if existing:
                cur.execute("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s) OR EXISTS(SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=%s AND deptype='o')", [existing[0]]*2)
                if cur.fetchone()[0] or existing[1]:
                    raise SystemExit('Existing APP_DB_USER owns objects, inherits roles or bypasses RLS. Use a dedicated unprivileged role.')
                cur.execute("SELECT has_database_privilege(%s,%s,'CREATE') OR has_schema_privilege(%s,'public','CREATE')", [runtime,database,runtime])
                if cur.fetchone()[0]:
                    raise SystemExit('Existing APP_DB_USER has CREATE privileges. Use a dedicated unprivileged role.')
            if not existing:
                cur.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Identifier(runtime),sql.Literal(password)))
            else:
                cur.execute(sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(sql.Identifier(runtime),sql.Literal(password)))
            cur.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database),sql.Identifier(runtime)))
            cur.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(runtime)))
            cur.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(runtime)))
            cur.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(runtime)))
            cur.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {}').format(sql.Identifier(owner),sql.Identifier(runtime)))
            cur.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {}').format(sql.Identifier(owner),sql.Identifier(runtime)))
    print('Application role provisioned with DML-only privileges.')


if __name__ == '__main__':
    main()
