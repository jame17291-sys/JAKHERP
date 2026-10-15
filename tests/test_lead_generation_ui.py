"""Presentation checks complement native view and server-policy tests."""
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET


BRANDING = Path(__file__).resolve().parents[1] / 'addons/jakh_branding'


class TestLeadGenerationUi(unittest.TestCase):
    def test_generation_component_is_removed_from_both_crm_toolbars(self):
        root = ET.parse(BRANDING / 'static/src/frontend_cleanup.xml').getroot()
        for native_template in ('crm.List.Buttons', 'crm.Kanban.Buttons'):
            with self.subTest(template=native_template):
                extension = root.find(f"t[@t-inherit='{native_template}']")
                self.assertIsNotNone(extension)
                self.assertEqual(extension.get('t-inherit-mode'), 'extension')
                specifications = extension.findall('xpath')
                self.assertEqual(len(specifications), 1)
                self.assertEqual(specifications[0].attrib,
                                 {'expr': '//t[LeadGenerationDropdown]', 'position': 'replace'})
                self.assertFalse(list(specifications[0]))
                # No New/import controls or native list/kanban actions replaced.
                self.assertEqual(len(list(extension)), 1)

    def test_old_partial_dropdown_patch_is_removed(self):
        source = (BRANDING / 'static/src/frontend_cleanup.js').read_text()
        self.assertNotIn('LeadGenerationDropdown', source)
        self.assertNotIn('Mail Plugins', source)
        self.assertIn('patch(SettingsPage.prototype', source)
        self.assertIn('patch(SettingsApp.prototype', source)

    def test_late_views_hide_feature_without_deleting_technical_fields(self):
        root = ET.parse(BRANDING / 'views/lead_generation_disabled.xml').getroot()
        views = {record.get('id'): record for record in root.findall('record')}
        self.assertEqual(set(views), {'settings_without_lead_generation',
                                     'crm_form_without_lead_enrichment',
                                     'lead_mining_form_history_only',
                                     'lead_mining_list_history_only'})
        settings = views['settings_without_lead_generation']
        form = views['crm_form_without_lead_enrichment']
        self.assertEqual(settings.find("field[@name='inherit_id']").get('ref'),
                         'crm.res_config_settings_view_form')
        self.assertEqual(form.find("field[@name='inherit_id']").get('ref'),
                         'crm_iap_enrich.crm_lead_view_form')
        self.assertEqual([node.get('expr') for node in settings.findall("field[@name='arch']/xpath")],
                         ["//block[@name='convert_visitor_setting_container']",
                          "//block[@name='generate_lead_setting_container']"])
        self.assertEqual([node.get('expr') for node in form.findall("field[@name='arch']/xpath")],
                         ["(//button[@name='iap_enrich'])[1]", "(//button[@name='iap_enrich'])[2]"])
        for view in (settings, form):
            self.assertGreaterEqual(int(view.find("field[@name='priority']").text), 99)
            for spec in view.findall("field[@name='arch']/xpath"):
                self.assertEqual(spec.get('position'), 'attributes')
                self.assertEqual(len(list(spec)), 1)
                self.assertEqual(spec[0].tag, 'attribute')
                self.assertEqual(spec[0].attrib, {'name': 'invisible'})
                self.assertEqual(spec[0].text, '1')

    def test_old_mining_urls_keep_readable_history_without_workflow_controls(self):
        root = ET.parse(BRANDING / 'views/lead_generation_disabled.xml').getroot()
        for kind, attributes in (('form', {'create', 'edit', 'delete', 'duplicate'}),
                                 ('list', {'create', 'edit', 'delete', 'import'})):
            view = root.find(f"record[@id='lead_mining_{kind}_history_only']")
            self.assertEqual(view.find("field[@name='model']").text, 'crm.iap.lead.mining.request')
            self.assertGreaterEqual(int(view.find("field[@name='priority']").text), 99)
            spec = view.find(f"field[@name='arch']/xpath[@expr='//{kind}']")
            self.assertEqual(spec.get('position'), 'attributes')
            self.assertEqual({item.get('name') for item in spec}, attributes)
            self.assertEqual({item.text for item in spec}, {'0'})
        form = root.find("record[@id='lead_mining_form_history_only']")
        controls = form.findall("field[@name='arch']/xpath")[1:]
        self.assertEqual({item.get('expr') for item in controls}, {
            "(//button[@name='action_submit'])[1]", "(//button[@name='action_submit'])[2]",
            "(//button[@name='action_submit'])[3]", "//button[@name='action_buy_credits']",
        })
        for spec in controls:
            self.assertEqual(spec.get('position'), 'attributes')
            self.assertEqual(len(list(spec)), 1)
            self.assertEqual(spec[0].attrib, {'name': 'invisible'})
            self.assertEqual(spec[0].text, '1')
        # No record-link or field is removed: historical leads stay reachable.
        self.assertNotIn('action_get_opportunity_action', ET.tostring(form, encoding='unicode'))
        self.assertNotIn('action_get_lead_action', ET.tostring(form, encoding='unicode'))


if __name__ == '__main__':
    unittest.main()
