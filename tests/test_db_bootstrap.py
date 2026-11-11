"""Exercise the actual provisioning entry point against disposable roles."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL role semantics required')
class DatabaseBootstrapTests(TransactionTestCase):
    def setUp(self):
        self.role = 'modus_test_runtime_'+uuid4().hex
        params = connection.settings_dict
        self.env = {**os.environ, 'DEBUG': '1', 'POSTGRES_DB': params['NAME'],
                    'POSTGRES_HOST': params['HOST'], 'POSTGRES_PORT': str(params['PORT']),
                    'POSTGRES_USER': params['USER'], 'POSTGRES_PASSWORD': params['PASSWORD'],
                    'APP_DB_USER': self.role, 'APP_DB_PASSWORD': secrets.token_urlsafe(32)}

    def tearDown(self):
        # Identifier is generated here, never user input. Only this role is owned.
        with connection.cursor() as cursor:
            cursor.execute('DROP OWNED BY '+self.role) if self.role_exists() else None
            cursor.execute('DROP ROLE IF EXISTS '+self.role)

    def role_exists(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', [self.role])
            return bool(cursor.fetchone())

    def provision(self):
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'ops/bootstrap_db.py')],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_new_role_is_restricted_and_repeat_is_safe(self):
        self.assertEqual(self.provision().returncode, 0)
        self.assertEqual(self.provision().returncode, 0)
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,has_schema_privilege(rolname,'public','CREATE') FROM pg_roles WHERE rolname=%s", [self.role])
            self.assertFalse(any(cursor.fetchone()))
            cursor.execute("SELECT has_table_privilege(%s,'feedback_ticket','SELECT,INSERT,UPDATE,DELETE')", [self.role])
            self.assertTrue(cursor.fetchone()[0])

    def test_existing_privileged_membership_is_rejected(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE '+self.role+' LOGIN')
            cursor.execute('GRANT pg_read_all_data TO '+self.role)
        self.assertNotEqual(self.provision().returncode, 0)

    def test_existing_bypass_rls_is_rejected(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE '+self.role+' LOGIN BYPASSRLS')
        self.assertNotEqual(self.provision().returncode, 0)

    def test_existing_schema_create_is_rejected(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE '+self.role+' LOGIN')
            cursor.execute('GRANT CREATE ON SCHEMA public TO '+self.role)
        self.assertNotEqual(self.provision().returncode, 0)

    def test_existing_object_owner_is_rejected(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE '+self.role+' LOGIN')
            cursor.execute('CREATE TABLE '+self.role+' (id int)')
            cursor.execute('ALTER TABLE '+self.role+' OWNER TO '+self.role)
        self.assertNotEqual(self.provision().returncode, 0)

    def test_production_rejects_placeholder_owner_secret_before_connect(self):
        self.env.update(DEBUG='0', POSTGRES_PASSWORD='CHANGE_ME_OWNER_PASSWORD', POSTGRES_HOST='unreachable.invalid')
        result = self.provision()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Production POSTGRES_PASSWORD', result.stderr)

    def test_existing_ownership_in_another_database_is_rejected(self):
        import psycopg
        from psycopg import sql
        other = 'modus_test_other_'+uuid4().hex
        params = connection.settings_dict
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE '+self.role+' LOGIN')
            cursor.execute('CREATE DATABASE '+other)
        try:
            with psycopg.connect(dbname=other, user=params['USER'], password=params['PASSWORD'],
                                 host=params['HOST'], port=params['PORT'], autocommit=True) as conn:
                conn.execute('CREATE TABLE ownership_probe (id int)')
                conn.execute(sql.SQL('ALTER TABLE ownership_probe OWNER TO {}').format(sql.Identifier(self.role)))
            result = self.provision()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('owns objects', result.stderr)
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP DATABASE '+other+' WITH (FORCE)')
