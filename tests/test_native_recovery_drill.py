"""Recovery rehearsal safety tests: no real PostgreSQL or customer HTTP access."""
import ast
import configparser
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import native_recovery_drill as drill
from runtime_config import config_text, write_private


class RecoveryDrillTests(unittest.TestCase):
    def test_deadline_and_destination_paths_are_bounded(self):
        for value in (False, 0, 59, 1801):
            with self.subTest(value=value), self.assertRaises(drill.RecoveryRejected):
                drill.Deadline(value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'private'; child.mkdir()
            self.assertEqual(drill.private_path(root, child), child)
            for path in (root, root.parent / 'outside', child):
                with self.subTest(path=path), self.assertRaises(drill.RecoveryRejected):
                    drill.private_path(root, path, exists=False)
            linked = root / 'linked'; linked.symlink_to(child, target_is_directory=True)
            with self.assertRaises(drill.RecoveryRejected):
                drill.private_path(root, linked / 'new', exists=False)

    def test_active_preview_and_test_http_refuse_before_connect_or_create(self):
        cfg = {'root': 'unused', 'customers': {'a': {'httpPort': 8810}}}
        with patch.object(drill.native_staging, 'settings', return_value=cfg), \
                patch.object(drill.os, 'open', return_value=7), \
                patch.object(drill.os, 'fdopen', return_value=MagicMock()), \
                patch.object(drill.fcntl, 'flock'), \
                patch.object(drill.native_staging, 'require_stopped_customers', side_effect=ValueError('Stop BOTH isolated customer web services')), \
                patch.object(drill, '_connect') as connect:
            with self.assertRaises(ValueError):
                drill.run_drill('unused', 'a')
            connect.assert_not_called()
        with patch.object(drill.native_staging, 'require_stopped_customers'), \
                patch.object(drill.socket, 'create_connection', return_value=MagicMock()) as socket_connect:
            with self.assertRaisesRegex(drill.RecoveryRejected, 'native test HTTP'):
                drill.require_quiet_http(cfg)
            self.assertEqual(socket_connect.call_args.args[0], ('127.0.0.1', 18810))

    def test_customer_requires_exact_enrolled_private_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'customer-a'; data.mkdir()
            (root / 'secrets').mkdir()
            write_private(root / 'secrets/a-db', 'a' * 64)
            config = root / 'customer-a.conf'
            write_private(config, config_text(database='jakh_staging_a', db_user='jakh_customer_a',
                db_password='a' * 64, master_password='m' * 64, addons=['/synthetic/addons'],
                data_dir=data, db_port=17816))
            customer = {'initialized': True, 'database': 'jakh_staging_a', 'role': 'jakh_customer_a',
                        'dataDir': str(data), 'config': str(config)}
            cfg = {'root': str(root), 'pgPort': 17816, 'customers': {'a': customer}}
            self.assertEqual(drill.validate_customer(cfg, 'a')[0], customer)
            customer['initialized'] = False
            with self.assertRaisesRegex(drill.RecoveryRejected, 'enrolled'):
                drill.validate_customer(cfg, 'a')
            customer['initialized'] = True; customer['database'] = 'production_customer'
            with self.assertRaisesRegex(drill.RecoveryRejected, 'original marked synthetic'):
                drill.validate_customer(cfg, 'a')
            customer['database'] = 'jakh_staging_a'; config.chmod(0o644)
            with self.assertRaisesRegex(drill.RecoveryRejected, 'must be private'):
                drill.validate_customer(cfg, 'a')

    def test_exact_tree_copy_hashes_nested_files_and_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; source.mkdir()
            (source / 'aa').mkdir(); (source / 'aa/empty').mkdir()
            (source / 'aa/file').write_bytes(b'synthetic attachment')
            (source / 'zero').write_bytes(b'')
            deadline = drill.Deadline(60)
            inventory = drill.inventory_tree(source, deadline)
            self.assertEqual(inventory['fileCount'], 2)
            self.assertIn('aa/empty', inventory['directories'])
            target = root / 'copy'
            drill.copy_tree_new(source, target, inventory, deadline)
            self.assertEqual(drill.inventory_tree(target, deadline), inventory)
            self.assertEqual(stat.S_IMODE((target / 'aa/file').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((target / 'aa/empty').stat().st_mode), 0o700)
            with self.assertRaises(FileExistsError):
                drill.copy_tree_new(source, target, inventory, deadline)
            self.assertEqual((target / 'aa/file').read_bytes(), b'synthetic attachment')
            (source / 'aa/file').write_bytes(b'changed source')
            partial = root / 'partial'
            with self.assertRaisesRegex(drill.RecoveryRejected, 'changed during'):
                drill.copy_tree_new(source, partial, inventory, deadline)
            self.assertTrue(partial.exists())

    def test_absent_empty_filestore_is_valid_but_links_devices_and_size_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; source.mkdir()
            deadline = drill.Deadline(60)
            self.assertEqual(drill.inventory_tree(root / 'absent', deadline)['files'], {})
            linked = source / 'linked'; linked.symlink_to(root)
            with self.assertRaisesRegex(drill.RecoveryRejected, 'Linked or special'):
                drill.inventory_tree(source, deadline)
            linked.unlink()
            fifo = source / 'fifo'; os.mkfifo(fifo)
            with self.assertRaisesRegex(drill.RecoveryRejected, 'linked, special'):
                drill.inventory_tree(source, deadline)
            fifo.unlink(); (source / 'bounded').write_bytes(b'123')
            with patch.object(drill, 'MAX_FILE_BYTES', 2), self.assertRaises(drill.RecoveryRejected):
                drill.inventory_tree(source, deadline)

    def test_active_customer_database_sessions_are_a_blocker(self):
        cursor = MagicMock(); cursor.fetchone.return_value = (1,)
        connection = MagicMock(); connection.cursor.return_value.__enter__.return_value = cursor
        cfg = {'customers': {'a': {'database': 'jakh_staging_a'}, 'b': {'database': 'jakh_staging_b'}}}
        with self.assertRaisesRegex(drill.RecoveryRejected, 'another session'):
            drill.require_quiet_databases(connection, cfg)
        self.assertEqual(cursor.execute.call_args.args[1], (['jakh_staging_a', 'jakh_staging_b'],))
        cursor.fetchone.return_value = (0,)
        drill.require_quiet_databases(connection, cfg)

    def test_custom_dump_has_private_hash_evidence_and_non_archives_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'database.dump'
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(b'PGDMP synthetic private archive')
            result = drill.dump_inventory(path, drill.Deadline(60))
            self.assertEqual(result['bytes'], path.stat().st_size)
            self.assertEqual(len(result['sha256']), 64)
            path.chmod(0o644)
            with self.assertRaisesRegex(drill.RecoveryRejected, 'private regular'):
                drill.dump_inventory(path, drill.Deadline(60))
            path.chmod(0o600); path.write_bytes(b'not a dump')
            with self.assertRaisesRegex(drill.RecoveryRejected, 'custom archive'):
                drill.dump_inventory(path, drill.Deadline(60))

    def test_wrong_cluster_and_owner_role_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = {'root': temporary, 'pgPort': 17816}
            cursor = MagicMock(); cursor.fetchone.side_effect = [('jakh_staging_cluster', '/different/pgdata', '17816')]
            connection = MagicMock(); connection.cursor.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(drill.RecoveryRejected, 'not this marked synthetic cluster'):
                drill.verify_cluster(connection, cfg)
            cursor.fetchone.side_effect = [('jakh_staging_cluster', str(Path(temporary) / 'pgdata'), '17816'), (False,)]
            with self.assertRaisesRegex(drill.RecoveryRejected, 'administrator only'):
                drill.verify_cluster(connection, cfg)

    def test_postgres_commands_preserve_ownership_never_clean_and_keep_credentials_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); binaries = root / 'postgres/bin'; binaries.mkdir(parents=True)
            (binaries / 'pg_restore').write_bytes(b'synthetic executable')
            cfg = {'postgres': str(root / 'postgres'), 'pgPort': 17816, 'root': str(root)}
            with patch.object(drill.native_staging, 'WORK_ROOT', root), \
                    patch.object(drill.native_staging, 'environment', return_value={'PGSERVICE': 'unsafe', 'PGOPTIONS': 'unsafe'}), \
                    patch.object(drill.subprocess, 'run') as execute:
                log = root / 'private.log'
                drill._run_pg(cfg, 'p' * 64, 'pg_restore', ['--exit-on-error', '--dbname=jakh_dr_a_unique', '/private/dump'], log, drill.Deadline(60))
                command = execute.call_args.args[0]
                self.assertNotIn('p' * 64, ' '.join(command))
                self.assertNotIn('--clean', command); self.assertNotIn('--no-owner', command)
                self.assertEqual(execute.call_args.kwargs['env']['PGPASSWORD'], 'p' * 64)
                self.assertNotIn('PGSERVICE', execute.call_args.kwargs['env'])
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
                self.assertLessEqual(execute.call_args.kwargs['timeout'], 60)

    def test_script_has_no_destructive_cleanup_or_production_acceptance_claim(self):
        source = (PACKAGE / 'scripts/native_recovery_drill.py').read_text()
        ast.parse(source)
        for forbidden in ('DROP DATABASE', '--clean', 'rmtree(', 'ALTER DATABASE', 'pg_stop(', 'pg_start('):
            self.assertNotIn(forbidden, source)
        self.assertIn("'productionAcceptance': False", source)
        self.assertIn("'productionRPOValidated': False", source)
        self.assertIn("'cloneEnrolledOrServed': False", source)
        self.assertTrue('retainedPartialRecovery=True' in source)
        self.assertIn('sourceIds', source.replace('source IDs', 'sourceIds'))


if __name__ == '__main__':
    unittest.main()
