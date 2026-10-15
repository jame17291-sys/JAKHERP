"""Synthetic, local-only publication guards. No project credentials or data."""
import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_publish_tree.py'
SPEC = importlib.util.spec_from_file_location('jakh_publish_tree', SCRIPT)
publish = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publish
SPEC.loader.exec_module(publish)


class TestPublicationCandidate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='jakh-publish-synthetic-')
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def put(self, relative, value=b'# Synthetic source\n'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        return path

    def categories(self, report):
        return {issue['category'] for issue in report['issues']}

    def git(self, *args):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.root), *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1'})

    def test_clean_source_allowlist_preserves_native_addon_declarations(self):
        for name in ['README.md', 'AGENTS.md', 'LICENSE', '.gitignore', 'vendor.lock.json',
                     'scripts/check.py', 'addons/jakh_core/data/groups.xml',
                     'addons/jakh_core/security/ir.access.csv', 'deploy/RELOCATION.md']:
            self.put(name)
        self.put('.github/workflows/quality.yml', 'name: QA\npermissions:\n  contents: read\njobs: {}\n')
        report = publish.inspect(self.root, mode='tree')
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['checked_files'], 10)
        self.assertFalse(report['history_checked'])

    def test_private_and_runtime_names_rejected_before_content_read(self):
        bad = ['secrets/owner', '.env', 'deploy/.env.local', 'runtime/customer.conf',
               'deploy/pgdata/PG_VERSION', 'tests/__pycache__/test.pyc',
               'deploy/rtlcss/node_modules/thing/index.js', 'data/customer.csv',
               'deploy/logs/server.log', 'deploy/authkeys/session.json', 'deploy/backup.dump',
               'deploy/key.pem', 'deploy/.npmrc', 'unknown/source.py']
        for path in bad:
            self.assertIsNotNone(publish.path_rejection(path), path)
        self.put('README.md')
        self.put('secrets/never-open-this')
        original = publish._read_source
        read = []
        def guarded(root, relative):
            self.assertNotIn('secrets', relative.parts)
            read.append(str(relative))
            return original(root, relative)
        with patch.object(publish, '_read_source', guarded):
            report = publish.inspect(self.root, mode='tree')
        self.assertFalse(report['ok'])
        self.assertEqual(read, ['README.md'])
        self.assertIn('excluded-runtime-or-private-path', self.categories(report))

    def test_unsafe_relative_paths(self):
        for path in ['', '../README.md', '/tmp/README.md', 'scripts/../README.md',
                     'scripts\\file.py', 'scripts//file.py', 'scripts/./file.py', 'scripts/new\nline.py']:
            self.assertEqual(publish.path_rejection(path), 'unsafe-path', path)

    def test_links_are_never_followed_even_inside_candidate(self):
        self.put('README.md')
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts' / 'linked.py').symlink_to(self.root / 'README.md')
        report = publish.inspect(self.root, mode='tree')
        self.assertFalse(report['ok'])
        self.assertIn('symlink-submodule-or-unmerged-entry', self.categories(report))
        with self.assertRaises(OSError):
            publish._read_source(self.root, PurePosixPath('scripts/linked.py'))

    def test_linked_parent_and_nonregular_file_are_rejected(self):
        self.put('README.md')
        (self.root / 'scripts').symlink_to(self.root)
        with self.assertRaises(OSError):
            publish._read_source(self.root, PurePosixPath('scripts/README.md'))
        (self.root / 'scripts').unlink()
        (self.root / 'scripts').mkdir()
        os.mkfifo(self.root / 'scripts' / 'pipe.py')
        # Do not open a FIFO. The file-kind check must reject it without blocking.
        report = publish.inspect(self.root, mode='tree')
        self.assertIn('unsafe-file-kind-or-link', self.categories(report))

    def test_secret_patterns_report_no_values(self):
        github = 'ghp_' + 'A1' * 18
        openai = 'sk-' + 'proj-' + 'Ab_9' * 20
        private = '-----BEGIN ' + 'PRIVATE KEY-----'
        access = 'AKIA' + 'A1' * 8
        literal = 'db_password = ' + 'Ab9_' * 12
        for value, category in [(github, 'github-token'), (openai, 'openai-token'),
                                (private, 'private-key'), (access, 'aws-access-key'),
                                (literal, 'literal-credential')]:
            issues = publish.scan_content('deploy/config.py', value.encode())
            self.assertIn(category, {issue.category for issue in issues})
            rendered = json.dumps([issue.__dict__ for issue in issues])
            self.assertNotIn(value, rendered)
        self.assertFalse(publish.scan_content('deploy/config.py', b'password = YOUR_PASSWORD_REPLACE_BEFORE_USE'))

    def test_database_signature_rejected_in_disguised_source(self):
        for header in [b'PGDMP', b'-- PostgreSQL database dump\n', b'SQLite format 3\x00']:
            issues = publish.scan_content('deploy/fixture.txt', header)
            self.assertIn('database-dump-content', {issue.category for issue in issues})

    def test_machine_runtime_paths_error_and_historical_docs_warning(self):
        path = '/Users/' + 'synthetic-user/work/customer.conf'
        self.put('deploy/config.py', 'config = ' + repr(path))
        self.put('docs/history.md', 'Historical evidence: ' + path)
        report = publish.inspect(self.root, mode='tree')
        self.assertFalse(report['ok'])
        errors = {(issue['path'], issue['category']) for issue in report['issues'] if issue['severity'] == 'error'}
        warnings = {(issue['path'], issue['category']) for issue in report['issues'] if issue['severity'] == 'warning'}
        self.assertIn(('deploy/config.py', 'machine-runtime-path'), errors)
        self.assertIn(('docs/history.md', 'historical-machine-path'), warnings)
        windows = 'C:' + chr(92) + 'Users' + chr(92) + 'synthetic' + chr(92) + 'runtime.conf'
        mac_cache = '/private/var/' + 'folders/xy/synthetic1234/T/runtime.conf'
        for value in (windows, mac_cache):
            self.assertIn('machine-runtime-path', {issue.category for issue in publish.scan_content('deploy/runtime.py', value.encode())})
        self.assertNotIn('machine-runtime-path', {issue.category for issue in publish.scan_content(
            'scripts/verify_publish_tree.py', SCRIPT.read_bytes())})

    def test_github_size_limit_is_checked_before_read(self):
        self.put('scripts/large.py', b'12345')
        with patch.object(publish, 'MAX_FILE_BYTES', 4):
            report = publish.inspect(self.root, mode='tree')
        self.assertIn('github-file-size-limit', self.categories(report))

    def test_workflows_require_literal_readonly_permissions_and_no_secret_context(self):
        path = '.github/workflows/quality.yml'
        safe = b'name: QA\npermissions: {contents: read}\njobs:\n  qa:\n    permissions: {}\n'
        self.assertFalse(publish.workflow_issues(path, safe))
        cases = [b'permissions: write-all\n', b'permissions:\n  contents: write\n',
                 b'permissions: read-all\njobs:\n  qa:\n    "permissions": write-all\n',
                 b'permissions: read-all\njobs: {qa: {permissions: write-all}}\n',
                 b'permissions: ${{ inputs.permissions }}\n', b'name: QA\n']
        for candidate in cases:
            self.assertTrue(publish.workflow_issues(path, candidate), candidate)
        context = b'permissions: read-all\nenv:\n  VALUE: ${{ secrets.' + b'DEMO_TOKEN }}\n'
        self.assertIn('workflow-secret-context', {issue.category for issue in publish.workflow_issues(path, context)})

    def test_tracked_scope_requires_exact_root_and_matching_index_bytes(self):
        self.put('README.md')
        self.git('init', '--quiet')
        self.git('add', '--', 'README.md')
        report = publish.inspect(self.root)
        self.assertTrue(report['ok'], report)
        self.put('README.md', '# Changed after staging\n')
        report = publish.inspect(self.root)
        self.assertIn('working-file-differs-from-index', self.categories(report))
        (self.root / 'scripts').mkdir()
        with self.assertRaises(publish.PublishRejected):
            publish.inspect(self.root / 'scripts')

    def test_tracked_prohibited_file_is_not_opened(self):
        self.put('README.md')
        self.put('.env', 'synthetic fixture, not a real credential')
        self.git('init', '--quiet'); self.git('add', '--', 'README.md', '.env')
        original = publish._read_source
        def guarded(root, relative):
            self.assertNotEqual(str(relative), '.env')
            return original(root, relative)
        with patch.object(publish, '_read_source', guarded):
            report = publish.inspect(self.root)
        self.assertIn('private-environment', self.categories(report))

    def vendor(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:') as archive:
            for name, payload in members:
                item = tarfile.TarInfo(name); item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
        payload = gzip.compress(output.getvalue())
        archive_path = 'deploy/rtlcss/vendor/escalade-3.1.1.tgz'
        self.put(archive_path, payload)
        integrity = 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode()
        self.put(publish.LOCK_PATH, json.dumps({'packages': {'node_modules/escalade': {
            'resolved': 'file:vendor/escalade-3.1.1.tgz', 'integrity': integrity}}}))
        return self.root / archive_path

    def test_vendor_archive_requires_integrity_and_scans_members_without_extracting(self):
        path = self.vendor([('package/index.js', b'export const fixture = true;')])
        self.assertTrue(publish.inspect(self.root, mode='tree')['ok'])
        path.write_bytes(path.read_bytes() + b'tampered')
        self.assertIn('vendor-archive-integrity', self.categories(publish.inspect(self.root, mode='tree')))
        self.vendor([('package/index.js', ('ghp_' + 'A1' * 18).encode())])
        report = publish.inspect(self.root, mode='tree')
        self.assertIn('github-token', self.categories(report))
        self.assertFalse((self.root / 'package').exists())

    def test_archive_traversal_and_expansion_are_bounded(self):
        self.vendor([('../escape.py', b'# synthetic')])
        report = publish.inspect(self.root, mode='tree')
        self.assertIn('unsafe-vendor-archive-member', self.categories(report))
        self.vendor([('package/index.js', b'x' * 1000)])
        with patch.object(publish, 'MAX_ARCHIVE_BYTES', 100):
            report = publish.inspect(self.root, mode='tree')
        self.assertIn('vendor-archive-expanded-limit', self.categories(report))


if __name__ == '__main__':
    unittest.main()
