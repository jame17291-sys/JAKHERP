"""Contract-specific CRM presentation must not fabricate or remove data."""
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET

import polib


BRANDING = Path(__file__).resolve().parents[1] / 'addons/jakh_branding'
EXPECTED_NATIVE_VIEWS = {
    'crm.crm_case_tree_view_leads': ('list', 'crm.lead'),
    'crm.view_crm_lead_kanban': ('kanban', 'crm.lead'),
    'crm.crm_case_kanban_view_leads': ('kanban', 'crm.lead'),
    'crm.crm_case_tree_view_oppor': ('list', 'crm.lead'),
    'crm.crm_lead_view_graph': ('graph', 'crm.lead'),
    'crm.crm_lead_view_graph_forecast': ('graph', 'crm.lead'),
    'crm.crm_lead_view_pivot': ('pivot', 'crm.lead'),
    'crm.crm_lead_view_pivot_forecast': ('pivot', 'crm.lead'),
    'crm.crm_opportunity_report_view_pivot': ('pivot', 'crm.lead'),
    'crm.crm_opportunity_report_view_pivot_lead': ('pivot', 'crm.lead'),
    'crm.crm_opportunity_report_view_graph': ('graph', 'crm.lead'),
    'crm.crm_opportunity_report_view_graph_lead': ('graph', 'crm.lead'),
    'crm.crm_activity_report_view_graph': ('graph', 'crm.activity.report'),
    'crm.crm_activity_report_view_pivot': ('pivot', 'crm.activity.report'),
}


class TestCrmPresentation(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(BRANDING / 'views/crm_presentation.xml').getroot()

    def test_sample_rows_disabled_across_crm_and_reports_without_other_view_changes(self):
        overrides = {}
        for record in self.root.findall('record'):
            if not record.get('id').endswith('_without_sample'):
                continue
            parent = record.find("field[@name='inherit_id']").get('ref')
            self.assertNotIn(parent, overrides)
            overrides[parent] = record
        self.assertEqual(set(overrides), set(EXPECTED_NATIVE_VIEWS))
        for parent, (tag, model) in EXPECTED_NATIVE_VIEWS.items():
            with self.subTest(view=parent):
                record = overrides[parent]
                self.assertEqual(record.get('model'), 'ir.ui.view')
                self.assertEqual(record.find("field[@name='model']").text, model)
                self.assertGreaterEqual(int(record.find("field[@name='priority']").text), 99)
                arch = record.find("field[@name='arch']")
                self.assertEqual(len(list(arch)), 1)
                self.assertEqual(arch[0].attrib, {'expr': f'//{tag}', 'position': 'attributes'})
                self.assertEqual(len(list(arch[0])), 1)
                self.assertEqual(arch[0][0].attrib, {'name': 'sample'})
                self.assertEqual(arch[0][0].text, '0')
                # No field, filter, grouping, permission or business action is replaced.
                self.assertEqual(arch[0][0].tag, 'attribute')

    def test_website_example_changes_placeholder_only(self):
        record = self.root.find("record[@id='crm_neutral_website_placeholder']")
        self.assertEqual(record.find("field[@name='inherit_id']").get('ref'), 'crm.crm_lead_view_form')
        arch = record.find("field[@name='arch']")
        self.assertEqual(len(list(arch)), 1)
        self.assertEqual(arch[0].attrib, {'expr': "//field[@name='website']", 'position': 'attributes'})
        self.assertEqual(arch[0][0].attrib, {'name': 'placeholder'})
        self.assertEqual(arch[0][0].text, 'https://example.com')

    def test_professional_empty_help_preserves_team_alias_without_test_language(self):
        helper = self.root.find("template[@id='crm_professional_empty_help']")
        self.assertEqual(helper.get('inherit_id'), 'crm.crm_action_helper')
        specs = helper.findall('xpath')
        self.assertEqual({spec.get('expr') for spec in specs},
                         {"//t[@t-if='team.alias_email']", '//t[@t-else]'})
        alias_branch = specs[0].find('t')
        self.assertEqual(alias_branch.get('t-if'), 'team.alias_email')
        link = alias_branch.find('.//a')
        self.assertEqual(link.get('t-attf-href'), 'mailto:#{team.alias_email}')
        self.assertEqual(link.find('t').get('t-out'), 'team.alias_email')
        copy = ''.join(helper.itertext())
        self.assertNotIn('playing', copy)
        self.assertNotIn('test the email gateway', copy)
        self.assertIn('No leads or opportunities match this view', copy)
        self.assertIn('import reviewed records, or adjust your filters.', copy)

    def test_new_help_copy_has_native_arabic_translations(self):
        catalog = {entry.msgid: entry for entry in polib.pofile(BRANDING / 'i18n/ar.po')}
        for message in (
            'No leads or opportunities match this view',
            'Create a lead or opportunity with New, import reviewed records, or adjust your filters.',
            'Use the configured team email to record an enquiry:',
        ):
            entry = catalog[message]
            self.assertFalse(entry.fuzzy)
            self.assertRegex(entry.msgstr, '[\u0600-\u06ff]')
            self.assertIn(('model_terms:ir.ui.view,arch_db:jakh_branding.crm_professional_empty_help', ''),
                          entry.occurrences)


if __name__ == '__main__':
    unittest.main()
