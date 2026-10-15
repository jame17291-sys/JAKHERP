"""No real database access: verify dependency-safe, non-destructive packaging."""
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_lean_runtime import PDF_LOCALES, build_runtime, dependency_closure, exclusion, permitted_language, slim_calendar, validate_active_languages
import build_lean_runtime
import regenerate_rtl_assets
import native_staging
from runtime_bootstrap import category_identifiers, validate_bootstrap_metadata


class LeanRuntimeTests(unittest.TestCase):
    def test_language_family_matching(self):
        for value in ('ar', 'ar_001', 'ar-SA', 'en', 'en_US', 'en-GB'):
            self.assertTrue(permitted_language(value))
        for value in ('fr', 'de', 'zh_CN', 'enough', 'arabic'):
            self.assertFalse(permitted_language(value))

    def test_customer_active_languages_are_exact_even_though_catalog_families_are_retained(self):
        validate_active_languages(['ar_001', 'en_US'])
        for value in ('ar_SY', 'en_GB', 'fr_FR'):
            with self.assertRaisesRegex(ValueError, 'Unsupported active languages'):
                validate_active_languages(['en_US', value])

    def test_dependency_closure_keeps_hidden_glue_and_detects_missing_modules(self):
        addons = {name: {'manifest': {'depends': depends}} for name, depends in
                  [('base', []), ('web', ['base']), ('jakh_core', ['web', 'hidden_glue']), ('hidden_glue', ['base'])]}
        self.assertEqual(dependency_closure(['base', 'web', 'jakh_core'], addons), set(addons))
        with self.assertRaisesRegex(ValueError, 'missing'):
            dependency_closure(['base', 'web', 'unknown'], addons)
        with self.assertRaises(ValueError):
            dependency_closure(['jakh_core'], addons)

    def test_only_known_translation_paths_are_stripped(self):
        selected = {'base', 'web'}
        self.assertEqual(exclusion(Path('odoo/addons/base/i18n/fr.po'), selected, vendor=True), 'otherLanguageCatalog')
        self.assertIsNone(exclusion(Path('odoo/addons/base/i18n/ar.po'), selected, vendor=True))
        self.assertIsNone(exclusion(Path('odoo/addons/base/i18n/base.pot'), selected, vendor=True))
        self.assertIsNone(exclusion(Path('odoo/addons/base/data/res.lang.csv'), selected, vendor=True))
        self.assertIsNone(exclusion(Path('odoo/tools/data/fr.po'), selected, vendor=True))
        self.assertEqual(exclusion(Path('odoo/addons/unused/model.py'), selected, vendor=True), 'uninstalledAddon')

    def test_fullcalendar_retains_vendor_definitions_license_and_registration(self):
        source = "/*! Vendor license */\n(function (index_js) {\n    'use strict';\n"
        for index, code in enumerate(('fr', 'ar', 'en-gb')):
            source += f"    var l{index} = {{\n        code: '{code}',\n        buttonText: {{ today: 'value' }},\n    }};\n\n"
        source += '    var localesAll = [l0, l1, l2, ];\n\n    index_js.globalLocales.push(...localesAll);\n})(FullCalendar);\n'
        result = slim_calendar(source)
        self.assertIn('/*! Vendor license */', result)
        self.assertNotIn("code: 'fr'", result)
        self.assertIn("code: 'ar'", result)
        self.assertIn('var localesAll = [l1, l2];', result)
        self.assertIn('index_js.globalLocales.push(...localesAll);', result)
        with self.assertRaises(ValueError):
            slim_calendar(source.replace('[l0, l1, l2, ]', '[l2, l1, l0]'))

    def fixture(self, root):
        source, custom = root / 'source', root / 'custom'
        custom.mkdir()
        for name in ('base', 'web', 'unused'):
            addon = source / 'odoo/addons' / name
            (addon / 'i18n').mkdir(parents=True)
            (addon / '__manifest__.py').write_text(repr({'depends': [] if name == 'base' else ['base']}))
            for language in ('ar', 'en_GB', 'fr'):
                (addon / 'i18n' / (language + '.po')).write_text(language)
            (addon / 'LICENSE').write_text('Preserve license')
        (source / 'odoo/__main__.py').write_text('pass\n')
        (source / 'LICENSE').write_text('Preserve vendor license')
        return source, custom, {'schema': 1, 'languages': ['ar_001', 'en_US'], 'installedModules': ['base', 'web']}

    def test_build_is_new_only_and_does_not_modify_original_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            self.assertTrue((source / 'odoo/addons/base/i18n/fr.po').is_file())
            self.assertTrue((target / 'vendor/odoo/addons/base/i18n/ar.po').is_file())
            self.assertFalse((target / 'vendor/odoo/addons/base/i18n/fr.po').exists())
            self.assertFalse((target / 'vendor/odoo/addons/unused').exists())
            self.assertEqual((target / 'vendor/LICENSE').read_text(), 'Preserve vendor license')
            # A tiny synthetic source can be smaller than truthful initialization
            # metadata. Savings is an exact net size, not a guaranteed positive.
            self.assertEqual(report['savedBytes'], report['sourceBytes'] - report['runtimePayloadBytes'])
            self.assertGreater(report['omitted']['uninstalledAddonBytes'], 0)
            self.assertEqual(json.loads((target / 'runtime-manifest.json').read_text())['includedModules'], ['base', 'web'])
            with self.assertRaisesRegex(ValueError, 'NEW'):
                build_runtime(source, custom, profile, target)
            with self.assertRaisesRegex(ValueError, 'overlap'):
                build_runtime(source, custom, profile, source / 'nested')

    def category_fixture(self, root):
        source, custom, profile = self.fixture(root)
        unused = source / 'odoo/addons/unused'
        (unused / '__manifest__.py').write_text(repr({'depends': ['base'], 'category': 'Services/Timesheets'}))
        (unused / 'model.py').write_text('raise RuntimeError("never copy or load omitted module code")\n')
        (source / 'odoo/addons/base/category-ref.xml').write_text('<odoo><record id="probe" model="ir.module.module"><field name="category_id" ref="base.module_category_services_timesheets"/></record></odoo>')
        return source, custom, profile

    def test_fresh_bootstrap_keeps_missing_category_metadata_without_unused_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.category_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            self.assertEqual(report['includedModules'], ['base', 'web'])
            self.assertFalse((target / 'vendor/odoo/addons/unused').exists())
            descriptor, = report['bootstrapMetadata']['descriptors']
            self.assertEqual(descriptor['category'], 'Services/Timesheets')
            path = target / 'bootstrap_addons' / descriptor['name']
            self.assertEqual(sorted(file.name for file in path.iterdir()), ['LICENSE', '__init__.py', '__manifest__.py'])
            self.assertEqual((path / '__init__.py').read_bytes(), b'')
            value = ast.literal_eval((path / '__manifest__.py').read_text())
            self.assertIs(value['installable'], False)
            self.assertIs(value['auto_install'], False)
            self.assertIs(value['application'], False)
            self.assertEqual(value['depends'], [])
            self.assertEqual(value['data'], [])
            self.assertEqual(descriptor['name'], 'unused')
            self.assertEqual(descriptor['sourceManifestSha256'], hashlib.sha256((source / 'odoo/addons/unused/__manifest__.py').read_bytes()).hexdigest())
            self.assertFalse((path / 'model.py').exists())
            self.assertIn('module_category_services_timesheets', category_identifiers(value['category']))
            self.assertEqual(validate_bootstrap_metadata(target, required=True), target / 'bootstrap_addons')
            with patch.object(native_staging, 'WORK_ROOT', root):
                native_staging.validate_runtime(target, ['base', 'web'])
            self.assertIn('raise RuntimeError', (source / 'odoo/addons/unused/model.py').read_text())

    def test_fresh_init_exposes_descriptors_but_serving_never_does(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.category_fixture(root)
            target = root / 'lean'
            build_runtime(source, custom, profile, target)
            entry_path = Path(__file__).resolve().parents[1] / 'deploy/container_entrypoint.py'
            spec = importlib.util.spec_from_file_location('synthetic_entrypoint', entry_path)
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            self.assertEqual(entry.application_addons('serve', target), [target / 'vendor/odoo/addons', target / 'addons'])
            self.assertEqual(entry.application_addons('init', target)[-1], target / 'bootstrap_addons')
            report = json.loads((target / 'runtime-manifest.json').read_text())
            report.pop('bootstrapMetadata')
            (target / 'runtime-manifest.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'Rebuild the lean runtime'):
                entry.application_addons('init', target)
            self.assertEqual(len(entry.application_addons('serve', target)), 2)

    def test_native_fresh_install_uses_one_snapshot_without_overwriting_old_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.category_fixture(root)
            target = root / 'lean'
            build_runtime(source, custom, profile, target)
            target = target.resolve()
            staging = root / 'staging'
            staging.mkdir()
            original = staging / 'customer-a.conf'
            original_text = '[options]\naddons_path = /old/vendor/odoo/addons,/live/package/addons\n'
            native_staging.write_private(original, original_text)
            cfg = {'root': str(staging), 'source': str(target / 'vendor'), 'python': 'synthetic-python',
                   'customers': {'a': {'initialized': False, 'config': str(original), 'database': 'synthetic',
                       'ownerLogin': 'owner@synthetic.test', 'ownerPasswordFile': 'synthetic-password-file'}}}
            with patch.object(native_staging, 'WORK_ROOT', root), \
                    patch.object(native_staging, 'settings', return_value=cfg), \
                    patch.object(native_staging, 'offline_odoo') as offline, \
                    patch.object(native_staging, 'checked') as enrollment, \
                    patch.object(native_staging, 'update_customer_state') as state:
                self.assertEqual(native_staging.snapshot_addons(target / 'vendor'),
                                 [target / 'vendor/odoo/addons', target / 'addons'])
                native_staging.install(SimpleNamespace(root=staging, customer='a', modules='base,web'))
                arguments = offline.call_args.args[2]
                self.assertEqual(arguments[arguments.index('--addons-path') + 1],
                                 ','.join(str(target / path) for path in ('vendor/odoo/addons', 'addons', 'bootstrap_addons')))
                chosen = Path(offline.call_args.args[1]['config'])
                self.assertNotEqual(chosen, original)
                self.assertEqual(original.read_text(), original_text)
                self.assertIn(str(target / 'addons'), chosen.read_text())
                self.assertNotIn('/live/package', chosen.read_text())
                self.assertNotIn('bootstrap_addons', chosen.read_text())
                command = enrollment.call_args.args[0]
                self.assertEqual(command[command.index('--config') + 1], str(chosen))
                self.assertEqual(state.call_args.kwargs['config'], str(chosen))
                self.assertTrue(state.call_args.kwargs['initialized'])

    def test_category_branches_keep_native_normalization_and_only_one_parent_descriptor(self):
        addons = {name: {'manifest': {'category': category}} for name, category in (
            ('base', 'Administration'), ('web', 'Administration'),
            ('parent_only', 'Human Resources'), ('appraisals', 'Human Resources/Appraisals'),
            ('contracts', 'Human Resources/Employee & Contracts'))}
        descriptors = build_lean_runtime.missing_category_descriptors(addons, {'base', 'web'})
        self.assertEqual([row['category'] for row in descriptors], ['Human Resources/Appraisals', 'Human Resources/Employee & Contracts'])
        self.assertEqual(category_identifiers('Human Resources/Employee & Contracts')[-1],
                         'module_category_human_resources_employee_and_contracts')

    def test_fresh_source_selection_uses_snapshot_addons_and_preserves_previous_configs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.payment_fixture(root)
            first, second = root / 'release-one', root / 'release-two'
            build_runtime(source, custom, profile, first)
            build_runtime(source, custom, profile, second)
            staging = root / 'staging'
            staging.mkdir()
            original = staging / 'customer-a.conf'
            original_text = '[options]\naddons_path = /old/vendor/odoo/addons,/live/package/addons\n'
            native_staging.write_private(original, original_text)
            cfg = {'root': str(staging), 'source': str(source), 'customers': {'a': {'initialized': False, 'config': str(original)}}}
            with patch.object(native_staging, 'WORK_ROOT', root), patch.object(native_staging, 'settings', return_value=cfg):
                native_staging.initialization_source(SimpleNamespace(root=staging, source=first / 'vendor'))
                selected_one = Path(cfg['customers']['a']['config'])
                self.assertIn(str(first / 'addons'), selected_one.read_text())
                self.assertNotIn('/live/package', selected_one.read_text())
                self.assertNotIn('bootstrap_addons', selected_one.read_text())
                self.assertEqual(original.read_text(), original_text)
                native_staging.initialization_source(SimpleNamespace(root=staging, source=second / 'vendor'))
                selected_two = Path(cfg['customers']['a']['config'])
                self.assertNotEqual(selected_one, selected_two)
                self.assertTrue(selected_one.is_file())
                self.assertIn(str(second / 'addons'), selected_two.read_text())
                self.assertEqual(cfg['source'], str((second / 'vendor').resolve()))

    def test_self_consistent_tampering_cannot_enable_bootstrap_code_data_or_installation(self):
        for values in ({'installable': True}, {'depends': ['base']}, {'data': ['attack.xml']},
                       {'assets': {'web.assets_backend': ['attack.js']}}, {'installable': 0}):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, custom, profile = self.category_fixture(root)
                target = root / 'lean'
                report = build_runtime(source, custom, profile, target)
                descriptor = report['bootstrapMetadata']['descriptors'][0]
                relative = 'bootstrap_addons/' + descriptor['name'] + '/__manifest__.py'
                file = target / relative
                data = ast.literal_eval(file.read_text())
                data.update(values)
                content = repr(data).encode()
                file.write_bytes(content)
                record = next(record for record in report['files'] if record['path'] == relative)
                record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
                (target / 'runtime-manifest.json').write_text(json.dumps(report))
                with patch.object(native_staging, 'WORK_ROOT', root), self.assertRaisesRegex(ValueError, 'inert and uninstallable'):
                    native_staging.validate_runtime(target, ['base', 'web'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.category_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            descriptor = report['bootstrapMetadata']['descriptors'][0]
            relative = 'bootstrap_addons/' + descriptor['name'] + '/__init__.py'
            content = b'raise RuntimeError("not permitted")\n'
            (target / relative).write_bytes(content)
            record = next(record for record in report['files'] if record['path'] == relative)
            record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
            (target / 'runtime-manifest.json').write_text(json.dumps(report))
            with patch.object(native_staging, 'WORK_ROOT', root), self.assertRaisesRegex(ValueError, 'exactly zero bytes'):
                native_staging.validate_runtime(target, ['base', 'web'])

    def test_bootstrap_initializer_is_required_and_cannot_contain_even_a_comment(self):
        for content in (None, b'\n', b'# inert-looking comment\n', b'pass\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, custom, profile = self.category_fixture(root)
                target = root / 'lean'
                report = build_runtime(source, custom, profile, target)
                descriptor = report['bootstrapMetadata']['descriptors'][0]
                relative = 'bootstrap_addons/' + descriptor['name'] + '/__init__.py'
                file = target / relative
                if content is None:
                    file.unlink()
                else:
                    file.write_bytes(content)
                    record = next(record for record in report['files'] if record['path'] == relative)
                    record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
                with self.assertRaisesRegex(ValueError, 'empty native initializer|exactly zero bytes'):
                    validate_bootstrap_metadata(target, report, required=True)

    @unittest.skipUnless(os.environ.get('JAKH_NATIVE_SOURCE'), 'Requires pinned JAKH_NATIVE_SOURCE for the actual native addons-path scanner')
    def test_pinned_native_configuration_accepts_empty_bootstrap_sentinels(self):
        # Read/exercise the pinned native scanner without importing the server,
        # connecting to PostgreSQL or importing any descriptor addon.
        native = Path(os.environ['JAKH_NATIVE_SOURCE']) / 'odoo/tools/config.py'
        tree = ast.parse(native.read_text())
        config = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'configmanager')
        scanner = next(node for node in config.body if isinstance(node, ast.FunctionDef) and node.name == '_is_addons_path')
        probe = ast.ClassDef(name='NativeAddonScanner', bases=[], keywords=[], body=[scanner], decorator_list=[])
        namespace = {'os': os}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), str(native), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.category_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            bootstrap = validate_bootstrap_metadata(target, report, required=True)
            self.assertTrue(namespace['NativeAddonScanner']._is_addons_path(str(bootstrap)))
            for descriptor in report['bootstrapMetadata']['descriptors']:
                manifest = ast.literal_eval((bootstrap / descriptor['name'] / '__manifest__.py').read_text())
                self.assertIs(manifest['installable'], False)
                self.assertEqual(manifest['depends'], [])
                self.assertEqual(manifest['data'], [])

    def payment_fixture(self, root):
        source, custom, profile = self.category_fixture(root)
        payment = source / 'odoo/addons/payment'
        payment.mkdir()
        (payment / '__manifest__.py').write_text(repr({'name': 'Retained payment registry', 'depends': ['base']}))
        (payment / 'providers.xml').write_text('<odoo><record id="provider" model="payment.provider"><field name="module_id" ref="base.module_unused"/><field name="image_128" type="bytes" file="unused/static/description/icon.png"/></record></odoo>')
        icon = source / 'odoo/addons/unused/static/description/icon.png'
        icon.parent.mkdir(parents=True)
        image = io.BytesIO()
        Image.new('RGB', (2, 2), color='#167369').save(image, format='PNG')
        icon.write_bytes(image.getvalue())
        profile['installedModules'].append('payment')
        return source, custom, profile

    def test_original_module_identity_and_only_explicit_native_png_dependency_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.payment_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            descriptor, = report['bootstrapMetadata']['descriptors']
            asset, = report['bootstrapMetadata']['assets']
            self.assertEqual(descriptor['name'], 'unused')
            self.assertEqual(asset['path'], 'bootstrap_addons/unused/static/description/icon.png')
            self.assertEqual(asset['references'], ['vendor/odoo/addons/payment/providers.xml'])
            original = source / 'odoo/addons/unused/static/description/icon.png'
            self.assertEqual(asset['sourceSha256'], hashlib.sha256(original.read_bytes()).hexdigest())
            self.assertEqual((target / asset['path']).read_bytes(), original.read_bytes())
            self.assertEqual((target / 'bootstrap_addons/unused/__init__.py').read_bytes(), b'')
            self.assertFalse((target / 'bootstrap_addons/unused/model.py').exists())
            self.assertFalse((target / 'bootstrap_addons/unused/i18n').exists())
            self.assertEqual(validate_bootstrap_metadata(target, report, required=True), target / 'bootstrap_addons')

    def test_external_code_or_unreferenced_asset_cannot_be_added_even_with_rehashed_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.payment_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            extra = 'bootstrap_addons/unused/static/attack.py'
            content = b'raise RuntimeError("must never return omitted code")\n'
            (target / extra).write_bytes(content)
            record = {'path': extra, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            report['files'].append(record)
            report['bootstrapMetadata']['assets'].append({'path': extra, 'bytes': len(content),
                'sourceSha256': record['sha256'], 'references': ['vendor/odoo/addons/payment/providers.xml']})
            with self.assertRaisesRegex(ValueError, 'Only explicitly referenced native PNGs'):
                validate_bootstrap_metadata(target, report, required=True)

    def test_rehashed_png_cannot_override_source_hash_or_its_retained_xml_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.payment_fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            asset = report['bootstrapMetadata']['assets'][0]
            image = io.BytesIO()
            Image.new('RGB', (2, 2), color='#ffffff').save(image, format='PNG')
            changed = image.getvalue()
            (target / asset['path']).write_bytes(changed)
            record = next(row for row in report['files'] if row['path'] == asset['path'])
            record.update(bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest())
            with self.assertRaisesRegex(ValueError, 'changed since verified source'):
                validate_bootstrap_metadata(target, report, required=True)
            original = (source / 'odoo/addons/unused/static/description/icon.png').read_bytes()
            (target / asset['path']).write_bytes(original)
            record.update(bytes=len(original), sha256=hashlib.sha256(original).hexdigest())
            asset['references'] = ['vendor/odoo/addons/base/category-ref.xml']
            with self.assertRaisesRegex(ValueError, 'lacks an explicit native'):
                validate_bootstrap_metadata(target, report, required=True)

    def test_unknown_external_xml_code_dependency_fails_before_creating_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.payment_fixture(root)
            file = source / 'odoo/addons/payment/providers.xml'
            file.write_text(file.read_text().replace('unused/static/description/icon.png', 'unused/model.py'))
            target = root / 'lean'
            with self.assertRaisesRegex(ValueError, 'unsupported external native XML'):
                build_runtime(source, custom, profile, target)
            self.assertFalse(target.exists())

    def test_symlink_input_rejected_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            (source / 'linked').symlink_to(source / 'LICENSE')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                build_runtime(source, custom, profile, root / 'lean')
            self.assertFalse((root / 'lean').exists())

    def test_runtime_validation_checks_hashes_and_all_installed_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            target = root / 'lean'
            build_runtime(source, custom, profile, target)
            with patch.object(native_staging, 'WORK_ROOT', root):
                self.assertEqual(native_staging.validate_runtime(target, ['base', 'web']), target.resolve())
                with self.assertRaisesRegex(ValueError, 'every currently installed'):
                    native_staging.validate_runtime(target, ['base', 'web', 'newly_installed'])
                (target / 'vendor/odoo/__main__.py').write_text('changed\n')
                with self.assertRaisesRegex(ValueError, 'changed since packaging'):
                    native_staging.validate_runtime(target, ['base', 'web'])

    def test_runtime_validation_rejects_unmanifested_code_and_missing_dependency_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            target = root / 'lean'
            report = build_runtime(source, custom, profile, target)
            with patch.object(native_staging, 'WORK_ROOT', root):
                extra = target / 'vendor/unreviewed.py'
                extra.write_text('pass\n')
                with self.assertRaisesRegex(ValueError, 'file set differs'):
                    native_staging.validate_runtime(target, ['base', 'web'])
                extra.unlink()  # Only a synthetic fixture created by this test.
                manifest_path = target / 'vendor/odoo/addons/web/__manifest__.py'
                manifest_path.unlink()
                report['files'] = [record for record in report['files'] if record['path'] != 'vendor/odoo/addons/web/__manifest__.py']
                (target / 'runtime-manifest.json').write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, 'addon manifests differ'):
                    native_staging.validate_runtime(target, ['base', 'web'])

    def test_runtime_switch_refuses_any_running_customer(self):
        with patch.object(native_staging.socket, 'create_connection', return_value=MagicMock()):
            with self.assertRaisesRegex(ValueError, 'BOTH'):
                native_staging.require_stopped_customers({'customers': {'a': {'httpPort': 8810}}})

    def test_pdf_viewer_retains_arabic_and_required_english_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            locales = source / PDF_LOCALES
            index = {'ar': 'ar/viewer.ftl', 'en-us': 'en-US/viewer.ftl', 'fr': 'fr/viewer.ftl'}
            for path in index.values():
                (locales / path).parent.mkdir(parents=True)
                (locales / path).write_text('pdfjs label')
            (locales / 'locale.json').write_text(json.dumps(index))
            build_runtime(source, custom, profile, root / 'lean')
            retained = root / 'lean/vendor' / PDF_LOCALES
            self.assertEqual(set(json.loads((retained / 'locale.json').read_text())), {'ar', 'en-us'})
            self.assertTrue((retained / 'ar/viewer.ftl').is_file())
            self.assertTrue((retained / 'en-US/viewer.ftl').is_file())
            self.assertFalse((retained / 'fr').exists())

    def test_activate_and_restore_only_change_runtime_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, custom, profile = self.fixture(root)
            runtime, staging = root / 'lean', root / 'staging'
            staging.mkdir()
            build_runtime(source, custom, profile, runtime)
            rtl_bin = root / 'rtl-bin'
            rtl_bin.mkdir()
            (rtl_bin / 'rtlcss').write_text('#!/bin/sh\nexit 0\n')
            (rtl_bin / 'rtlcss').chmod(0o755)
            customers = {}
            for key, port in (('a', 8810), ('b', 8811)):
                config = staging / f'{key}.conf'
                native_staging.write_private(config, '[options]\naddons_path = /original/addons\ndb_name = customer_' + key + '\n')
                customers[key] = {'httpPort': port, 'config': str(config), 'database': 'customer_' + key}
            cfg = {'marker': native_staging.MARKER, 'root': str(staging.resolve()), 'source': str(source), 'customers': customers}
            native_staging.write_private(staging / 'staging.json', json.dumps(cfg))
            args = SimpleNamespace(root=staging, action='activate-runtime', runtime=runtime, rtlcss_bin=rtl_bin)
            with patch.object(native_staging, 'WORK_ROOT', root), \
                    patch.object(native_staging.socket, 'create_connection', side_effect=ConnectionRefusedError), \
                    patch.object(regenerate_rtl_assets, 'compiler_health', return_value={'version': '4.3.0', 'nativeConversionProbe': 'passed'}), \
                    patch.object(build_lean_runtime, 'native_inventory', return_value=profile):
                native_staging.change_runtime(args)
                active = native_staging.settings(staging)
                self.assertEqual(active['source'], str(runtime.resolve() / 'vendor'))
                self.assertTrue(native_staging.environment(active)['PATH'].startswith(str(rtl_bin.resolve())))
                for key in customers:
                    self.assertEqual(active['customers'][key]['database'], 'customer_' + key)
                    self.assertNotEqual(active['customers'][key]['config'], customers[key]['config'])
                    self.assertIn(str(runtime.resolve() / 'addons'), Path(active['customers'][key]['config']).read_text())
                    self.assertIn('/original/addons', Path(customers[key]['config']).read_text())
                args.action = 'restore-runtime'
                native_staging.change_runtime(args)
                restored = native_staging.settings(staging)
                self.assertEqual(restored['source'], str(source))
                self.assertEqual(restored['customers'], customers)
                self.assertFalse('runtimeRollback' in restored)
                self.assertFalse('rtlcssBin' in restored)
                self.assertFalse('rtlcssReady' in restored)
                previous_rtl = root / 'previous-rtl-bin'
                previous_rtl.mkdir()
                (previous_rtl / 'rtlcss').write_text('#!/bin/sh\nexit 0\n')
                (previous_rtl / 'rtlcss').chmod(0o755)
                restored['rtlcssBin'] = str(previous_rtl)
                restored['rtlcssReady'] = True
                (staging / 'staging.json').write_text(json.dumps(restored))
                args.action = 'activate-runtime'
                native_staging.change_runtime(args)
                args.action = 'restore-runtime'
                native_staging.change_runtime(args)
                restored = native_staging.settings(staging)
                self.assertEqual(restored['rtlcssBin'], str(previous_rtl))
                self.assertTrue(restored['rtlcssReady'])


if __name__ == '__main__':
    unittest.main()
