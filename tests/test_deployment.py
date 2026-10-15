"""Synthetic deployment safety tests; never connect to or modify customer databases."""
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
from prepare_source import extract_verified, sha256, validate_members
from runtime_config import config_text, identifier, secret_file, write_private
import native_staging


class DeploymentTests(unittest.TestCase):
    def test_customer_metadata_updates_preserve_the_other_customer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'staging'
            root.mkdir()
            cfg = {'marker': native_staging.MARKER, 'root': str(root), 'customers': {
                'a': {'initialized': True, 'installedModules': ['jakh_core']},
                'b': {'initialized': True, 'installedModules': ['jakh_core']}}}
            write_private(root / 'staging.json', json.dumps(cfg))
            with patch.object(native_staging, 'WORK_ROOT', Path(temporary)):
                native_staging.update_customer_state(root, 'a', modules=['jakh_branding'])
                native_staging.update_customer_state(root, 'b', modules=['jakh_assurance'])
            saved = json.loads((root / 'staging.json').read_text())
            self.assertEqual(saved['customers']['a']['installedModules'], ['jakh_branding', 'jakh_core'])
            self.assertEqual(saved['customers']['b']['installedModules'], ['jakh_assurance', 'jakh_core'])
            self.assertEqual(stat.S_IMODE((root / 'staging.json').stat().st_mode), 0o600)

    def test_offline_update_refuses_a_running_customer_before_mutation(self):
        with patch.object(native_staging.socket, 'create_connection', return_value=MagicMock()), \
                patch.object(native_staging, 'checked') as execute:
            with self.assertRaisesRegex(ValueError, 'Stop this isolated customer'):
                native_staging.offline_odoo({}, {'httpPort': 8810}, [], 'tests')
            execute.assert_not_called()

    def test_native_tests_do_not_reuse_the_preview_http_port(self):
        customer = {'initialized': True, 'httpPort': 8810}
        cfg = {'customers': {'a': customer}}
        with patch.object(native_staging, 'settings', return_value=cfg), \
                patch.object(native_staging, 'offline_odoo') as execute:
            native_staging.test_modules(SimpleNamespace(root='unused', customer='a', modules='jakh_core'))
            arguments = execute.call_args.args[2]
            self.assertEqual(arguments[arguments.index('--http-port') + 1], '18810')

    def test_offline_updates_disable_cron_and_serve_explicitly_enables_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = {'root': temporary, 'python': 'synthetic-python', 'source': temporary}
            customer = {'httpPort': 8810, 'config': 'private-config', 'database': 'synthetic', 'initialized': True}
            with patch.object(native_staging.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                    patch.object(native_staging, 'environment', return_value={}), \
                    patch.object(native_staging, 'checked') as execute:
                native_staging.offline_odoo(cfg, customer, [], 'offline')
                self.assertIn('--max-cron-threads=0', execute.call_args.args[0])
            cfg['customers'] = {'a': customer}
            with patch.object(native_staging, 'settings', return_value=cfg), \
                    patch.object(native_staging, 'environment', return_value={}), \
                    patch.object(native_staging.os, 'chdir'), patch.object(native_staging.os, 'execve') as execute:
                native_staging.serve(SimpleNamespace(root=temporary, customer='a', cron_threads=1))
                command = execute.call_args.args[1]
                self.assertEqual(command[command.index('--max-cron-threads') + 1], '1')

    def member(self, name, kind=tarfile.REGTYPE, size=0):
        member = tarfile.TarInfo(name)
        member.type, member.size = kind, size
        member.linkname = '../../outside'
        return member

    def test_translation_overwrite_is_explicit_and_jakh_scoped(self):
        cfg = {'customers': {'a': {'initialized': True, 'httpPort': 8810}}}
        args = SimpleNamespace(root='unused', customer='a', modules='jakh_core,jakh_branding',
                               overwrite_translations=True)
        with patch.object(native_staging, 'settings', return_value=cfg), \
                patch.object(native_staging, 'offline_odoo') as execute:
            native_staging.test_modules(args)
            self.assertIn('--i18n-overwrite', execute.call_args.args[2])
            execute.reset_mock()
            args.modules = 'base,jakh_core'
            with self.assertRaisesRegex(ValueError, 'restricted'):
                native_staging.test_modules(args)
            execute.assert_not_called()

    def archive(self, directory, members):
        archive = Path(directory) / 'vendor.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            for member in members:
                output.addfile(member, io.BytesIO(b'x' * member.size) if member.isfile() else None)
        return archive, {'bytes': archive.stat().st_size, 'sha256': sha256(archive), 'archiveRoot': 'vendor'}

    def test_traversal_links_special_members_and_duplicates_rejected(self):
        for name, kind in [('/etc/passwd', tarfile.REGTYPE), ('vendor/../../escape', tarfile.REGTYPE),
                           ('wrong/file', tarfile.REGTYPE), ('vendor\\escape', tarfile.REGTYPE),
                           ('vendor/link', tarfile.SYMTYPE), ('vendor/hard', tarfile.LNKTYPE),
                           ('vendor/char', tarfile.CHRTYPE), ('vendor/fifo', tarfile.FIFOTYPE)]:
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                validate_members([self.member(name, kind)], 'vendor')
        with self.assertRaises(ValueError):
            validate_members([self.member('vendor/a'), self.member('vendor/a')], 'vendor')
        with self.assertRaises(ValueError):
            validate_members([self.member('vendor/big', size=1024 ** 3 + 1)], 'vendor')

    def test_verified_archive_extracts_only_into_new_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive, spec = self.archive(temporary, [self.member('vendor', tarfile.DIRTYPE), self.member('vendor/file', size=3)])
            target = Path(temporary) / 'new-source'
            root = extract_verified(archive, target, spec)
            self.assertEqual((root / 'file').read_bytes(), b'xxx')
            with self.assertRaises(ValueError):
                extract_verified(archive, target, spec)
            self.assertEqual((root / 'file').read_bytes(), b'xxx')

    def test_hash_size_bad_archive_and_symlink_fail_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive, spec = self.archive(temporary, [self.member('vendor/file', size=1)])
            target = Path(temporary) / 'out'
            for changed in (dict(spec, sha256='0' * 64), dict(spec, bytes=0)):
                with self.assertRaises(ValueError):
                    extract_verified(archive, target, changed)
                self.assertFalse(target.exists())
            linked = Path(temporary) / 'linked.tar.gz'
            linked.symlink_to(archive)
            with self.assertRaises(ValueError):
                extract_verified(linked, target, spec)
            bad, bad_spec = self.archive(temporary, [self.member('vendor/../../escape')])
            with self.assertRaises(ValueError):
                extract_verified(bad, target, bad_spec)
            self.assertFalse(target.exists())

    def test_private_files_are_exclusive_and_secrets_not_permissive(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'secret'
            write_private(target, 'a' * 64 + '\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(secret_file(target), 'a' * 64)
            with self.assertRaises(FileExistsError):
                write_private(target, 'replacement')
            target.chmod(0o644)
            with self.assertRaises(ValueError):
                secret_file(target)
            linked = Path(temporary) / 'link'
            linked.symlink_to(target)
            with self.assertRaises(ValueError):
                secret_file(linked)

    def test_single_database_config_and_injection_rejection(self):
        config = config_text(database='customer_alpha', db_user='customer_alpha', db_password='p' * 64,
            master_password='m' * 64, addons=['/safe/addons'], data_dir='/safe/data')
        for expected in ('db_name = customer_alpha', 'dbfilter = ^customer_alpha$', 'list_db = False',
                         'http_interface = 127.0.0.1', 'proxy_mode = False', 'with_demo = False'):
            self.assertIn(expected, config)
        for value in ('customer.*', 'customer-b', 'customer\nlist_db=True', '', 'postgres;DROP', 'a' * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                identifier(value)
        with self.assertRaises(ValueError):
            config_text(database='customer', db_user='customer', db_password='admin', master_password='m' * 64, addons=['/a'], data_dir='/b')
        with self.assertRaises(ValueError):
            config_text(database='postgres', db_user='customer', db_password='p' * 64, master_password='m' * 64, addons=['/a'], data_dir='/b')
        with self.assertRaises(ValueError):
            config_text(database='customer', db_user='customer', db_password='p' * 64, master_password='m' * 64, addons=['/a\nlist_db=True'], data_dir='/b')

    def test_vendor_provenance_and_deployment_boundaries(self):
        lock = json.loads((PACKAGE / 'vendor.lock.json').read_text())
        self.assertEqual(lock['odoo']['sha256'], '53b2b33a58b9e0f743d2076b01b5a955a867c044de232aed109d838e45018961')
        self.assertEqual(lock['postgresql']['version'], '16.15')
        compose = (PACKAGE / 'deploy/compose.yaml').read_text()
        self.assertIn("127.0.0.1:${JAKH_HTTP_PORT:-8810}:8069", compose)
        self.assertNotIn('5432:5432', compose)
        self.assertIn('POSTGRES_PASSWORD_FILE', compose)
        self.assertIn('profiles: [sso]', compose)
        self.assertNotIn('admin:admin', compose)
        initializer = (PACKAGE / 'deploy/init-customer.sql').read_text()
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT', initializer)
        self.assertIn('REVOKE ALL ON DATABASE', initializer)
        entry = (PACKAGE / 'deploy/container_entrypoint.py').read_text()
        self.assertLess(entry.index('os.setuid(UID)'), entry.index("import psycopg2"))
        self.assertIn('Refusing HTTP', entry)

    def test_container_uses_filtered_runtime_not_full_vendor_source(self):
        docker = (PACKAGE / 'deploy/Dockerfile').read_text()
        self.assertIn('AS source-builder', docker)
        self.assertIn('scripts/build_lean_runtime.py', docker)
        self.assertIn('--profile /opt/jakh/deploy/runtime-modules.json', docker)
        final = docker.split('FROM runtime-base AS application', 1)[1]
        self.assertIn('COPY --from=source-builder /opt/jakh-runtime /opt/jakh-runtime', final)
        self.assertNotIn('COPY addons', final)
        self.assertNotIn('/opt/odoo-source', final)
        self.assertIn('COPY --from=source-builder /opt/jakh-rtlcss /opt/jakh-rtlcss', final)
        self.assertIn('PATH="/opt/jakh-rtlcss/node_modules/.bin:${PATH}"', docker)
        self.assertIn('npm ci --prefix /opt/jakh-rtlcss --offline --ignore-scripts --omit=dev', docker)
        self.assertIn('COPY deploy/rtlcss/vendor /opt/jakh-rtlcss/vendor', docker)
        rtl = json.loads((PACKAGE / 'deploy/rtlcss/package.json').read_text())
        self.assertEqual(rtl['jakhRtlcssVersion'], '4.3.0')
        self.assertEqual(rtl['dependencies']['rtlcss'], 'file:vendor/rtlcss-4.3.0.tgz')
        profile = json.loads((PACKAGE / 'deploy/runtime-modules.json').read_text())
        self.assertEqual(profile['languages'], ['ar_001', 'en_US'])
        self.assertEqual(len(profile['installedModules']), len(set(profile['installedModules'])))
        self.assertTrue({'base', 'web', 'jakh_core', 'jakh_branding', 'l10n_ae'}.issubset(profile['installedModules']))
        entry = (PACKAGE / 'deploy/container_entrypoint.py').read_text()
        self.assertIn("SOURCE = Path('/opt/jakh-runtime/vendor')", entry)
        self.assertIn("runtime / 'addons'", entry)
        self.assertIn("addons=application_addons('serve')", entry)
        self.assertIn("'--addons-path'", entry)

    @unittest.skipUnless(shutil.which('ruby'), 'Optional local Ruby/Psych YAML parser unavailable')
    def test_compose_yaml_parses_and_customer_ports_are_private(self):
        code = '''
require 'yaml'
d = YAML.safe_load(File.read(ARGV[0]))
abort 'unexpected services' unless d['services'].keys.sort == ['app', 'db', 'idp-db', 'keycloak']
abort 'database published' if d['services']['db'].key?('ports') || d['services']['idp-db'].key?('ports')
abort 'public app port' unless d['services']['app']['ports'].all? { |p| p.start_with?('127.0.0.1:') }
abort 'privileges not dropped' unless d['services']['app']['security_opt'] == ['no-new-privileges:true']
abort 'missing private tmpfs' unless d['services']['app']['tmpfs'].include?('/run/jakh:mode=0700')
'''
        subprocess.run([shutil.which('ruby'), '-e', code, str(PACKAGE / 'deploy/compose.yaml')], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
