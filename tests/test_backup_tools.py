import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import subprocess
import sys
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from ops.backup import digest, prune_owned, restore_verify


class BackupToolTests(TestCase):
    def test_health_uses_snapshot_age_even_if_completion_is_recent(self):
        with TemporaryDirectory() as root:
            (Path(root)/'backup-status.json').write_text(json.dumps({'last_success': time.time(), 'last_snapshot_at': time.time()-1200}))
            result = subprocess.run([sys.executable, 'ops/backup.py', 'health', '--directory', root], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'stale', result.stderr)

    def test_health_rejects_fresh_status_when_archive_is_missing(self):
        with TemporaryDirectory() as root:
            (Path(root)/'backup-status.json').write_text(json.dumps({'last_snapshot_at': time.time(), 'archive': 'modus-20261111T000000Z-aabbccdd.dump'}))
            result = subprocess.run([sys.executable, 'ops/backup.py', 'health', '--directory', root], capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_orphans_do_not_displace_the_last_complete_archive(self):
        with TemporaryDirectory() as root:
            folder = Path(root)
            complete = folder/'modus-20261111T000000Z-aabbccdd.dump'
            complete.write_bytes(b'valid')
            complete.with_suffix('.json').write_text(json.dumps({'sha256': digest(complete)}))
            orphan = folder/'modus-20261112T000000Z-aabbccdd.dump'
            orphan.write_bytes(b'orphan')
            invalid = folder/'modus-20261113T000000Z-aabbccdd.dump'
            invalid.write_bytes(b'invalid manifest')
            invalid.with_suffix('.json').write_text('[]')
            prune_owned(folder, keep=1, days=1, max_bytes=1)
            self.assertTrue(complete.exists())
            self.assertTrue(orphan.exists())
            self.assertTrue(invalid.exists())

    def test_invalid_companion_does_not_interrupt_retention_or_delete_unknown_files(self):
        with TemporaryDirectory() as root:
            folder = Path(root)
            for index in range(3):
                archive = folder/f'modus-20261111T00000{index}Z-aabbccdd.dump'
                archive.write_bytes(b'archive')
                archive.with_suffix('.json').write_text(json.dumps({'sha256': digest(archive)}))
            damaged = folder/'modus-20261110T000000Z-aabbccdd.dump'
            damaged.write_bytes(b'incomplete')
            damaged.with_suffix('.json').write_text('{broken')
            foreign = folder/'pre-remediation.dump'
            foreign.write_bytes(b'never remove')
            prune_owned(folder, keep=1, days=14, max_bytes=1024)
            self.assertTrue((folder/'modus-20261111T000002Z-aabbccdd.dump').exists())
            self.assertFalse((folder/'modus-20261111T000001Z-aabbccdd.dump').exists())
            self.assertTrue(damaged.exists())
            self.assertTrue(foreign.exists())

    def test_restore_rejects_corrupt_archive_before_creating_database(self):
        with TemporaryDirectory() as root:
            archive = Path(root)/'copy.dump'
            archive.write_bytes(b'changed')
            archive.with_suffix('.json').write_text(json.dumps({'sha256': 'wrong'}))
            pg = Mock()
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                restore_verify(pg, archive)
            pg.sql.assert_not_called()

    def test_restore_rejects_insufficient_space_before_creating_database(self):
        with TemporaryDirectory() as root:
            archive = Path(root)/'copy.dump'
            archive.write_bytes(b'archive')
            archive.with_suffix('.json').write_text(json.dumps({'sha256': digest(archive), 'database_bytes': 1000}))
            pg = Mock()
            with patch('ops.backup.shutil.disk_usage', return_value=SimpleNamespace(free=100)), self.assertRaisesRegex(RuntimeError, 'space'):
                restore_verify(pg, archive)
            pg.sql.assert_not_called()
