"""Static regression coverage for native Arabic catalogs (run in the Odoo venv)."""
import ast
from pathlib import Path
import re
import unittest

import polib


ADDONS = Path(__file__).resolve().parents[1] / 'addons'
PLACEHOLDER = re.compile(r'%(?:\([^)]+\))?[#0 +\-]*\d*(?:\.\d+)?[a-zA-Z%]')
UNAMBIGUOUS_PLACEHOLDER = re.compile(r'%(?:\([^)]+\))?[#0+\-]*\d*(?:\.\d+)?[a-zA-Z%]')


def formatted_messages(module):
    """Whitespace printf flags apply only to actual formatted Python calls.

    Without arguments, native translation correctly treats '100% after' as
    literal prose, not an ascii conversion named '% a'. Keep full placeholder
    checks for calls that really interpolate positional or named arguments.
    """
    result = set()
    for source in module.rglob('*.py'):
        if 'tests' in source.relative_to(module).parts:
            continue
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', None)
            if (name in ('_', '_lt') and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and (len(node.args) > 1 or node.keywords)):
                result.add(node.args[0].value)
    return result


class TestArabicCatalogs(unittest.TestCase):
    def test_every_custom_module_has_complete_non_fuzzy_arabic_catalog(self):
        modules = sorted(path.parent for path in ADDONS.glob('*/__manifest__.py'))
        self.assertEqual(len(modules), 7)
        for module in modules:
            with self.subTest(module=module.name):
                catalog = polib.pofile(module / 'i18n/ar.po')
                self.assertTrue(catalog)
                self.assertEqual(catalog.metadata['Language'], 'ar')
                self.assertFalse(catalog.untranslated_entries())
                self.assertFalse(catalog.fuzzy_entries())
                self.assertEqual(len({entry.msgid for entry in catalog}), len(catalog))
                interpolated = formatted_messages(module)
                for entry in catalog:
                    pattern = PLACEHOLDER if entry.msgid in interpolated or 'python-format' in entry.flags else UNAMBIGUOUS_PLACEHOLDER
                    self.assertEqual(sorted(pattern.findall(entry.msgid)),
                                     sorted(pattern.findall(entry.msgstr)), entry.msgid)

    def test_python_literal_markers_have_native_catalog_entries(self):
        for module in sorted(path.parent for path in ADDONS.glob('*/__manifest__.py')):
            messages = {entry.msgid for entry in polib.pofile(module / 'i18n/ar.po')}
            for source in module.rglob('*.py'):
                if 'tests' in source.relative_to(module).parts:
                    continue
                tree = ast.parse(source.read_text(), filename=str(source))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    name = node.func.id if isinstance(node.func, ast.Name) else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else None)
                    if name not in ('_', '_lt') or not isinstance(node.args[0], ast.Constant):
                        continue
                    message = node.args[0].value
                    if isinstance(message, str):
                        self.assertTrue(message in messages, f'{source}:{node.lineno}: Missing Arabic term: {message}')

    def test_frontend_messages_use_native_javascript_catalog_markers(self):
        for module in ('jakh_core', 'jakh_branding'):
            catalog = {entry.msgid: entry for entry in polib.pofile(ADDONS / module / 'i18n/ar.po')}
            for source in (ADDONS / module / 'static/src').glob('*.js'):
                for literal in re.findall(r'_t\(("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')', source.read_text()):
                    message = ast.literal_eval(literal)
                    self.assertIn(message, catalog, source)
                    self.assertIn('odoo-javascript', catalog[message].comment, message)

    def test_native_menu_override_references_are_retained(self):
        catalog = polib.pofile(ADDONS / 'jakh_core/i18n/ar.po')
        references = {reference for entry in catalog for reference, line in entry.occurrences}
        for xmlid in ('crm.crm_menu_root', 'sale.sale_menu_root', 'project.menu_main_pm',
                      'purchase.menu_purchase_root', 'stock.menu_stock_root', 'account.menu_finance',
                      'contacts.menu_contacts', 'mail.menu_root_discuss', 'project_todo.menu_todo_todos',
                      'spreadsheet_dashboard.spreadsheet_dashboard_menu_root'):
            self.assertIn('model:ir.ui.menu,name:' + xmlid, references)


if __name__ == '__main__':
    unittest.main()
