"""Keep the dashboard and module navigation independently scrollable."""
import ast
from pathlib import Path
import re
import unittest
from xml.etree import ElementTree as ET

from test_label_layout import compiled_rules, sass


ADDONS = Path(__file__).resolve().parents[1] / 'addons'
DASHBOARD = 'body.o_web_client > .o_action_manager > .jakh-dashboard.o_action'
NATIVE_ACTION = 'html .o_web_client > .o_action_manager > .o_action'
NATIVE_MOBILE_ACTION = f'{NATIVE_ACTION}:not(.o_action_delegate_scroll)'


def action_specificity(selector):
    """Specificity for these pinned native/custom action selectors only."""
    selector = re.sub(r':not\(([^()]*)\)', r'\1', selector)
    if any(character in selector for character in '[:#'):
        raise AssertionError('Unsupported selector in action-specificity fixture')
    classes = len(re.findall(r'\.[\w-]+', selector))
    rest = re.sub(r'\.[\w-]+', '', selector)
    elements = re.findall(r'\b[a-zA-Z][\w-]*\b', rest)
    return 0, classes, len(elements)


@unittest.skipUnless(sass is not None, 'Optional libsass compiler is not installed')
class TestCompiledScrollLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.theme = tuple(compiled_rules(sass.compile(
            filename=str(ADDONS / 'jakh_core/static/src/theme.scss'), output_style='expanded',
        )))
        cls.sidebar = tuple(compiled_rules(sass.compile(
            filename=str(ADDONS / 'jakh_branding/static/src/sidebar.scss'), output_style='expanded',
        )))

    def declarations(self, rules, selector, media=()):
        matching = [rule for rule in rules if rule.media == media and selector in rule.selectors]
        self.assertTrue(matching, f'Missing compiled selector {selector!r} in {media!r}')
        result = {}
        for rule in matching:
            for name, declaration in rule.declarations.items():
                if name not in result or declaration[1] or not result[name][1]:
                    result[name] = declaration
        return result

    def test_dashboard_is_a_constrained_natural_flow_scroll_container(self):
        declarations = self.declarations(self.theme, DASHBOARD)
        for name, value in {
            'display': 'block', 'height': '100%', 'min-height': '0', 'min-width': '0',
            'overflow-y': 'auto', 'overflow-x': 'hidden',
            'scrollbar-width': 'auto', 'scrollbar-gutter': 'stable',
        }.items():
            self.assertEqual(declarations.get(name), (value, False), name)
        # Do not restore the old min-height:100% or flex-column section sizing
        # through a shorter or responsive rule.
        for rule in self.theme:
            if any(selector in ('.jakh-dashboard', DASHBOARD) for selector in rule.selectors):
                if 'min-height' in rule.declarations:
                    self.assertEqual(rule.declarations['min-height'][0], '0')
                if 'display' in rule.declarations:
                    self.assertEqual(rule.declarations['display'][0], 'block')

    def test_override_beats_native_desktop_and_ties_mobile_specificity(self):
        # The old .jakh-dashboard rule lost against the native three-class
        # selector. Mobile adds a class through :not(); equal specificity is
        # intentional because JAKH's normal backend assets load after web.
        self.assertEqual(action_specificity(DASHBOARD), (0, 4, 1))
        self.assertGreater(action_specificity(DASHBOARD), action_specificity(NATIVE_ACTION))
        self.assertEqual(action_specificity(DASHBOARD), action_specificity(NATIVE_MOBILE_ACTION))
        manifest = ast.literal_eval((ADDONS / 'jakh_core/__manifest__.py').read_text())
        self.assertIn('web', manifest['depends'])
        self.assertIn('jakh_core/static/src/theme.scss', manifest['assets']['web.assets_backend'])
        self.assertFalse(any(isinstance(asset, (list, tuple)) and
                             'jakh_core/static/src/theme.scss' in asset
                             for asset in manifest['assets']['web.assets_backend']))

    def test_desktop_scrollbars_are_not_hidden_by_native_action_rules(self):
        scrollbar = self.declarations(self.theme, f'{DASHBOARD}::-webkit-scrollbar')
        self.assertEqual(scrollbar.get('display'), ('block', False))
        self.assertEqual(scrollbar.get('width'), ('10px', False))
        self.assertIn('background', self.declarations(self.theme, f'{DASHBOARD}::-webkit-scrollbar-thumb'))
        self.assertIn('background', self.declarations(self.theme, f'{DASHBOARD}::-webkit-scrollbar-track'))
        for rule in self.theme:
            if any(selector.startswith(DASHBOARD) for selector in rule.selectors):
                self.assertNotEqual(rule.declarations.get('scrollbar-width', ('', False))[0], 'none')
                self.assertNotEqual(rule.declarations.get('display', ('', False))[0], 'none')

    def test_sidebar_nav_scrolls_with_stationary_brand_and_footer(self):
        sidebar = self.declarations(self.sidebar, '.jakh_sidebar')
        for name, value in {'position': 'fixed', 'inset-block': '0', 'min-height': '0',
                            'display': 'flex', 'flex-direction': 'column'}.items():
            self.assertEqual(sidebar.get(name), (value, False), name)
        nav = self.declarations(self.sidebar, '.jakh_sidebar_navigation')
        for name, value in {'flex': '1 1 auto', 'min-height': '0', 'overflow-y': 'auto',
                            'overflow-x': 'hidden'}.items():
            self.assertEqual(nav.get(name), (value, False), name)
        for selector in ('.jakh_sidebar_brand', '.jakh_sidebar_footer'):
            self.assertEqual(self.declarations(self.sidebar, selector).get('flex-shrink'), ('0', False))

    def test_scrolling_changes_are_not_applied_to_other_actions_or_the_page(self):
        for rule in self.theme:
            for selector in rule.selectors:
                if '.o_action_manager' in selector:
                    self.assertTrue(selector == DASHBOARD or selector.startswith(f'{DASHBOARD}::'), selector)
                if any(rule.declarations.get(name, ('', False))[0] in ('auto', 'scroll')
                       for name in ('overflow', 'overflow-y')):
                    self.assertEqual(selector, DASHBOARD)
        for rule in self.sidebar:
            for selector in rule.selectors:
                if selector.startswith('body') and '.o_action_manager' not in selector and ' ' not in selector:
                    # The existing mobile drawer lock is deliberate. No global
                    # page scrolling is enabled to work around the dashboard.
                    for name in ('overflow', 'overflow-y'):
                        self.assertNotIn(rule.declarations.get(name, ('', False))[0], ('auto', 'scroll'))
                if selector.endswith('> .o_action_manager'):
                    self.assertFalse({'overflow', 'overflow-y', 'overflow-x'}.intersection(rule.declarations))


class TestDashboardScrollTarget(unittest.TestCase):
    def test_native_action_class_and_keyboard_focus_target_remain_on_dashboard_root(self):
        tree = ET.parse(ADDONS / 'jakh_core/static/src/dashboard.xml')
        template = tree.find("t[@t-name='jakh_core.Dashboard']")
        main = template.find('main')
        self.assertIsNotNone(main)
        self.assertTrue({'jakh-dashboard', 'o_action'}.issubset(main.get('class', '').split()))
        self.assertEqual(main.get('tabindex'), '-1')
        self.assertIsNotNone(main.find("section[@aria-labelledby='jakh-modules-heading']"))
        self.assertIsNotNone(main.find(".//section[@aria-labelledby='jakh-overview-heading']"))
        self.assertIsNotNone(main.find(".//footer[@class='jakh-dashboard-footer']"))


if __name__ == '__main__':
    unittest.main()
