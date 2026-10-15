"""Compiled label/layout regressions; data fields keep their native wrapping."""
from dataclasses import dataclass
from pathlib import Path
import re
import unittest
from xml.etree import ElementTree as ET

try:
    import sass
except ImportError:
    sass = None


BRANDING = Path(__file__).resolve().parents[1] / 'addons/jakh_branding'
MOBILE = ('(max-width: 767px)',)
SIDEBAR_LABEL = '.jakh_sidebar_link .jakh_sidebar_label'
FORM_LABEL = 'body.o_web_client.jakh-sidebar-enabled .o_form_view .o_wrap_label'


@dataclass
class CssRule:
    selectors: tuple
    declarations: dict
    media: tuple


def compiled_rules(css, media=()):
    """Parse this compiled sheet's declaration blocks and nested media rules.

    Unlike searching the SCSS source, this checks fully expanded selectors and
    distinguishes desktop declarations from mobile/print overrides. This sheet
    has no string-valued brace/semicolon content or other at-rule kinds.
    """
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    position = 0
    while css[position:].strip():
        opening = css.index('{', position)
        header = ' '.join(css[position:opening].split())
        depth, closing = 1, opening + 1
        while depth:
            if closing >= len(css):
                raise AssertionError('Unbalanced compiled CSS')
            depth += (css[closing] == '{') - (css[closing] == '}')
            closing += 1
        body = css[opening + 1:closing - 1]
        if header.startswith('@media '):
            yield from compiled_rules(body, (*media, header[len('@media '):]))
        else:
            if header.startswith('@') or '{' in body:
                raise AssertionError(f'Unexpected compiled rule type: {header}')
            declarations = {}
            for declaration in body.split(';'):
                if not declaration.strip():
                    continue
                name, value = declaration.split(':', 1)
                value = ' '.join(value.split())
                important = value.endswith('!important')
                if important:
                    value = value[:-len('!important')].rstrip()
                declarations[name.strip()] = (value, important)
            yield CssRule(tuple(selector.strip() for selector in header.split(',')), declarations, media)
        position = closing


@unittest.skipUnless(sass is not None, 'Optional libsass compiler is not installed')
class TestCompiledLabelLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        css = sass.compile(filename=str(BRANDING / 'static/src/sidebar.scss'), output_style='expanded')
        cls.rules = tuple(compiled_rules(css))

    def declarations(self, selector, media=()):
        matching = [rule for rule in self.rules if rule.media == media and selector in rule.selectors]
        self.assertTrue(matching, f'Missing compiled selector {selector!r} in {media!r}')
        result = {}
        for rule in matching:
            for name, declaration in rule.declarations.items():
                if name not in result or declaration[1] or not result[name][1]:
                    result[name] = declaration
        return result

    def assert_declarations(self, selector, expected, media=()):
        actual = self.declarations(selector, media)
        for name, declaration in expected.items():
            self.assertEqual(actual.get(name), declaration, f'{selector}: {name} in {media!r}')

    def test_desktop_width_and_readable_link_metrics(self):
        self.assert_declarations(':root', {
            '--jakh-sidebar-width': ('256px', False),
            '--jakh-sidebar-rail': ('70px', False),
        })
        self.assert_declarations('body.o_web_client.jakh-sidebar-enabled', {
            'padding-inline-start': ('var(--jakh-sidebar-width)', False),
        })
        self.assert_declarations('.jakh_sidebar', {'width': ('var(--jakh-sidebar-width)', False)})
        self.assert_declarations('.jakh_sidebar_link', {
            'font-size': ('13px', False), 'min-height': ('42px', False),
            'text-align': ('start', False),
        })
        self.assert_declarations('.jakh_sidebar_children .jakh_sidebar_link', {
            'font-size': ('12px', False), 'min-height': ('36px', False),
        })

    def test_sidebar_labels_wrap_at_words_without_clipping(self):
        self.assert_declarations(SIDEBAR_LABEL, {
            'white-space': ('normal', False), 'overflow-wrap': ('normal', False),
            'word-break': ('normal', False), 'hyphens': ('none', False),
        })
        clipping = {'overflow', 'overflow-x', 'overflow-y', 'text-overflow',
                    'line-clamp', '-webkit-line-clamp', 'max-height', 'height'}
        for rule in self.rules:
            if any(selector.endswith('.jakh_sidebar_label') for selector in rule.selectors):
                self.assertFalse(clipping.intersection(rule.declarations), rule)
                for name in ('overflow-wrap', 'word-wrap', 'word-break'):
                    if name in rule.declarations:
                        self.assertEqual(rule.declarations[name][0], 'normal', rule)
                if 'white-space' in rule.declarations:
                    self.assertEqual(rule.declarations['white-space'][0], 'normal', rule)

    def test_nested_indentation_is_compact_and_capped_using_logical_properties(self):
        self.assert_declarations('.jakh_sidebar_children', {
            'margin-inline-start': ('8px', True), 'padding-inline-start': ('4px', True),
        })
        self.assert_declarations('.jakh_sidebar_children .jakh_sidebar_children', {
            'margin-inline-start': ('6px', True),
        })
        deepest = '.jakh_sidebar_children .jakh_sidebar_children .jakh_sidebar_children'
        self.assert_declarations(deepest, {
            'margin-inline-start': ('0', True), 'padding-inline-start': ('0', True),
            'border-inline-start': ('0', False),
        })
        for selector in ('.jakh_sidebar_children', '.jakh_sidebar_children .jakh_sidebar_children', deepest):
            declarations = self.declarations(selector)
            self.assertFalse({'margin-left', 'margin-right', 'padding-left', 'padding-right'}.intersection(declarations))

    def test_mobile_drawer_and_collapsed_rail_remain_responsive(self):
        self.assert_declarations('.jakh_sidebar.is-collapsed', {'width': ('var(--jakh-sidebar-rail)', False)})
        self.assert_declarations('body.o_web_client.jakh-sidebar-collapsed', {
            'padding-inline-start': ('var(--jakh-sidebar-rail)', False),
        })
        self.assert_declarations('.jakh_sidebar.is-collapsed .jakh_sidebar_label', {'display': ('none', False)})
        for selector in ('.jakh_sidebar', '.jakh_sidebar.is-collapsed'):
            self.assert_declarations(selector, {
                'width': ('280px', False), 'max-width': ('calc(100vw - 40px)', False),
            }, MOBILE)
            self.assert_declarations(f'{selector} .jakh_sidebar_label', {'display': ('revert', False)}, MOBILE)
        for selector in ('body.o_web_client.jakh-sidebar-enabled', 'body.o_web_client.jakh-sidebar-collapsed'):
            self.assert_declarations(selector, {'padding-inline-start': ('0', False)}, MOBILE)

    def test_form_wrapping_override_is_label_only_and_preserves_native_sizing(self):
        self.assert_declarations(FORM_LABEL, {
            'overflow-wrap': ('normal', True), 'word-break': ('normal', True),
            'hyphens': ('none', False),
        })
        # Do not override native grid min-content sizing or mobile floating
        # labels' nowrap/ellipsis treatment while fixing emergency word breaks.
        forbidden_layout = {'width', 'min-width', 'max-width', 'white-space', 'display',
                            'grid-template-columns', 'flex-basis', 'overflow', 'text-overflow'}
        self.assertFalse(forbidden_layout.intersection(self.declarations(FORM_LABEL)))
        wrapping = {'overflow-wrap', 'word-wrap', 'word-break', 'hyphens'}
        for rule in self.rules:
            if wrapping.intersection(rule.declarations):
                self.assertFalse(rule.media, 'Wrapping policy must not diverge across screen sizes')
                self.assertTrue(set(rule.selectors).issubset({FORM_LABEL, SIDEBAR_LABEL}), rule)

    def test_subtotal_label_uses_space_or_wraps_row_without_restyling_amounts(self):
        row = ('body.o_web_client.jakh-sidebar-enabled .o_form_view '
               '.oe_subtotal_footer .o_cell_custom > .d-flex:has(> .o_form_label)')
        label = f'{row} > .o_form_label'
        self.assertEqual(self.declarations(row), {
            'flex-wrap': ('wrap', False), 'row-gap': ('4px', True),
        })
        self.assertEqual(self.declarations(label), {
            'flex-shrink': ('0', False), 'white-space': ('normal', False),
            'max-width': ('100%', False),
        })
        # Only the native custom subtotal row and its direct label may change.
        # Its monetary widget, alignment, currency and numeric text are intact.
        subtotal_selectors = {
            selector
            for rule in self.rules
            for selector in rule.selectors
            if '.oe_subtotal_footer' in selector
        }
        self.assertEqual(subtotal_selectors, {row, label})

    def test_settings_quarter_width_labels_stack_without_changing_inputs(self):
        selector = ('body.o_web_client.jakh-sidebar-enabled .o_form_view '
                    '.o_setting_box .row > .o_form_label.col-3')
        self.assertEqual(self.declarations(selector), {
            'flex-basis': ('100%', False), 'width': ('100%', False),
            'white-space': ('normal', True), 'margin-inline': ('0', True),
        })
        settings_selectors = {
            candidate
            for rule in self.rules
            for candidate in rule.selectors
            if '.o_setting_box' in candidate
        }
        self.assertEqual(settings_selectors, {selector}, 'Only direct Settings labels may receive the stacking override')


class TestSidebarAccessibleLabels(unittest.TestCase):
    def test_native_full_names_remain_visible_and_accessible(self):
        tree = ET.parse(BRANDING / 'static/src/sidebar.xml')
        labels = [node for node in tree.iter('span') if node.get('class') == 'jakh_sidebar_label']
        self.assertEqual(len(labels), 3)
        for label in labels:
            outputs = [label.get('t-out')] if label.get('t-out') else [node.get('t-out') for node in label.iter('t')]
            self.assertEqual(outputs, ['menu.name'], 'Never shorten, slice or substitute a native menu name')
        named_buttons = [button for button in tree.iter('button') if button.get('t-att-aria-label') == 'menu.name']
        self.assertEqual(len(named_buttons), 2)
        for button in named_buttons:
            self.assertEqual(button.get('t-att-title'), 'menu.name')
            self.assertEqual(button.get('class'), 'jakh_sidebar_link')
            self.assertIsNotNone(button.find("span[@t-out='menu.name']"))
        branches = [button for button in named_buttons if button.get('t-if')]
        self.assertEqual(branches[0].get('t-att-aria-expanded'), "this.isExpanded(menu) ? 'true' : 'false'")
        self.assertEqual(branches[0].get('t-att-aria-controls'), "'jakh-menu-' + menu.id")


if __name__ == '__main__':
    unittest.main()
