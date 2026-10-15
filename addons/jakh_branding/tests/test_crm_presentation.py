from types import SimpleNamespace

from lxml import html

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'jakh')
class CrmPresentationTests(TransactionCase):
    """Check effective native views and helpers without creating CRM records."""

    def test_crm_and_reporting_views_show_actual_records_only(self):
        views = (
            ('crm.crm_case_tree_view_leads', 'list'),
            ('crm.view_crm_lead_kanban', 'kanban'),
            ('crm.crm_case_kanban_view_leads', 'kanban'),
            ('crm.crm_case_tree_view_oppor', 'list'),
            ('crm.crm_lead_view_graph', 'graph'),
            ('crm.crm_lead_view_graph_forecast', 'graph'),
            ('crm.crm_lead_view_pivot', 'pivot'),
            ('crm.crm_lead_view_pivot_forecast', 'pivot'),
            ('crm.crm_opportunity_report_view_pivot', 'pivot'),
            ('crm.crm_opportunity_report_view_pivot_lead', 'pivot'),
            ('crm.crm_opportunity_report_view_graph', 'graph'),
            ('crm.crm_opportunity_report_view_graph_lead', 'graph'),
            ('crm.crm_activity_report_view_graph', 'graph'),
            ('crm.crm_activity_report_view_pivot', 'pivot'),
        )
        self.assertEqual(len(views), 14)
        for xmlid, root_tag in views:
            with self.subTest(view=xmlid):
                arch = self.env.ref(xmlid)._get_combined_arch()
                self.assertEqual(arch.tag, root_tag)
                self.assertEqual(arch.get('sample'), '0')
                self.assertTrue(arch.xpath('.//field'), 'Native fields and measures remain available')

        # These primary views inherit a modified base instead of declaring a
        # sample attribute themselves; they must also receive the policy.
        for xmlid in ('crm.crm_lead_view_kanban_forecast',
                      'crm.crm_lead_view_tree_opportunity_reporting'):
            with self.subTest(inherited_view=xmlid):
                self.assertEqual(self.env.ref(xmlid)._get_combined_arch().get('sample'), '0')

    def test_crm_form_hides_migration_tab_preserves_business_fields(self):
        arch = self.env.ref('crm.crm_lead_view_form').with_context(lang='en_US')._get_combined_arch()
        self.assertFalse(arch.xpath("//page[@name='jakh_source']"))
        website = arch.xpath("//field[@name='website']")
        self.assertTrue(website)
        for field in website:
            self.assertEqual(field.get('placeholder'), 'https://example.com')
        for name in ('name', 'partner_id', 'expected_revenue', 'probability', 'description'):
            self.assertTrue(arch.xpath(f"//field[@name='{name}']"), name)
        self.assertTrue(arch.xpath("//button[@name='action_jakh_new_estimate']"))
        self.assertTrue(arch.xpath("//button[@name='action_jakh_estimates']"))
        # Removing presentation must not delete source provenance used by the
        # reviewed import process or remove its financial field protection.
        fields = self.env['crm.lead']._fields
        for name in ('jakh_source_system', 'jakh_source_id', 'jakh_source_currency_id',
                     'jakh_source_amount', 'jakh_source_payload'):
            self.assertIn(name, fields)
            self.assertTrue(fields[name].readonly, name)
        self.assertTrue(fields['jakh_source_amount'].groups)
        self.assertTrue(fields['jakh_source_currency_id'].groups)
        self.assertTrue(fields['jakh_source_payload'].groups)

    def test_dynamic_empty_help_is_clear_for_leads_and_opportunities(self):
        for lead_type in ('lead', 'opportunity'):
            with self.subTest(lead_type=lead_type):
                rendered = self.env['crm.lead'].with_context(
                    lang='en_US', default_type=lead_type,
                ).get_empty_list_help('')
                text = html.fromstring(str(rendered)).text_content()
                self.assertIn('No leads or opportunities match this view', text)
                self.assertIn('Create a lead or opportunity with New, import reviewed records, or adjust your filters.', text)
                self.assertNotIn('playing with', text.lower())
                self.assertNotIn('test the email gateway', text.lower())
                self.assertNotIn('Odoo', text)

    def test_team_empty_help_preserves_configured_email_link(self):
        for address in (False, 'enquiries@example.com'):
            with self.subTest(alias=address):
                rendered = self.env['ir.qweb'].with_context(lang='en_US')._render(
                    'crm.crm_action_helper', {'team': SimpleNamespace(alias_email=address)},
                )
                document = html.fromstring(str(rendered))
                text = document.text_content()
                self.assertIn('No leads or opportunities match this view', text)
                self.assertIn('Create a lead or opportunity with New, import reviewed records, or adjust your filters.', text)
                self.assertNotIn('playing with', text.lower())
                self.assertNotIn('test the email gateway', text.lower())
                links = document.xpath('//a[starts-with(@href, "mailto:")]')
                if address:
                    self.assertEqual(len(links), 1)
                    self.assertEqual(links[0].get('href'), f'mailto:{address}')
                    self.assertEqual(links[0].text_content(), address)
                else:
                    self.assertFalse(links)
