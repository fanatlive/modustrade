"""Consistent PostgreSQL dumps and isolated restore drills; no Django dependency.

Default transport: Docker Compose db service. BACKUP_LOCAL_PG=1 uses local PG
clients (for the dedicated backup container). Never restores into an existing DB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Postgres:
    def __init__(self, args):
        self.args = args
        self.env = os.environ.copy()
        env_path = Path(args.env_file)
        if env_path.exists():
            for line in env_path.read_text(encoding='utf-8-sig').splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    self.env.setdefault(key.strip(), value.strip().strip('"\''))
        self.user = self.env.get('POSTGRES_USER', 'modus')
        self.database = self.env.get('POSTGRES_DB', 'modus')

    def command(self, program: str, *args: str) -> list[str]:
        prefix = [] if self.env.get('BACKUP_LOCAL_PG') == '1' else [
            'docker', 'compose', '--env-file', self.args.env_file, '-f', self.args.compose_file, 'exec', '-T', 'db']
        return prefix + [program, '-U', self.user, *args]

    def sql(self, query: str, database: str | None = None) -> str:
        result = subprocess.run(self.command('psql', '-XqAt', '-v', 'ON_ERROR_STOP=1', '-d', database or self.database),
                                input=query, capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=120)
        if result.returncode:
            raise RuntimeError('PostgreSQL command failed: '+result.stderr[-2000:])
        return result.stdout.strip()


TABLE_HASHES = """SELECT json_object_agg(name, signature) FROM (
  SELECT c.relname AS name, (xpath('/row/s/text()', query_to_xml(
    format('SELECT count(*)::text || '':'' || coalesce(sum(hashtextextended(row_to_json(t)::text,0)::numeric),0)::text AS s FROM public.%I t', c.relname),
    false, true, '')))[1]::text AS signature
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r'
) hashes;"""


def backup(pg: Postgres, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    size = int(pg.sql('SELECT pg_database_size(current_database());'))
    if shutil.disk_usage(folder).free < max(512*1024**2, size*2):
        raise RuntimeError('Insufficient free space for a safe backup; existing files were kept.')
    name = 'modus-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    target, partial = folder/(name+'.dump'), folder/(name+'.partial')
    snapshot_epoch = time.time()
    session = subprocess.Popen(pg.command('psql', '-XqAt', '-v', 'ON_ERROR_STOP=1', '-d', pg.database),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', env=pg.env)
    try:
        session.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ; SET TIME ZONE 'UTC'; SELECT pg_export_snapshot();\n")
        session.stdin.flush()
        snapshot = session.stdout.readline().strip()
        if not re.fullmatch(r'[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9A-Fa-f]+', snapshot):
            raise RuntimeError('Could not acquire a consistent PostgreSQL snapshot.')
        session.stdin.write(TABLE_HASHES+'\n')
        session.stdin.flush()
        tables = json.loads(session.stdout.readline())
        started = time.monotonic()
        with partial.open('xb') as output:
            result = subprocess.run(pg.command('pg_dump', '-d', pg.database, '-Fc', '--snapshot='+snapshot),
                                    stdout=output, stderr=subprocess.PIPE, env=pg.env, timeout=1800)
        if result.returncode:
            raise RuntimeError('pg_dump failed: '+result.stderr.decode('utf-8', errors='replace')[-2000:])
        partial.replace(target)
        manifest = {'format': 1, 'database': pg.database,
                    'captured_at': datetime.fromtimestamp(snapshot_epoch, timezone.utc).isoformat(),
                    'snapshot_epoch': snapshot_epoch, 'completed_at': datetime.now(timezone.utc).isoformat(),
                    'sha256': digest(target), 'tables': tables, 'database_bytes': size,
                    'archive_bytes': target.stat().st_size,
                    'dump_seconds': round(time.monotonic()-started, 3), 'postgres_major': 17}
        temporary_manifest = target.with_suffix('.json.partial')
        temporary_manifest.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        temporary_manifest.replace(target.with_suffix('.json'))
        return target
    finally:
        if session.poll() is None:
            session.stdin.write('ROLLBACK;\n\\q\n')
            session.stdin.flush()
            try:
                session.wait(timeout=10)
            except subprocess.TimeoutExpired:
                session.kill()
                session.wait()
        if partial.exists():
            partial.unlink()


def restore_verify(pg: Postgres, archive: Path) -> dict:
    manifest = json.loads(archive.with_suffix('.json').read_text(encoding='utf-8'))
    if digest(archive) != manifest['sha256']:
        raise RuntimeError('Archive checksum mismatch. No database was created.')
    if shutil.disk_usage(archive.parent).free < max(512*1024**2, int(manifest['database_bytes'])*2):
        raise RuntimeError('Insufficient free space for isolated restoration. No database was created.')
    scratch = 'modus_verify_'+uuid.uuid4().hex
    started = time.monotonic()
    # Generated identifier only. Never accept the restore target from a caller.
    pg.sql('CREATE DATABASE '+scratch+';', 'postgres')
    try:
        with archive.open('rb') as source:
            result = subprocess.run(pg.command('pg_restore', '-d', scratch, '--no-owner', '--no-acl', '--exit-on-error'),
                                    stdin=source, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    env=pg.env, timeout=1800)
        if result.returncode:
            raise RuntimeError('pg_restore failed: '+result.stderr.decode('utf-8', errors='replace')[-2000:])
        actual = json.loads(pg.sql("SET TIME ZONE 'UTC'; "+TABLE_HASHES, scratch))
        if actual != manifest['tables']:
            raise RuntimeError('Restored table counts/checksums differ from the exported snapshot.')
        if pg.sql("SELECT count(*) FROM pg_constraint WHERE NOT convalidated;", scratch) != '0':
            raise RuntimeError('Unvalidated database constraints found.')
        return {'verified': True, 'tables': len(actual), 'restore_seconds': round(time.monotonic()-started, 3),
                'sha256': manifest['sha256']}
    finally:
        if re.fullmatch(r'modus_verify_[0-9a-f]{32}', scratch):
            pg.sql('DROP DATABASE '+scratch+' WITH (FORCE);', 'postgres')


def prune_owned(folder: Path, keep: int, days: int, max_bytes: int) -> None:
    """Only complete archives produced by this tool inside the selected folder."""
    archives = []
    for archive in folder.glob('modus-*.dump'):
        if not re.fullmatch(r'modus-\d{8}T\d{6}Z-[0-9a-f]{8}\.dump', archive.name) or archive.is_symlink():
            continue
        try:
            manifest = json.loads(archive.with_suffix('.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict):
                continue
            if manifest.get('sha256') == digest(archive):
                archives.append(archive)
        except (OSError, ValueError, TypeError):
            continue
    archives.sort(key=lambda p: p.name, reverse=True)
    total = 0
    for index, archive in enumerate(archives):
        total += archive.stat().st_size
        if index and (index >= keep or total > max_bytes or time.time()-archive.stat().st_mtime > days*86400):
            companion = archive.with_suffix('.json')
            try:
                value = json.loads(companion.read_text(encoding='utf-8')) if companion.exists() else None
                valid = isinstance(value, dict) and value.get('sha256') == digest(archive)
            except (OSError, ValueError, TypeError):
                valid = False
            if valid:
                archive.unlink()
                companion.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'restore-verify', 'loop', 'health'])
    parser.add_argument('--directory', default='.local/backups')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--compose-file', default='compose.yaml')
    parser.add_argument('--interval', type=int, default=600)
    parser.add_argument('--keep', type=int, default=2016)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--max-bytes', type=int, default=1073741824)
    args = parser.parse_args()
    folder = Path(args.directory).resolve()
    if args.action == 'health':
        state = json.loads((folder/'backup-status.json').read_text(encoding='utf-8'))
        if time.time()-state['last_snapshot_at'] > 900:
            raise SystemExit('Backup is stale.')
        name = state.get('archive', '')
        if not re.fullmatch(r'modus-\d{8}T\d{6}Z-[0-9a-f]{8}\.dump', name):
            raise SystemExit('No complete backup archive.')
        archive = folder/name
        if not archive.is_file() or archive.is_symlink():
            raise SystemExit('Backup archive is missing.')
        manifest = json.loads(archive.with_suffix('.json').read_text(encoding='utf-8'))
        if (manifest.get('snapshot_epoch') != state['last_snapshot_at']
                or manifest.get('archive_bytes') != archive.stat().st_size
                or not re.fullmatch(r'[0-9a-f]{64}', manifest.get('sha256', ''))):
            raise SystemExit('Backup metadata is inconsistent.')
        print('Backup is fresh.')
        return
    if args.interval < 60 or args.keep < 1 or args.days < 1 or args.max_bytes < 1:
        raise SystemExit('Invalid backup interval/retention.')
    pg = Postgres(args)
    if args.action == 'restore-verify':
        if not args.archive:
            parser.error('--archive is required')
        print(json.dumps(restore_verify(pg, args.archive.resolve())))
        return
    while True:
        cycle_start = time.monotonic()
        try:
            target = backup(pg, folder)
            prune_owned(folder, args.keep, args.days, args.max_bytes)
            manifest = json.loads(target.with_suffix('.json').read_text(encoding='utf-8'))
            status = {'last_snapshot_at': manifest['snapshot_epoch'], 'last_success': time.time(), 'archive': target.name}
            temporary = folder/'backup-status.partial'
            temporary.write_text(json.dumps(status), encoding='utf-8')
            temporary.replace(folder/'backup-status.json')
            print(json.dumps(status), flush=True)
        except Exception as error:
            if args.action != 'loop':
                raise
            print(json.dumps({'backup_error': str(error)}), flush=True)
        if args.action != 'loop':
            break
        time.sleep(max(1, args.interval-(time.monotonic()-cycle_start)))


if __name__ == '__main__':
    main()
