"""Destination verification contract, using only synthetic temporary files."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
RUN_ID = 'fie-offhost-20261024T000000Z'


class BackupVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fie-verifier-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'runs'
        self.run = self.base / RUN_ID
        self.run.mkdir(parents=True)
        self.sample = self.run / 'sample.dump.enc'
        self.sample.write_bytes(b'synthetic ciphertext')
        self.outside = self.base / 'outside.enc'
        self.outside.write_bytes(b'synthetic outside ciphertext')
        source = (REPO / 'deploy/backup/a3-dest-verify.sh').read_text()
        self.assertEqual(source.count('BASE=/srv/fie-backup'), 1)
        self.script = self.root / 'verify.sh'
        self.script.write_text(source.replace('BASE=/srv/fie-backup', f'BASE={self.base}'))

    def manifest(self, name='sample.dump.enc', digest=None, newline='\n'):
        digest = digest or hashlib.sha256(self.sample.read_bytes()).hexdigest()
        (self.run / 'SHA256SUMS').write_text(f'{digest}  {name}{newline}')

    def verify(self, success=False, command=RUN_ID):
        result = subprocess.run(['sh', str(self.script)], capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin', 'SSH_ORIGINAL_COMMAND': command})
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        self.assertEqual('DEST-VERIFY-OK:' in result.stdout, success, result.stdout)
        return result

    def test_valid_manifest(self):
        self.manifest()
        result = self.verify(success=True)
        self.assertIn(hashlib.sha256(self.sample.read_bytes()).hexdigest(), result.stdout)

    def test_valid_manifest_without_final_newline(self):
        self.manifest(newline='')
        result = self.verify(success=True)
        self.assertIn(hashlib.sha256(self.sample.read_bytes()).hexdigest(), result.stdout)

    def test_hash_mismatch(self):
        self.manifest(digest='0' * 64)
        self.verify()

    def test_empty_manifest(self):
        (self.run / 'SHA256SUMS').touch()
        self.verify()

    def test_whitespace_manifest(self):
        (self.run / 'SHA256SUMS').write_text(' \t\n')
        self.verify()

    def test_parent_traversal(self):
        self.manifest('../outside.enc', hashlib.sha256(self.outside.read_bytes()).hexdigest())
        self.verify()

    def test_absolute_path(self):
        self.manifest(str(self.outside), hashlib.sha256(self.outside.read_bytes()).hexdigest())
        self.verify()

    def test_nested_path(self):
        (self.run / 'nested').mkdir()
        (self.run / 'nested' / 'sample.enc').write_bytes(self.sample.read_bytes())
        self.manifest('nested/sample.enc')
        self.verify()

    def test_outside_artifact_symlink(self):
        self.sample.unlink()
        self.sample.symlink_to(self.outside)
        self.manifest(digest=hashlib.sha256(self.outside.read_bytes()).hexdigest())
        self.verify()

    def test_inside_artifact_symlink(self):
        (self.run / 'alias.enc').symlink_to(self.sample)
        self.manifest('alias.enc')
        self.verify()

    def test_dangling_artifact_symlink(self):
        self.sample.unlink()
        self.sample.symlink_to(self.root / 'missing')
        self.manifest(digest='0' * 64)
        self.verify()

    def test_run_directory_symlink(self):
        self.manifest()
        moved = self.root / 'outside-run'
        self.run.rename(moved)
        self.run.symlink_to(moved, target_is_directory=True)
        self.verify()

    def test_manifest_symlink(self):
        self.manifest()
        moved = self.root / 'outside-manifest'
        (self.run / 'SHA256SUMS').rename(moved)
        (self.run / 'SHA256SUMS').symlink_to(moved)
        self.verify()

    def test_malformed_hash(self):
        self.manifest(digest='invalid')
        self.verify()

    def test_missing_artifact(self):
        self.manifest('missing.enc')
        self.verify()

    def test_option_shaped_name(self):
        (self.run / '-sample.enc').write_bytes(self.sample.read_bytes())
        self.manifest('-sample.enc')
        self.verify()

    def test_bad_run_argument(self):
        self.manifest()
        self.verify(command='../' + RUN_ID)


if __name__ == '__main__':
    unittest.main()
