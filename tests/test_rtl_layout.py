"""Exercise the real native RTL compiler; no database or generated assets touched.

Set JAKH_NATIVE_SOURCE to the pinned vendor source directory. The compiler defaults
to the locally installed, reviewed deployment dependency; JAKH_RTLCSS may override
its executable path. Missing optional tooling is reported as a skip, never a pass.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from test_label_layout import compiled_rules, sass


PACKAGE = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get(
    'JAKH_RTLCSS', str(PACKAGE / 'deploy/rtlcss/node_modules/.bin/rtlcss'),
))
NATIVE_SOURCE = Path(os.environ['JAKH_NATIVE_SOURCE']) if os.environ.get('JAKH_NATIVE_SOURCE') else None
CONFIG = NATIVE_SOURCE / 'odoo/addons/base/data/rtlcss.json' if NATIVE_SOURCE else None
READY = sass is not None and COMPILER.is_file() and CONFIG is not None and CONFIG.is_file()


@unittest.skipUnless(READY, 'Requires libsass, actual rtlcss, and JAKH_NATIVE_SOURCE')
class TestNativeRtlLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_rules = {}
        cls.rtl_rules = {}
        for name, relative in {
            'theme': 'addons/jakh_core/static/src/theme.scss',
            'sidebar': 'addons/jakh_branding/static/src/sidebar.scss',
        }.items():
            css = sass.compile(filename=str(PACKAGE / relative), output_style='expanded')
            cls.source_rules[name] = tuple(compiled_rules(css))
            cls.rtl_rules[name] = tuple(compiled_rules(cls.convert(css)))

    @staticmethod
    def convert(css):
        result = subprocess.run(
            [str(COMPILER), '-c', str(CONFIG), '-'], input=css,
            capture_output=True, text=True, check=True, timeout=30,
        )
        return result.stdout

    def declarations(self, rules, selector, media=()):
        matching = [rule for rule in rules if selector in rule.selectors and rule.media == media]
        self.assertTrue(matching, f'Missing actual compiled selector: {selector!r} {media!r}')
        result = {}
        for rule in matching:
            for name, declaration in rule.declarations.items():
                if name not in result or declaration[1] or not result[name][1]:
                    result[name] = declaration
        return result

    def test_actual_compiler_matches_the_reviewed_version(self):
        expected = json.loads((PACKAGE / 'deploy/rtlcss/package.json').read_text())['jakhRtlcssVersion']
        locked = json.loads((PACKAGE / 'deploy/rtlcss/package-lock.json').read_text())
        self.assertEqual(locked['packages']['node_modules/rtlcss']['version'], expected)
        result = subprocess.run([str(COMPILER), '--version'], capture_output=True,
                                text=True, check=True, timeout=30)
        self.assertEqual(result.stdout.strip(), f'rtlcss version: {expected}')

    def test_logical_edges_and_start_alignment_are_not_mirrored_twice(self):
        checked = 0
        for name, rules in self.source_rules.items():
            for rule in rules:
                logical = {
                    key: value for key, value in rule.declarations.items()
                    if '-inline' in key or '-block' in key or
                    (key == 'text-align' and value[0] in ('start', 'end'))
                }
                for selector in rule.selectors:
                    converted = self.declarations(self.rtl_rules[name], selector, rule.media)
                    for key, value in logical.items():
                        self.assertEqual(converted.get(key), value, (selector, key))
                        checked += 1
        self.assertGreaterEqual(checked, 20)
        tile = self.declarations(self.rtl_rules['theme'], '.jakh-module')
        self.assertEqual(tile['padding-inline'], ('18px 40px', False))
        self.assertNotIn('padding', tile, 'Physical padding would be mirrored in addition to logical padding')

    def test_mobile_drawer_transform_flips_once_and_open_position_stays_zero(self):
        media = ('(max-width: 767px)',)
        for selector in ('.jakh_sidebar', '.jakh_sidebar.is-collapsed'):
            original = self.declarations(self.source_rules['sidebar'], selector, media)
            converted = self.declarations(self.rtl_rules['sidebar'], selector, media)
            self.assertEqual(original['transform'], ('translateX(-100%)', False))
            self.assertEqual(converted['transform'], ('translateX(100%)', False))
            opened = self.declarations(self.rtl_rules['sidebar'], f'{selector}.is-open', media)
            self.assertEqual(opened['transform'], ('translateX(0)', False))
        fixed = self.declarations(self.rtl_rules['sidebar'], '.jakh_sidebar')
        self.assertEqual(fixed['inset-inline-start'], ('0', False))
        self.assertFalse({'left', 'right'}.intersection(fixed))

    def test_dashboard_scroll_and_natural_flow_rules_survive_rtl_unchanged(self):
        # Scroll containers and natural-flow geometry stay direction-neutral.
        # Icons may legitimately have physical anchors mirrored by the compiler.
        for rule in self.source_rules['theme']:
            for selector in rule.selectors:
                if (selector.startswith('.jakh-dashboard') or
                        selector.startswith('body.o_web_client > .o_action_manager > .jakh-dashboard') or
                        selector in ('.jakh-module-grid', '.jakh-kpi-grid')):
                    self.assertEqual(
                        self.declarations(self.rtl_rules['theme'], selector, rule.media),
                        self.declarations(self.source_rules['theme'], selector, rule.media),
                    )
        for selector in ('.jakh_sidebar_navigation', '.jakh_sidebar_brand', '.jakh_sidebar_footer'):
            self.assertEqual(
                self.declarations(self.rtl_rules['sidebar'], selector),
                self.declarations(self.source_rules['sidebar'], selector),
            )

    def test_module_icon_anchor_mirrors_while_native_glyph_direction_stays_ltr(self):
        # Native .oi glyph shaping is deliberately LTR even on an RTL page.
        # A logical inset on the icon therefore resolves against the wrong
        # direction; its physical anchor must be mirrored by the native compiler.
        selector = '.jakh-module > i'
        original = self.declarations(self.source_rules['theme'], selector)
        converted = self.declarations(self.rtl_rules['theme'], selector)
        self.assertEqual(original.get('right'), ('16px', False))
        self.assertNotIn('left', original)
        self.assertEqual(converted.get('left'), ('16px', False))
        self.assertNotIn('right', converted)
        for declarations in (original, converted):
            self.assertEqual(declarations.get('position'), ('absolute', False))
            self.assertEqual(declarations.get('top'), ('20px', False))
            self.assertFalse({'inset', 'inset-inline', 'inset-inline-start',
                              'inset-inline-end', 'direction'}.intersection(declarations))

        native = NATIVE_SOURCE / 'odoo/addons/web/static/src/webclient'
        fixture = ''.join(f'@import "{native / relative}";\n' for relative in
                          ('icons_mappings/rtl_icons.scss', 'icons.scss'))
        ltr = sass.compile(string=fixture, output_style='expanded')
        for css in (ltr, self.convert(ltr)):
            # Isolate the unchanged native base rule; unrelated icon keyframes
            # are outside this declaration-block parser's intentional scope.
            match = re.search(r'(?m)^\.oi \{[^{}]*\}', css)
            self.assertIsNotNone(match, 'Native icon base rule changed; review the fixture')
            declarations = self.declarations(tuple(compiled_rules(match.group(0))), '.oi')
            self.assertEqual(declarations.get('direction'), ('ltr', False))

    def test_native_webclient_and_action_direction_really_become_rtl(self):
        web = NATIVE_SOURCE / 'odoo/addons/web/static'
        # Compile unchanged native mixins/layout, with the ordinary screen
        # breakpoint map. Isolate the native base client block from unrelated
        # component styles; fixture values below do not affect direction.
        client = (web / 'src/webclient/webclient.scss').read_text()
        match = re.search(r'(?m)^\.o_web_client \{[^{}]*\}', client)
        self.assertIsNotNone(match, 'Native base webclient rule changed; review the fixture')
        fixture = '''
            $o-webclient-media: screen;
            $grid-breakpoints: (xs: 0, sm: 576px, md: 768px, lg: 992px, xl: 1200px, xxl: 1400px);
            $o-webclient-background-color: #f6f8f7;
            $o-webclient-color-scheme: light;
        '''
        for relative in ('lib/bootstrap/scss/mixins/_breakpoints.scss',
                         'src/scss/utils.scss', 'src/webclient/webclient_layout.scss'):
            fixture += f'\n@import "{web / relative}";\n'
        fixture += match.group(0)
        ltr = sass.compile(string=fixture, output_style='expanded')
        before = tuple(compiled_rules(ltr))
        after = tuple(compiled_rules(self.convert(ltr)))
        for selector in ('.o_web_client', 'html .o_web_client > .o_action_manager',
                         'html .o_web_client .o_content',
                         'html .o_web_client .o_content > .o_view_controller'):
            self.assertEqual(self.declarations(before, selector)['direction'], ('ltr', False))
            self.assertEqual(self.declarations(after, selector)['direction'], ('rtl', False))
        native_action = 'html .o_web_client > .o_action_manager > .o_action'
        self.assertEqual(self.declarations(before, native_action), self.declarations(after, native_action))


if __name__ == '__main__':
    unittest.main()
