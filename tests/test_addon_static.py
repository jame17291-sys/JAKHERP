"""Dependency-free structural checks, not a substitute for Odoo ORM/browser tests."""
import ast
import csv
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class AddonStructure(unittest.TestCase):
    def test_python_parses(self):
        for file in (ROOT / 'addons').rglob('*.py'):
            with self.subTest(file=str(file.relative_to(ROOT))):
                ast.parse(file.read_text(), filename=str(file))

    def test_xml_parses(self):
        for file in (ROOT / 'addons').rglob('*.xml'):
            with self.subTest(file=str(file.relative_to(ROOT))):
                self.assertIn(ET.parse(file).getroot().tag, ('odoo', 'templates'))

    def test_settings_icons_align_with_native_labels(self):
        root = ET.parse(ROOT / 'addons/jakh_branding/static/src/frontend_cleanup.xml').getroot()
        template = root.find("t[@t-name='jakh_branding.SettingsTabIcons']")
        self.assertEqual(template.get('t-inherit'), 'web.SettingsPage.SidePanel.Regular')
        alignment = next(node for node in template.findall('xpath') if node.get('expr') == "//a[hasclass('tab')]")
        self.assertEqual(alignment.get('position'), 'attributes')
        attribute = alignment.find("attribute[@name='class']")
        self.assertEqual(attribute.get('add'), 'align-items-center')
        self.assertEqual(attribute.get('separator'), ' ', 'Append alignment without replacing native tab styles or events')
        icon = template.find('.//i')
        self.assertEqual(icon.get('aria-hidden'), 'true')
        self.assertIn('d-md-block', icon.get('class'), 'Keep the native responsive visibility')

    def test_manifest_files_and_clean_core(self):
        for path in (ROOT / 'addons').glob('*/__manifest__.py'):
            manifest = ast.literal_eval(path.read_text())
            self.assertTrue(manifest['version'].startswith('20.0.'))
            self.assertEqual(manifest['license'], 'LGPL-3')
            self.assertTrue(manifest['installable'])
            for name in manifest.get('data', []):
                with self.subTest(addon=path.parent.name, data=name):
                    self.assertTrue((path.parent / name).is_file())
            self.assertNotIn('web_enterprise', manifest['depends'])

    def test_manifest_asset_files(self):
        """Check paths introduced by strings and Odoo's asset directives."""
        directive_arities = {
            'append': 2, 'prepend': 2, 'remove': 2, 'include': 2,
            'before': 3, 'after': 3, 'replace': 3,
        }
        addons = ROOT / 'addons'
        for path in addons.glob('*/__manifest__.py'):
            manifest = ast.literal_eval(path.read_text())
            for bundle, entries in manifest.get('assets', {}).items():
                for entry in entries:
                    with self.subTest(addon=path.parent.name, bundle=bundle, asset=entry):
                        if isinstance(entry, str):
                            names = [entry]
                        else:
                            self.assertIsInstance(entry, (tuple, list))
                            self.assertTrue(entry, 'An asset directive cannot be empty')
                            self.assertIn(entry[0], directive_arities)
                            self.assertEqual(len(entry), directive_arities[entry[0]])
                            if entry[0] == 'include':
                                # Include refers to an asset bundle, not a file.
                                self.assertIsInstance(entry[1], str)
                                self.assertIn('.', entry[1])
                                continue
                            names = entry[1:]
                        for name in names:
                            self.assertIsInstance(name, str)
                            asset = Path(name)
                            self.assertFalse(asset.is_absolute())
                            self.assertNotIn('..', asset.parts)
                            self.assertGreaterEqual(len(asset.parts), 3)
                            self.assertEqual(asset.parts[1], 'static')
                            if (addons / asset.parts[0]).is_dir():
                                # glob also validates native wildcard asset entries.
                                self.assertTrue(
                                    any(candidate.is_file() for candidate in addons.glob(name)),
                                    f'Manifest asset has no matching file: {name}',
                                )
                            else:
                                # Vendor targets can be referenced by before/after/remove;
                                # Odoo resolves those against the pinned vendor tree.
                                self.assertIsInstance(entry, (tuple, list))
                                self.assertIn(entry[0], ('before', 'after', 'replace', 'remove'))
                                self.assertEqual(name, entry[1])

    def test_odoo20_security_format(self):
        for path in (ROOT / 'addons').glob('*/security/ir.access.csv'):
            with path.open(newline='') as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, ['id', 'name', 'model_id', 'group_id/id', 'operation', 'domain'])
                for row in reader:
                    self.assertIn(row['operation'], ('c', 'r', 'u', 'd', 'cr', 'cu', 'cd', 'ru', 'rd', 'ud', 'cru', 'crd', 'cud', 'rud', 'crud'))
                    self.assertTrue(row['model_id'])
                    if not row['group_id/id']:
                        self.assertTrue(row['domain'], 'Global entries restrict records, never grant access')

    def test_vendor_is_locked(self):
        lock = json.loads((ROOT / 'vendor.lock.json').read_text())
        self.assertEqual(lock['odoo']['edition'], 'Community')
        self.assertEqual(lock['odoo']['version'], '20.0.post20261012')
        self.assertEqual(len(lock['odoo']['sha256']), 64)
        self.assertTrue(lock['odoo']['url'].startswith('https://nightly.odoo.com/'))

    def test_complete_scope_baseline(self):
        register = json.loads((ROOT / 'requirements/traceability.json').read_text())
        self.assertEqual(len(register['streams']), 9)
        self.assertEqual(len(register['gaps']), 42)
        self.assertEqual(len({row['id'] for row in register['gaps']}), 42)
        self.assertEqual(len(register['ricefw']), 34)
        self.assertTrue(register['acceptanceState'].startswith('Unaccepted'))

    def test_acceptance_design_covers_every_gap_without_forged_signoff(self):
        register = json.loads((ROOT / 'requirements/traceability.json').read_text())
        matrix = json.loads((ROOT / 'requirements/acceptance-matrix.json').read_text())
        rows = matrix['gapScenarios']
        self.assertEqual(len(rows), 42)
        self.assertEqual({row['id'] for row in rows}, {row['id'] for row in register['gaps']})
        for row in rows:
            self.assertTrue(row['owner'])
            self.assertTrue(row['acceptance'])
            self.assertIn(row['state'], ('planned', 'partial'))
            for evidence in row.get('technicalEvidence', []):
                self.assertTrue((ROOT / evidence.split('::')[0]).is_file())
        self.assertEqual(matrix['signOff']['state'], 'Awaiting human review')
        self.assertIsNone(matrix['signOff']['customerApprover'])
        self.assertIsNone(matrix['signOff']['approvedOn'])


if __name__ == '__main__':
    unittest.main()
