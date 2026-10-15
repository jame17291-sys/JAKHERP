"""Offline package/source integrity checks; do not contact any package registry."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_rtlcss as compiler


class CompilerBundleTests(unittest.TestCase):
    def test_every_packed_file_has_original_or_mechanical_build_provenance(self):
        result = compiler.verify_bundle()
        self.assertEqual({item['name']: item['version'] for item in result}, compiler.VERSIONS)
        self.assertGreater(sum(item['files'] for item in result), 80)

    def test_modified_tarball_is_rejected_before_npm(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'bundle'
            shutil.copytree(compiler.BUNDLE, bundle, ignore=shutil.ignore_patterns('node_modules'))
            archive = bundle / 'vendor/rtlcss-4.3.0.tgz'
            archive.write_bytes(archive.read_bytes() + b'altered')
            with self.assertRaisesRegex(ValueError, 'integrity failure'):
                compiler.verify_bundle(bundle)

    def test_unreviewed_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'bundle'
            shutil.copytree(compiler.BUNDLE, bundle, ignore=shutil.ignore_patterns('node_modules'))
            lock_path = bundle / 'package-lock.json'
            lock = json.loads(lock_path.read_text())
            lock['packages']['node_modules/unreviewed'] = {'version': '1.0.0'}
            lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, 'Unexpected packages'):
                compiler.verify_bundle(bundle)

    def test_installer_uses_canonical_new_project_and_offline_script_free_commands(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(compiler.subprocess, 'run') as run:
            destination = Path(temporary) / 'compiler'
            result = compiler.install(destination)
            self.assertEqual(result, destination.resolve() / 'node_modules/.bin')
            invocation = run.call_args_list[0]
            self.assertIn('--offline', invocation.args[0])
            self.assertIn('--ignore-scripts', invocation.args[0])
            self.assertEqual(invocation.kwargs['cwd'], destination.resolve())
            with self.assertRaisesRegex(ValueError, 'must be NEW'):
                compiler.install(destination)


if __name__ == '__main__':
    unittest.main()
