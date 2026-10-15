"""Checkout layout defaults retain the existing isolated staging boundary."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import native_staging


class PortableWorkRootTests(unittest.TestCase):
    def test_standalone_checkout_owns_its_work_directory(self):
        checkout = Path('/handover/JAKHERP')
        self.assertEqual(native_staging._default_work_root(checkout), checkout / 'work')

    def test_legacy_outputs_layout_preserves_existing_workspace_state(self):
        package = Path('/workspace/outputs/jakh-erp-odoo')
        self.assertEqual(native_staging._default_work_root(package), Path('/workspace/work'))

    def test_other_parent_names_do_not_select_an_ancestor_work_directory(self):
        for package in (Path('/handover/outputs-copy/JAKHERP'), Path('/handover/projects/JAKHERP')):
            with self.subTest(package=package):
                self.assertEqual(native_staging._default_work_root(package), package / 'work')

    def test_default_matches_the_current_package_layout(self):
        self.assertEqual(native_staging.WORK_ROOT,
                         native_staging._default_work_root(native_staging.PACKAGE))

    def test_settings_still_refuses_the_work_root_and_outside_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'work'
            work.mkdir()
            outside = Path(temporary) / 'outside'
            outside.mkdir()
            with patch.object(native_staging, 'WORK_ROOT', work):
                for candidate in (work, outside):
                    with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, 'specific child'):
                        native_staging.settings(candidate)

    def test_marked_child_is_accepted_but_a_linked_marker_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'work'
            staging = work / 'synthetic-local'
            staging.mkdir(parents=True)
            marker = staging / 'staging.json'
            value = {'marker': native_staging.MARKER, 'root': str(staging)}
            marker.write_text(json.dumps(value))
            with patch.object(native_staging, 'WORK_ROOT', work):
                self.assertEqual(native_staging.settings(staging), value)
                marker.unlink()
                outside_marker = Path(temporary) / 'outside-marker.json'
                outside_marker.write_text(json.dumps(value))
                marker.symlink_to(outside_marker)
                with self.assertRaisesRegex(ValueError, 'initialized isolated staging'):
                    native_staging.settings(staging)


if __name__ == '__main__':
    unittest.main()
