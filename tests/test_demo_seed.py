"""Bounded seed safety regressions; no Odoo, database, mail or network service.

Helpers use private temporary fixtures. The worker tests replace the registry,
database connection and business scenario, while exercising the real transaction
guard and failure paths.
"""
import ast
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import seed_construction_demo as seed


def manifest():
    return {'schema': 1, 'scenario': seed.SCENARIO, 'synthetic': True,
            'status': 'complete', 'customer': 'a', 'database': 'jakh_staging_a',
            'company_id': 7, 'records': {'project': {'model': 'project.project', 'id': 12}},
            'label': 'DEMO ONLY — تجربة'}


class OfflineBoundaryTests(unittest.TestCase):
    def test_socket_guard_blocks_external_and_named_addresses_without_io(self):
        stream = object()
        with patch.object(seed.socket.socket, 'connect', return_value='local') as connect, \
                patch.object(seed.socket.socket, 'connect_ex', return_value=0) as connect_ex:
            with seed.offline_only():
                for address in [('203.0.113.1', 443), ('example.invalid', 443),
                                ('localhost', 5432), '/private/unix-socket']:
                    for method in (seed.socket.socket.connect, seed.socket.socket.connect_ex):
                        with self.subTest(address=address, method=method), \
                                self.assertRaises(seed.RecoveryRejected):
                            method(stream, address)
                connect.assert_not_called()
                connect_ex.assert_not_called()
                self.assertEqual(seed.socket.socket.connect(stream, ('127.0.0.1', 5432)), 'local')
                self.assertEqual(seed.socket.socket.connect_ex(stream, ('::1', 5432, 0, 0)), 0)
            connect.assert_called_once_with(stream, ('127.0.0.1', 5432))
            connect_ex.assert_called_once_with(stream, ('::1', 5432, 0, 0))
            self.assertIs(seed.socket.socket.connect, connect)
            self.assertIs(seed.socket.socket.connect_ex, connect_ex)

    def test_smtp_is_blocked_even_on_loopback_and_guard_restores_on_error(self):
        with patch.object(seed.smtplib.SMTP, 'connect') as transport, \
                patch.object(seed.socket.socket, 'connect') as connect:
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                with seed.offline_only():
                    for host in ('127.0.0.1', '::1', 'mail.example.invalid'):
                        with self.subTest(host=host), self.assertRaises(seed.RecoveryRejected):
                            seed.smtplib.SMTP.connect(object(), host, 25)
                    raise RuntimeError('synthetic failure')
            transport.assert_not_called()
            connect.assert_not_called()
            self.assertIs(seed.smtplib.SMTP.connect, transport)
            self.assertIs(seed.socket.socket.connect, connect)


class ManifestBoundaryTests(unittest.TestCase):
    def test_header_requires_complete_synthetic_customer_binding(self):
        seed._validate_manifest_header(manifest(), 'a', 'jakh_staging_a', 7)
        for field, value in [('schema', 2), ('scenario', 'other-scenario'),
                             ('synthetic', False), ('synthetic', 1), ('status', 'running'),
                             ('customer', 'b'), ('database', 'production'), ('company_id', 8),
                             ('records', {}), ('records', [])]:
            candidate = manifest()
            candidate[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(seed.RecoveryRejected):
                seed._validate_manifest_header(candidate, 'a', 'jakh_staging_a', 7)
        for candidate in (None, [], 'not a manifest', {}):
            with self.subTest(candidate=candidate), self.assertRaises(seed.RecoveryRejected):
                seed._validate_manifest_header(candidate, 'a', 'jakh_staging_a', 7)

    def test_private_export_is_exclusive_owner_only_and_identical_retry_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'secrets').mkdir(mode=0o700)
            value = manifest()
            destination = seed._private_manifest({'root': str(root)}, 'a', value)
            self.assertEqual(destination, root / 'secrets/construction-demo-a.json')
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(json.loads(destination.read_text()), value)
            original = destination.read_bytes()
            with patch.object(seed, 'write_private') as write:
                self.assertEqual(seed._private_manifest({'root': str(root)}, 'a', value), destination)
                write.assert_not_called()
            self.assertEqual(destination.read_bytes(), original)

    def test_mismatched_or_nonprivate_export_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'secrets').mkdir(mode=0o700)
            value = manifest()
            destination = seed._private_manifest({'root': str(root)}, 'a', value)
            original = destination.read_bytes()
            changed = dict(value, company_id=8)
            with self.assertRaisesRegex(seed.RecoveryRejected, 'not overwritten'):
                seed._private_manifest({'root': str(root)}, 'a', changed)
            self.assertEqual(destination.read_bytes(), original)
            destination.chmod(0o644)
            with self.assertRaisesRegex(seed.RecoveryRejected, 'must be private'):
                seed._private_manifest({'root': str(root)}, 'a', value)
            self.assertEqual(destination.read_bytes(), original)

    def test_manifest_file_and_parent_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root, target_root = Path(temporary), Path(outside)
            secrets = root / 'secrets'
            secrets.mkdir(mode=0o700)
            target = target_root / 'existing.json'
            target.write_text('retained outside fixture')
            destination = secrets / 'construction-demo-a.json'
            destination.symlink_to(target)
            with self.assertRaises(seed.RecoveryRejected):
                seed._private_manifest({'root': str(root)}, 'a', manifest())
            self.assertEqual(target.read_text(), 'retained outside fixture')
            destination.unlink()
            secrets.rmdir()
            secrets.symlink_to(target_root, target_is_directory=True)
            with self.assertRaises(seed.RecoveryRejected):
                seed._private_manifest({'root': str(root)}, 'a', manifest())
            self.assertFalse((target_root / 'construction-demo-a.json').exists())

    def test_exclusive_writer_refuses_a_destination_created_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'secrets').mkdir(mode=0o700)
            original_writer = seed.write_private

            def competing_writer(path, content):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write('retained concurrent fixture')
                return original_writer(path, content)

            with patch.object(seed, 'write_private', side_effect=competing_writer), \
                    self.assertRaises(FileExistsError):
                seed._private_manifest({'root': str(root)}, 'a', manifest())
            self.assertEqual((root / 'secrets/construction-demo-a.json').read_text(),
                             'retained concurrent fixture')


class FakeCursor:
    def __init__(self, events):
        self.events = events
        self.connection_identity = ('jakh_customer_a', 'jakh_staging_a', False,
                                    False, False, False, 'jakh_customer_a')
        self.result = None
        self.commits = self.rollbacks = self.closes = 0

    def execute(self, query, values=None):
        self.events.append(('sql', query))
        normalized = ' '.join(query.lower().split())
        if normalized.startswith('select current_user, current_database()'):
            self.result = self.connection_identity
        elif normalized.startswith('select id,company_id from res_users'):
            self.result = (2, 7)
        elif normalized.startswith('select pg_try_advisory_xact_lock('):
            self.result = (True,)
        else:
            raise AssertionError('Unexpected SQL reached the isolated worker fixture')

    def fetchone(self):
        return self.result

    def commit(self):
        self.commits += 1
        self.events.append(('commit',))

    def rollback(self):
        self.rollbacks += 1
        self.events.append(('rollback',))

    def close(self):
        self.closes += 1


@contextmanager
def mocked_worker(existing=False, queue_mail=False):
    """Replace every external boundary, retaining the production worker body."""
    events = []
    cursor = FakeCursor(events)
    value = manifest()
    parameters = MagicMock()
    parameters.get_str.return_value = json.dumps(value) if existing else ''
    parameters.get_bool.return_value = False
    parameters.set_str.side_effect = lambda key, raw: events.append(('manifest', key, json.loads(raw)))
    parameters.set_bool.side_effect = lambda key, enabled: events.append(('enabled', key, enabled))
    modules, projects, mail = MagicMock(), MagicMock(), MagicMock()
    modules.search.return_value.mapped.return_value = list(seed.REQUIRED_MODULES)
    projects.with_context.return_value.search_count.return_value = 0
    mail.search.side_effect = [SimpleNamespace(ids=[40]), SimpleNamespace(ids=[40, 41] if queue_mail else [40])]

    class Environment:
        su = False
        user = SimpleNamespace(has_group=lambda group: True)
        company = SimpleNamespace(id=7, name='JAKH SYNTHETIC Customer A — SA',
                                  country_id=SimpleNamespace(code='SA'),
                                  currency_id=SimpleNamespace(name='SAR'))

        def __getitem__(self, model):
            return {'ir.config_parameter': parameters, 'ir.module.module': modules,
                    'project.project': projects, 'mail.mail': mail}[model]

    env = Environment()
    api = SimpleNamespace(Environment=MagicMock(return_value=env))
    odoo = ModuleType('odoo')
    odoo.SUPERUSER_ID = 1
    odoo.api = api
    odoo.tools = SimpleNamespace(config=SimpleNamespace(parse_config=MagicMock()))
    registry_module = ModuleType('odoo.modules.registry')
    registry_module.Registry = MagicMock(return_value=SimpleNamespace(cursor=lambda: cursor))
    scenario = MagicMock()
    scenario.run.side_effect = lambda: (events.append(('scenario',)) or value)
    cfg = {'root': '/synthetic-staging', 'customers': {}}
    customer = {'config': '/synthetic-staging/customer-a.conf', 'database': 'jakh_staging_a',
                'role': 'jakh_customer_a', 'ownerLogin': 'owner@invalid.test'}

    def export(*args):
        events.append(('export',))
        return Path('/synthetic-staging/secrets/construction-demo-a.json')

    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {'odoo': odoo,
            'odoo.modules': ModuleType('odoo.modules'), 'odoo.modules.registry': registry_module}))
        stack.enter_context(patch.object(seed.native_staging, 'settings', return_value=cfg))
        mocks = stack.enter_context(patch.multiple(seed,
            require_quiet_http=MagicMock(), validate_customer=MagicMock(return_value=(customer, None)),
            private_path=MagicMock(side_effect=lambda root, path, **kwargs: path),
            secret_file=MagicMock(return_value='private-fixture-not-a-credential'),
            _connect=MagicMock(return_value=MagicMock()), verify_cluster=MagicMock(),
            require_quiet_databases=MagicMock(), Demo=MagicMock(return_value=scenario),
            validate_saved=MagicMock(), _private_manifest=MagicMock(side_effect=export)))
        stack.enter_context(redirect_stdout(io.StringIO()))
        yield SimpleNamespace(events=events, cursor=cursor, value=value, parameters=parameters,
                              scenario=scenario, env=env, api=api, mocks=mocks)


class TransactionBoundaryTests(unittest.TestCase):
    def test_wrong_connection_database_owner_or_role_privileges_refuse_before_business_work(self):
        baseline = ('jakh_customer_a', 'jakh_staging_a', False,
                    False, False, False, 'jakh_customer_a')
        for position, value in [(0, 'other_role'), (1, 'other_database'),
                                (2, True), (3, True), (4, True), (5, True), (6, 'other_owner')]:
            with self.subTest(position=position), mocked_worker() as fixture:
                identity = list(baseline)
                identity[position] = value
                fixture.cursor.connection_identity = tuple(identity)
                with self.assertRaisesRegex(seed.RecoveryRejected, 'dedicated unprivileged'):
                    seed.worker(Path('/synthetic-staging'), 'a')
                fixture.api.Environment.assert_not_called()
                fixture.scenario.run.assert_not_called()
                fixture.parameters.set_str.assert_not_called()
                self.assertEqual((fixture.cursor.commits, fixture.cursor.rollbacks, fixture.cursor.closes), (0, 1, 1))
                self.assertNotIn('export', [event[0] for event in fixture.events])

    def test_complete_dataset_and_marker_precede_the_only_final_commit_and_export(self):
        with mocked_worker() as fixture:
            self.assertEqual(seed.worker(Path('/synthetic-staging'), 'a'), fixture.value)
            kinds = [event[0] for event in fixture.events]
            self.assertLess(kinds.index('scenario'), kinds.index('manifest'))
            self.assertLess(kinds.index('manifest'), kinds.index('enabled'))
            self.assertLess(kinds.index('enabled'), kinds.index('commit'))
            self.assertLess(kinds.index('commit'), kinds.index('export'))
            self.assertEqual((fixture.cursor.commits, fixture.cursor.rollbacks, fixture.cursor.closes), (1, 0, 1))
            context = fixture.api.Environment.call_args.args[2]
            self.assertEqual(context['allowed_company_ids'], [7])
            self.assertFalse(context['mail_notify_force_send'])
            self.assertTrue(context['no_reset_password'])

    def test_business_actions_and_marker_writes_cannot_commit_early(self):
        for phase in ('scenario', 'marker'):
            with self.subTest(phase=phase), mocked_worker() as fixture:
                if phase == 'scenario':
                    fixture.scenario.run.side_effect = lambda: fixture.cursor.commit()
                else:
                    fixture.parameters.set_str.side_effect = lambda *args: fixture.cursor.commit()
                with self.assertRaisesRegex(seed.RecoveryRejected, 'single seed transaction'):
                    seed.worker(Path('/synthetic-staging'), 'a')
                self.assertEqual((fixture.cursor.commits, fixture.cursor.rollbacks, fixture.cursor.closes), (0, 1, 1))
                fixture.parameters.set_bool.assert_not_called()
                self.assertNotIn('export', [event[0] for event in fixture.events])

    def test_scenario_failure_and_queued_mail_roll_back_without_marker_or_export(self):
        for queued in (False, True):
            with self.subTest(queued_mail=queued), mocked_worker(queue_mail=queued) as fixture:
                if not queued:
                    fixture.scenario.run.side_effect = ValueError('synthetic business failure')
                with self.assertRaises(seed.RecoveryRejected if queued else ValueError):
                    seed.worker(Path('/synthetic-staging'), 'a')
                self.assertEqual((fixture.cursor.commits, fixture.cursor.rollbacks, fixture.cursor.closes), (0, 1, 1))
                fixture.parameters.set_str.assert_not_called()
                fixture.parameters.set_bool.assert_not_called()
                self.assertNotIn('export', [event[0] for event in fixture.events])

    def test_existing_marker_is_validated_and_returned_without_replaying_or_committing(self):
        with mocked_worker(existing=True) as fixture:
            self.assertEqual(seed.worker(Path('/synthetic-staging'), 'a'), fixture.value)
            fixture.scenario.run.assert_not_called()
            fixture.parameters.set_str.assert_not_called()
            fixture.parameters.set_bool.assert_not_called()
            self.assertEqual((fixture.cursor.commits, fixture.cursor.rollbacks, fixture.cursor.closes), (0, 1, 1))
            self.assertEqual([event[0] for event in fixture.events][-2:], ['rollback', 'export'])

    def test_business_scenario_has_no_elevated_destructive_or_transaction_sql(self):
        tree = ast.parse((PACKAGE / 'scripts/seed_construction_demo.py').read_text())
        scenario = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Demo')
        forbidden = {'sudo', 'unlink', 'rmtree', 'commit', 'rollback'}
        for node in ast.walk(scenario):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            self.assertNotIn(node.func.attr, forbidden)
            if node.func.attr == 'execute':
                self.assertTrue(node.args and isinstance(node.args[0], ast.Constant))
                self.assertTrue(node.args[0].value.lstrip().upper().startswith('SELECT '))


if __name__ == '__main__':
    unittest.main()
