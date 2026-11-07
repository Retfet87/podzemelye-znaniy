import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def run_backup(self, directory, *, rotate='0', fail=False):
        directory=Path(directory)
        tools=directory/'bin';tools.mkdir(exist_ok=True)
        for name,script in {
            'pg_dump':'#!/bin/sh\n[ "${FAIL_DUMP:-0}" = 0 ] || exit 1\nfor arg; do case "$arg" in --file=*) printf "verified-test-archive" > "${arg#--file=}";; esac; done\n',
            'pg_restore':'#!/bin/sh\ngrep -q verified-test-archive "$2"\n',
        }.items():
            path=tools/name;path.write_text(script);path.chmod(0o700)
        env={'PATH':str(tools)+os.pathsep+os.defpath,'BACKUP_DIR':str(directory/'archives'),
             'BACKUP_SCRIPT':str(ROOT/'scripts/backup.sh'),'BACKUP_RUN_ONCE':'1',
             'BACKUP_ROTATION_ENABLED':rotate,'FAIL_DUMP':'1' if fail else '0'}
        return subprocess.run(['sh',str(ROOT/'scripts/backup_daily.sh')],env=env,capture_output=True,text=True)

    def test_daily_archive_atomic_validation_failure_preserves_old_and_rotation_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'archives';root.mkdir()
            old=root/'old-existing.dump';old.write_bytes(b'old archive never managed')
            for i in range(9):self.assertEqual(self.run_backup(tmp).returncode,0)
            managed=root/'scheduled-v1'
            self.assertEqual(len(list(managed.glob('*.dump'))),9)
            result=self.run_backup(tmp,rotate='1',fail=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(len(list(managed.glob('*.dump'))),9)
            self.assertFalse(list(managed.glob('.incomplete.*')))
            self.assertEqual(self.run_backup(tmp,rotate='1').returncode,0)
            self.assertEqual(len(list(managed.glob('*.dump'))),7)
            self.assertEqual(old.read_bytes(),b'old archive never managed')

    def test_unknown_directory_and_stale_lock_never_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'archives/scheduled-v1';root.mkdir(parents=True)
            existing=root/'backup_2026-01-01_00-00-00_ABCDEF.dump';existing.write_bytes(b'untouched')
            self.assertNotEqual(self.run_backup(tmp,rotate='1').returncode,0)
            self.assertTrue(existing.exists())
            (root/'.managed').write_text('podzemelye-scheduled-v1\n');(root/'.lock').mkdir()
            self.assertNotEqual(self.run_backup(tmp).returncode,0)
            self.assertTrue((root/'.lock').exists())
