"""ORM tests require a verified Odoo 20 installation and PostgreSQL."""
from odoo import Command
from lxml import etree
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'jakh')
class TestEstimateWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        common = {'company_id': cls.company.id, 'company_ids': [Command.set(cls.company.ids)]}
        cls.manager = cls.env['res.users'].create({**common, 'name': 'Construction manager test', 'login': 'jakh-estimate-manager-test', 'group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('jakh_core.group_manager').id])]})
        cls.estimator = cls.env['res.users'].create({**common, 'name': 'Estimator test', 'login': 'jakh-estimator-test', 'group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('jakh_core.group_estimator').id])]})
        cls.technical = cls.env['res.users'].create({**common, 'name': 'Technical test', 'login': 'jakh-estimate-technical-test', 'group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('jakh_core.group_technical').id])]})
        cls.partner = cls.env['res.partner'].create({'name': 'Illustrative estimate customer', 'company_id': cls.company.id})
        cls.product = cls.env['product.product'].create({'name': 'Illustrative BOQ service', 'type': 'service', 'uom_id': cls.env.ref('uom.product_uom_unit').id, 'taxes_id': [Command.clear()]})
        cls.cost_code = cls.env['jakh.cost.code'].with_user(cls.manager).create({'name': 'Illustrative civil code', 'code': 'EST-TEST-CIV', 'company_id': cls.company.id})
        cls.env['product.pricelist'].create({'name': 'Estimate test company currency', 'currency_id': cls.company.currency_id.id, 'company_id': cls.company.id})

    def _estimate(self, user=None):
        estimate = self.env['jakh.estimate'].with_user(user or self.estimator).create({'title': 'Illustrative 11-building estimate', 'partner_id': self.partner.id, 'company_id': self.company.id, 'unit_count': 11, 'header_markup_percent': 8, 'line_ids': [Command.create({'code': '01.02.003', 'name': 'Illustrative concrete scope', 'cost_code_id': self.cost_code.id, 'product_id': self.product.id, 'uom_id': self.product.uom_id.id, 'quantity': 10, 'unit_cost': 100, 'item_markup_percent': 3})]})
        return estimate

    def _approve(self, estimate):
        estimate.action_submit()
        estimate.with_user(self.manager).action_approve()
        return estimate.with_user(self.manager)

    def test_costing_versions_and_approval_lock(self):
        estimate = self._estimate()
        self.assertAlmostEqual(estimate.cost_amount, 11000)
        self.assertAlmostEqual(estimate.sell_amount, 12236.4)
        estimate.action_save_version()
        estimate.line_ids.write({'quantity': 12})
        estimate.action_save_version()
        latest = estimate.version_ids.sorted('number')[-1]
        self.assertEqual(latest.comparison_json['modified'][0]['changed_fields'], ['quantity'])
        estimate = self._approve(estimate)
        with self.assertRaises(UserError):
            estimate.line_ids.write({'quantity': 13})
        with self.assertRaises(UserError):
            estimate.unlink()
        with self.assertRaises(AccessError):
            latest.write({'name': 'Altered audit evidence'})
        with self.assertRaises(AccessError):
            estimate.write({'state': 'draft'})

    def test_building_overrides_and_revision_remapping(self):
        estimate = self._estimate()
        self.env['jakh.estimate.building'].with_user(self.estimator).create({'estimate_id': estimate.id, 'number': 1, 'name': 'Villa 01', 'override_ids': [Command.create({'line_id': estimate.line_ids.id, 'quantity': 0})]})
        self.assertEqual(estimate.line_ids.total_quantity, 100)
        original_uuid = estimate.line_ids.line_uuid
        estimate = self._approve(estimate)
        action = estimate.action_new_revision()
        revision = self.env['jakh.estimate'].with_user(self.estimator).browse(action['res_id'])
        self.assertEqual(revision.state, 'draft')
        self.assertEqual(revision.line_ids.line_uuid, original_uuid)
        self.assertEqual(revision.building_ids.override_ids.line_id.estimate_id, revision)
        self.assertEqual(revision.line_ids.total_quantity, 100)
        self.assertEqual(estimate.state, 'approved')

    def test_handover_is_native_draft_and_idempotent(self):
        estimate = self._approve(self._estimate())
        first = estimate.action_handover()
        second = estimate.action_handover()
        self.assertEqual(first['res_id'], second['res_id'])
        self.assertEqual(estimate.sale_order_id.state, 'draft')
        self.assertEqual(estimate.project_id.jakh_estimate_id, estimate)
        self.assertEqual(estimate.sale_order_id.order_line.product_uom_qty, 110)
        wbs = self.env['jakh.wbs'].with_user(self.manager).search([('project_id', '=', estimate.project_id.id)])
        self.assertEqual(len(wbs), 1)
        self.assertEqual(wbs.state, 'draft')
        self.assertEqual(wbs.budget_amount, 11000)
        self.assertEqual(wbs.jakh_estimate_line_id, estimate.line_ids)
        self.assertTrue(wbs.jakh_sale_line_id)
        self.assertEqual(self.env['sale.order'].search_count([('jakh_estimate_id', '=', estimate.id)]), 1)

    def test_permissions_and_financial_field_restrictions(self):
        estimate = self._estimate()
        with self.assertRaises(AccessError):
            estimate.write({'state': 'approved'})
        estimate.action_submit()
        with self.assertRaises(AccessError):
            estimate.action_approve()
        estimate.with_user(self.manager).action_approve()
        with self.assertRaises(AccessError):
            estimate.action_handover()
        technical_line = estimate.line_ids.with_user(self.technical)
        self.assertEqual(technical_line.read(['quantity'])[0]['quantity'], 10)
        with self.assertRaises(AccessError):
            technical_line.read(['unit_cost'])
        with self.assertRaises(AccessError):
            estimate.with_user(self.technical).read(['sell_amount'])

    def test_foreign_currency_handover_is_blocked_without_relabelling(self):
        foreign = self.env.ref('base.USD') if self.company.currency_id != self.env.ref('base.USD') else self.env.ref('base.EUR')
        estimate = self._estimate()
        estimate.write({'currency_id': foreign.id})
        estimate = self._approve(estimate)
        with self.assertRaises(UserError):
            estimate.action_handover()
        self.assertFalse(estimate.sale_order_id)
        self.assertEqual(estimate.currency_id, foreign)

    def test_native_crm_preserves_source_currency_and_fields(self):
        source_currency = self.env.ref('base.USD')
        lead = self.env['crm.lead'].with_user(self.manager).create({'name': 'Original CRM pursuit', 'type': 'opportunity', 'partner_id': self.partner.id, 'company_id': self.company.id, 'expected_revenue': 1000, 'jakh_source_system': 'JAKH CRM', 'jakh_source_id': 'SOURCE-TEST-1', 'jakh_source_currency_id': source_currency.id, 'jakh_source_amount': 500, 'jakh_source_payload': {'sourceId': 'SOURCE-TEST-1', 'currency': 'USD', 'amount': 500}})
        action = lead.action_jakh_new_estimate()
        estimate = self.env['jakh.estimate'].with_user(self.manager).browse(action['res_id'])
        self.assertEqual(estimate.opportunity_id, lead)
        self.assertEqual(estimate.currency_id, self.company.currency_id)
        self.assertEqual(lead.jakh_source_currency_id, source_currency)
        self.assertEqual(lead.jakh_source_amount, 500)
        self.assertEqual(lead.expected_revenue, 1000)
        with self.assertRaises(AccessError):
            lead.write({'jakh_source_amount': 999})

    def test_opportunity_form_hides_migration_metadata_but_keeps_estimating(self):
        form = self.env['crm.lead'].with_user(self.manager).get_view(
            self.env.ref('crm.crm_lead_view_form').id, 'form')
        arch = etree.fromstring(form['arch'])
        self.assertFalse(arch.xpath("//page[@name='jakh_source']"))
        self.assertFalse(arch.xpath("//field[starts-with(@name, 'jakh_source_')]"))
        self.assertTrue(arch.xpath("//button[@name='action_jakh_new_estimate']"))
        self.assertTrue(arch.xpath("//button[@name='action_jakh_estimates']"))
        self.assertIn('jakh_source_payload', self.env['crm.lead']._fields)

    def test_estimate_prerequisites_use_business_guidance(self):
        leads = self.env['crm.lead'].with_user(self.manager).with_context(lang='en_US')
        lead = leads.create({'name': 'Estimate prerequisite test', 'type': 'lead',
                             'company_id': self.company.id})
        with self.assertRaisesRegex(UserError, 'Convert the lead to an opportunity'):
            lead.action_jakh_new_estimate()
        lead.write({'type': 'opportunity'})
        with self.assertRaisesRegex(UserError, 'Select a customer'):
            lead.action_jakh_new_estimate()
