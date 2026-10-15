from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.lead_generation import LeadMiningRequest


@tagged('post_install', '-at_install', 'jakh')
class LeadGenerationRemovalTests(TransactionCase):
    def test_generation_menus_and_bound_enrichment_action_removed(self):
        owner = new_test_user(self.env, 'jakh_no_generation_owner', groups='jakh_core.group_platform_owner')
        menus = self.env['ir.ui.menu'].with_user(owner)
        root = self.env.ref('crm_iap_mine.crm_menu_lead_generation')
        hidden = menus.search([('id', 'child_of', root.id)])
        for debug in (False, True):
            payload = menus.load_menus(debug)
            self.assertFalse(set(hidden.ids).intersection(payload))
            self.assertIn(self.env.ref('crm.crm_menu_root').id, payload)
        self.assertFalse(self.env.ref('crm_iap_enrich.action_enrich_mail').binding_model_id)

    def test_settings_and_form_hide_retired_controls_keep_native_fields(self):
        arch = self.env.ref('base.res_config_settings_view_form')._get_combined_arch()
        for name in ('convert_visitor_setting_container', 'generate_lead_setting_container'):
            self.assertEqual(arch.xpath(f"//block[@name='{name}']")[0].get('invisible'), '1')
        for name in ('lead_enrich_auto', 'module_crm_iap_enrich', 'module_crm_iap_mine', 'auth_totp_enforce'):
            self.assertTrue(arch.xpath(f"//field[@name='{name}']"), name)
        form = self.env.ref('crm.crm_lead_view_form')._get_combined_arch()
        for button in form.xpath("//button[@name='iap_enrich']"):
            self.assertEqual(button.get('invisible'), '1')

    def test_policy_is_idempotent_audited_and_preserves_existing_leads(self):
        lead = self.env['crm.lead'].create({'name': 'Preserved customer opportunity', 'type': 'opportunity', 'email_from': 'buyer@example.com'})
        before = lead.read(['name', 'email_from', 'type', 'iap_enrich_done'])
        parameters = self.env['ir.config_parameter'].sudo()
        parameters.set_bool('jakh.crm.lead_generation.disabled', False)
        audit = self.env['jakh.audit.event']
        domain = [('operation', '=', 'disable_lead_generation')]
        count = audit.search_count(domain)
        requests_before = self.env['crm.iap.lead.mining.request'].search_count([])
        self.env['res.users']._jakh_disable_lead_generation()
        self.env['res.users']._jakh_disable_lead_generation()
        self.assertEqual(audit.search_count(domain), count + 1)
        self.assertEqual(lead.read(['name', 'email_from', 'type', 'iap_enrich_done']), before)
        self.assertEqual(self.env['crm.iap.lead.mining.request'].search_count([]), requests_before)
        self.assertFalse(self.env.ref('crm_iap_enrich.ir_cron_lead_enrichment').active)
        self.assertEqual(parameters.get_str('crm.iap.lead.enrich.setting'), 'manual')

    def test_privileged_direct_generation_and_enrichment_calls_are_blocked(self):
        lead = self.env['crm.lead'].sudo().create({'name': 'RPC guard check', 'email_from': 'buyer@example.com'})
        before = lead.read(['name', 'email_from', 'iap_enrich_done'])
        with patch('odoo.addons.iap.tools.iap_tools.iap_jsonrpc') as network:
            for action in (lead.action_generate_leads, lead.iap_enrich, lead._process_leads):
                with self.assertRaises(UserError):
                    action()
            lead._iap_enrich_leads_cron(enrich_hours_delay=999, batch_size=1)
            network.assert_not_called()
        self.assertFalse(lead.show_enrich_button)
        self.assertEqual(lead.read(['name', 'email_from', 'iap_enrich_done']), before)

    def test_mining_create_submit_retry_and_network_paths_blocked(self):
        requests = self.env['crm.iap.lead.mining.request'].sudo()
        # Seed a pre-removal historical record through the native ORM, below
        # this feature's create guard. It remains inside the rolled-back test
        # transaction; native ORM access checks and validations still run.
        request = super(LeadMiningRequest, requests).create([{'name': 'Historical request', 'state': 'error'}])
        count = requests.search_count([])
        with patch('odoo.addons.iap.tools.iap_tools.iap_jsonrpc') as network:
            with self.assertRaises(UserError):
                requests.create({'lead_number': 1})
            for action in (request.action_submit, request.action_draft, request.action_buy_credits,
                           request._perform_request, lambda: request._iap_contact_mining({})):
                with self.assertRaises(UserError):
                    action()
            network.assert_not_called()
        self.assertEqual(requests.search_count([]), count)
        self.assertEqual(request.name, 'Historical request')
        self.assertEqual(request.state, 'error')

    def test_manual_leads_and_native_import_remain_available(self):
        with patch('odoo.addons.iap.tools.iap_tools.iap_jsonrpc') as network:
            lead = self.env['crm.lead'].create({'name': 'Manual construction opportunity', 'type': 'opportunity'})
            lead.write({'name': 'Updated construction opportunity'})
            result = self.env['crm.lead'].load(['name', 'type'], [['Imported construction lead', 'lead']])
            self.assertTrue(result['ids'], result.get('messages'))
            self.assertEqual(lead.name, 'Updated construction opportunity')
            self.assertEqual(self.env['crm.lead'].browse(result['ids']).name, 'Imported construction lead')
            network.assert_not_called()

    def test_settings_cannot_reactivate_feature_and_staff_cannot_apply_policy(self):
        settings = self.env['res.config.settings'].create({'lead_enrich_auto': 'auto'})
        with self.assertRaises(UserError):
            settings.set_values()
        self.assertFalse(self.env.ref('crm_iap_enrich.ir_cron_lead_enrichment').active)
        settings.write({'lead_enrich_auto': 'manual', 'module_website_crm_iap_reveal': True})
        with self.assertRaises(UserError):
            settings.set_values()
        staff = new_test_user(self.env, 'jakh_no_generation_staff', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['res.users'].with_user(staff)._jakh_disable_lead_generation()
