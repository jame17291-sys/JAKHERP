"""A continuous project journey: source once, post once, settle, then close."""
from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestEndToEndContracting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.e2e.director', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='jakh.e2e.technical', groups='jakh_core.group_technical')
        cls.reviewer = new_test_user(cls.env, login='jakh.e2e.reviewer', groups='jakh_core.group_quality')
        cls.company = cls.env.company

        def account(code, name, kind, reconcile=False):
            return cls.env['account.account'].create({'code': code, 'name': name, 'account_type': kind,
                'reconcile': reconcile, 'company_ids': [Command.set(cls.company.ids)]})

        cls.ar = account('E2E901', 'E2E Receivables', 'asset_receivable', True)
        cls.ap = account('E2E902', 'E2E Payables', 'liability_payable', True)
        cls.income = account('E2E903', 'E2E Revenue', 'income')
        cls.expense = account('E2E904', 'E2E Project cost', 'expense_direct_cost')
        cls.clearing = account('E2E905', 'E2E Payroll/plant accrual', 'liability_current', True)
        cls.retention = account('E2E906', 'E2E Retention', 'asset_current')
        cls.advance = account('E2E907', 'E2E Advance liability', 'liability_current')
        cls.bank_account = account('E2E908', 'E2E Bank', 'asset_cash')
        cls.ap_retention = account('E2E909', 'E2E Subcontract retention liability', 'liability_current')
        cls.ap_advance = account('E2E910', 'E2E Subcontract prepaid advance', 'asset_current')
        cls.sales = cls.env['account.journal'].create({'name': 'E2E Sales', 'code': 'E2ES', 'type': 'sale',
            'company_id': cls.company.id, 'default_account_id': cls.income.id})
        cls.purchase = cls.env['account.journal'].create({'name': 'E2E Purchases', 'code': 'E2EP', 'type': 'purchase',
            'company_id': cls.company.id, 'default_account_id': cls.expense.id})
        cls.cost_journal = cls.env['account.journal'].create({'name': 'E2E Costs', 'code': 'E2EC', 'type': 'general', 'company_id': cls.company.id})
        cls.bank = cls.env['account.journal'].create({'name': 'E2E Bank', 'code': 'E2EB', 'type': 'bank',
            'company_id': cls.company.id, 'default_account_id': cls.bank_account.id})
        (cls.bank.inbound_payment_method_line_ids | cls.bank.outbound_payment_method_line_ids).write({'payment_account_id': cls.bank_account.id})
        cls.customer = cls.env['res.partner'].create({'name': 'Synthetic E2E Main Contractor Client', 'property_account_receivable_id': cls.ar.id})
        cls.vendor = cls.env['res.partner'].create({'name': 'Synthetic E2E Subcontractor', 'property_account_payable_id': cls.ap.id})
        cls.scope_product = cls.env['product.product'].create({'name': 'E2E measured scope', 'type': 'service',
            'uom_id': cls.env.ref('uom.product_uom_unit').id, 'taxes_id': [Command.clear()]})
        cls.sub_product = cls.env['product.product'].create({'name': 'E2E subcontract installation', 'type': 'service',
            'purchase_method': 'receive', 'property_account_expense_id': cls.expense.id, 'supplier_taxes_id': [Command.clear()]})
        cls.code = cls.env['jakh.cost.code'].with_user(cls.manager).create({'name': 'E2E Civil works', 'code': 'E2E-CIV', 'company_id': cls.company.id})
        cls.env['product.pricelist'].create({'name': 'E2E company currency', 'currency_id': cls.company.currency_id.id, 'company_id': cls.company.id})

    def _settle(self, invoice):
        direction = 'inbound' if invoice.move_type in ('out_invoice', 'in_refund') else 'outbound'
        methods = self.bank.inbound_payment_method_line_ids if direction == 'inbound' else self.bank.outbound_payment_method_line_ids
        method = methods.filtered(lambda row: row.code == 'manual')[:1]
        payments = self.env['account.payment.register'].with_user(self.manager).with_context(
            active_model='account.move', active_ids=invoice.ids).create({
                'journal_id': self.bank.id, 'payment_method_line_id': method.id, 'payment_date': fields.Date.today(),
            })._create_payments()
        self.assertTrue(payments.move_id.filtered(lambda move: move.state == 'posted'))
        self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
        return payments

    def test_opportunity_to_settled_final_account_without_reentry(self):
        if 'jakh.estimate' not in self.env or 'jakh_estimate_id' not in self.env['jakh.project.contract']._fields:
            self.skipTest('The complete programme includes the estimating/handover addons.')
        lead = self.env['crm.lead'].with_user(self.manager).create({'name': 'Synthetic E2E tender',
            'type': 'opportunity', 'partner_id': self.customer.id, 'company_id': self.company.id})
        estimate = self.env['jakh.estimate'].with_user(self.manager).browse(lead.action_jakh_new_estimate()['res_id'])
        estimate.write({'title': 'Synthetic complete project', 'unit_count': 1, 'header_markup_percent': 25,
            'line_ids': [Command.create({'code': 'E2E-001', 'name': 'Complete measured scope',
                'cost_code_id': self.code.id, 'product_id': self.scope_product.id, 'uom_id': self.scope_product.uom_id.id,
                'quantity': 10, 'unit_cost': 80})]})
        estimate.action_submit(); estimate.action_approve(); estimate.action_handover(); estimate.action_handover()
        contract = estimate.commercial_contract_id
        project = estimate.project_id
        project.write({'privacy_visibility': 'employees'})
        project.message_subscribe(partner_ids=(self.technical.partner_id | self.reviewer.partner_id).ids)
        wbs = project.jakh_wbs_ids
        self.assertEqual(len(wbs), 1)
        self.assertEqual(wbs.budget_amount, 800)
        self.assertEqual(contract.line_ids.quantity, 10)
        self.assertEqual(contract.line_ids.unit_price, 100)
        self.assertTrue(project.account_id)
        building = self.env['jakh.project.location'].with_user(self.manager).create({
            'name': 'E2E Building A', 'code': 'E2E-A', 'project_id': project.id})
        discipline = self.env['jakh.discipline'].with_user(self.manager).create({
            'name': 'E2E Civil', 'code': 'E2E-CIV', 'company_id': self.company.id})
        contract.line_ids.write({'location_id': building.id, 'discipline_id': discipline.id})
        wbs.action_release()
        estimate.sale_order_id.action_confirm()
        contract.write({'journal_id': self.sales.id, 'income_account_id': self.income.id,
            'retention_percent': 10, 'retention_cap': 100, 'retention_account_id': self.retention.id,
            'recovery_percent': 20, 'advance_cap': 100, 'advance_account_id': self.advance.id,
            'certificate_review_policy': 'technical_commercial_finance'})
        contract.action_activate()
        building_budget = self.env['jakh.boq.location.budget'].with_user(self.manager).create({
            'contract_line_id': contract.line_ids.id, 'reason': 'Reviewed original BOQ building baseline',
            'line_ids': [Command.create({'location_id': building.id, 'discipline_id': discipline.id,
                'quantity': 10, 'budget_amount': 800})]})
        building_budget.action_submit(); building_budget.action_approve()

        advance = self.env['jakh.contract.cash.document'].with_user(self.manager).create({
            'name': 'E2E mobilization advance', 'contract_id': contract.id, 'kind': 'advance', 'amount': 100,
            'reason': 'Approved mobilization funding.',
        })
        advance.action_submit(); advance.action_approve(); advance.action_create_invoice()
        advance.invoice_id.action_post(); self._settle(advance.invoice_id)

        request = self.env['jakh.purchase.request'].with_user(self.manager).create({
            'name': 'E2E installed work package', 'project_id': project.id, 'wbs_id': wbs.id,
            'supplier_id': self.vendor.id, 'material_plan_reference': 'Approved released work package',
            'line_ids': [Command.create({'name': 'Subcontract installation', 'product_id': self.sub_product.id,
                'uom_id': self.sub_product.uom_id.id, 'quantity': 10,
                'contract_line_id': contract.line_ids.id, 'location_id': building.id, 'discipline_id': discipline.id})]})
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({'line_id': request.line_ids.id, 'unit_price': 20})
        request.action_submit(); request.action_approve(); request.action_release()
        order = request.purchase_order_id; order.button_confirm()
        agreement = self.env['jakh.subcontract.finance.agreement'].with_user(self.manager).create({
            'name': 'E2E subcontract commercial terms', 'purchase_order_id': order.id, 'journal_id': self.purchase.id,
            'retention_percent': 10, 'retention_cap': 20, 'advance_percent': 20, 'advance_cap': 40,
            'recovery_percent': 20, 'retention_account_id': self.ap_retention.id, 'advance_account_id': self.ap_advance.id})
        agreement.action_activate()
        supplier_advance = self.env['jakh.subcontract.cash.document'].with_user(self.manager).create({
            'name': 'E2E subcontract mobilization advance', 'agreement_id': agreement.id,
            'document_type': 'advance', 'amount': 40, 'reason': 'Approved subcontract mobilization funding.'})
        supplier_advance.action_submit(); supplier_advance.action_approve(); supplier_advance.action_create_bill()
        supplier_advance.invoice_id.action_post(); self._settle(supplier_advance.invoice_id)
        ses = self.env['jakh.subcontract.service.entry'].with_user(self.manager).create({
            'name': 'E2E accepted subcontract work', 'purchase_line_id': order.order_line.id, 'quantity': 10,
            'notes': 'Completed installation measured and inspected.'})
        ses.action_submit(); ses.action_approve()
        supplier_certificate = self.env['jakh.subcontract.payment.certificate'].with_user(self.manager).browse(ses.action_create_bill()['res_id'])
        supplier_certificate.action_submit(); supplier_certificate.action_approve(); supplier_certificate.action_create_bill()
        self.assertEqual(supplier_certificate.gross_amount, 200)
        self.assertEqual(supplier_certificate.recovery_amount, 40)
        self.assertEqual(supplier_certificate.retention_amount, 20)
        self.assertEqual(supplier_certificate.net_before_tax, 140)
        ses.invoice_id.action_post(); self._settle(ses.invoice_id)
        self.assertEqual(agreement.advance_balance, 0)
        supplier_release = self.env['jakh.subcontract.cash.document'].with_user(self.manager).create({
            'name': 'E2E subcontract retained sum release', 'agreement_id': agreement.id,
            'document_type': 'release', 'amount': 20, 'reason': 'All subcontract completion conditions met.'})
        supplier_release.action_submit(); supplier_release.action_approve(); supplier_release.action_create_bill()
        supplier_release.invoice_id.action_post(); self._settle(supplier_release.invoice_id)
        self.assertEqual(agreement.retention_balance, 0)
        agreement.action_close()

        inbox = self.env['jakh.integration.event'].with_user(self.manager)
        for kind in ('jisr', 'p6'):
            mapping = self.env['jakh.integration.binding'].with_user(self.manager).create({
                'name': 'Reviewed E2E ' + kind, 'kind': kind, 'company_id': self.company.id,
                'source_project_ref': 'E2E-EXTERNAL-PROJECT', 'source_wbs_ref': 'E2E-EXTERNAL-WBS',
                'project_id': project.id, 'wbs_id': wbs.id, 'contract_line_id': contract.line_ids.id,
                'location_id': building.id, 'discipline_id': discipline.id})
            mapping.action_approve()
        data = {'version': 1, 'kind': 'jisr', 'external_id': 'E2E-JISR-1', 'company_id': self.company.id,
            'source_project_ref': 'E2E-EXTERNAL-PROJECT', 'source_wbs_ref': 'E2E-EXTERNAL-WBS',
            'data': {'source_reference': 'Approved labour allocation',
            'work_date': str(fields.Date.today()), 'resource_reference': 'Crew E2E', 'quantity': 8, 'amount': 120,
            'journal_id': self.cost_journal.id, 'expense_account_id': self.expense.id, 'offset_account_id': self.clearing.id}}
        event = inbox.ingest_source(data); self.assertEqual(inbox.ingest_source(data)['id'], event['id'])
        labour = self.env[event['business_model']].with_user(self.manager).browse(event['business_id'])
        labour.action_accept(); self.assertEqual(labour.account_move_id.state, 'posted')

        plant = self.env['jakh.site.resource'].with_user(self.manager).create({'name': 'E2E Excavator', 'kind': 'plant',
            'company_id': self.company.id, 'source_reference': 'PLANT-E2E', 'cost_journal_id': self.cost_journal.id,
            'expense_account_id': self.expense.id, 'clearing_account_id': self.clearing.id})
        start = datetime.combine(fields.Date.today(), datetime.min.time())
        allocation = self.env['jakh.resource.assignment'].with_user(self.manager).create({'name': 'E2E plant allocation',
            'resource_id': plant.id, 'project_id': project.id, 'wbs_id': wbs.id, 'site_reference': 'E2E Site',
            'contract_line_id': contract.line_ids.id, 'location_id': building.id, 'discipline_id': discipline.id,
            'start_at': start, 'end_at': start + timedelta(hours=8)})
        allocation.action_approve(); allocation.write({'actual_hours': 8, 'actual_cost': 80, 'completion_reference': 'Verified shift return'})
        allocation.action_prepare_actual(); allocation.actual_feed_id.action_accept()

        progress = inbox.ingest_source({'version': 1, 'kind': 'p6', 'external_id': 'E2E-P6-1', 'company_id': self.company.id,
            'source_project_ref': 'E2E-EXTERNAL-PROJECT', 'source_wbs_ref': 'E2E-EXTERNAL-WBS',
            'data': {'source_reference': 'P6 accepted data date',
            'as_of_date': str(fields.Date.today()), 'planned_value': 800, 'earned_value': 800, 'progress_percent': 100}})
        self.env[progress['business_model']].with_user(self.manager).browse(progress['business_id']).action_accept()
        forecast = self.env['jakh.project.forecast'].with_user(self.manager).create({'name': 'E2E final forecast',
            'wbs_id': wbs.id, 'forecast_final_cost': 400, 'basis': 'All commitments fulfilled and actual costs posted.'})
        forecast.action_submit(); forecast.action_approve()

        drawing = self.env['jakh.document'].with_user(self.technical).create({'name': 'E2E as-built drawing', 'code': 'E2E-ASB',
            'project_id': project.id, 'wbs_id': wbs.id, 'location_id': building.id, 'discipline_id': discipline.id})
        revision = self.env['jakh.document.revision'].with_user(self.technical).create({'document_id': drawing.id,
            'name': 'A', 'summary': 'As-built issue', 'reviewer_id': self.reviewer.id, 'due_date': fields.Date.today()})
        self.env['ir.attachment'].with_user(self.technical).create({'name': 'As-built evidence.txt', 'raw': b'Synthetic as-built evidence',
            'res_model': revision._name, 'res_id': revision.id})
        revision.action_submit(); revision.with_user(self.reviewer).action_approve()
        inspection = self.env['jakh.assurance.case'].with_user(self.manager).create({'name': 'E2E completion inspection',
            'kind': 'inspection', 'project_id': project.id, 'wbs_id': wbs.id, 'due_date': fields.Date.today(), 'description': '<p>Final site inspection</p>'})
        inspection.action_submit(); inspection.action_start(); inspection.write({'resolution': '<p>All work accepted</p>'}); inspection.action_resolve()
        self.env['ir.attachment'].with_user(self.manager).create({'name': 'Inspection evidence.txt', 'raw': b'Synthetic inspection evidence',
            'res_model': inspection._name, 'res_id': inspection.id})
        inspection.action_close()

        certificate = self.env['jakh.payment.certificate'].with_user(self.manager).create({'name': 'E2E final certificate', 'contract_id': contract.id})
        certificate.action_prepare_lines(); certificate.line_ids.write({'cumulative_quantity': 10})
        certificate.action_submit()
        certificate.write({'review_note': 'Return for final site-measurement evidence check.'})
        certificate.action_return(); certificate.action_submit()
        certificate.action_review_approve(); certificate.action_review_approve(); certificate.action_approve()
        self.assertEqual(certificate.review_cycle, 2)
        certificate.action_create_invoice(); certificate.invoice_id.action_post()
        self.assertEqual(certificate.period_gross, 1000)
        self.assertEqual(certificate.recovery_amount, 100)
        self.assertEqual(certificate.retention_amount, 100)
        self._settle(certificate.invoice_id)
        release = self.env['jakh.contract.cash.document'].with_user(self.manager).create({'name': 'E2E retained sum release',
            'contract_id': contract.id, 'kind': 'retention', 'amount': 100, 'reason': 'Completion and release conditions verified.'})
        release.action_submit(); release.action_approve(); release.action_create_invoice(); release.invoice_id.action_post(); self._settle(release.invoice_id)

        cvr = self.env['jakh.project.control.report'].with_user(self.manager).create({'project_id': project.id})
        cvr.action_generate()
        self.assertEqual(cvr.line_ids.actual_cost, 400)
        self.assertEqual(cvr.line_ids.billed_revenue, 1000)
        self.assertEqual(cvr.line_ids.forecast_margin, 600)
        items = self.env['jakh.project.control.entry'].with_user(self.manager).search([('project_id', '=', project.id), ('entry_type', '=', 'actual_cost')])
        self.assertTrue(all(item.account_move_line_id for item in items))
        self.assertEqual(sum(items.mapped('amount')), sum(items.account_move_line_id.mapped('balance')))
        self.assertEqual(items.contract_line_id, contract.line_ids)
        self.assertEqual(items.location_id, building)
        construction = self.env['jakh.construction.report'].with_user(self.manager).create({
            'project_id': project.id, 'location_id': building.id})
        construction.action_generate()
        self.assertEqual(sum(construction.cost_line_ids.mapped('original_budget')), 800)
        self.assertEqual(sum(construction.cost_line_ids.mapped('actual_cost')), 400)
        self.assertEqual(construction.unbilled_line_ids, self.env['jakh.unbilled.report.line'])
        report = self.env['jakh.financial.report'].with_user(self.manager).create({'report_type': 'trial_balance'})
        report.action_generate(); self.assertAlmostEqual(sum(report.line_ids.mapped('balance')), 0)

        schedule = self.env['jakh.report.schedule'].with_user(self.manager).create({'name': 'E2E weekly CVR', 'project_id': project.id, 'frequency': 'weekly'})
        schedule.action_run_now(); self.assertEqual(len(schedule.delivery_ids), 1)
        building_schedule = self.env['jakh.report.schedule'].with_user(self.manager).create({
            'name': 'E2E building cost report', 'project_id': project.id,
            'report_type': 'construction_cost', 'frequency': 'weekly'})
        building_schedule.action_run_now(); self.assertEqual(len(building_schedule.delivery_ids), 1)
        final = self.env['jakh.project.final.account'].with_user(self.manager).create({'name': 'E2E final account',
            'contract_id': contract.id, 'reason': 'Source-to-cash project evidence accepted.'})
        final.action_submit(); final.action_approve(); final.action_close()
        self.assertEqual(contract.state, 'closed')
        self.assertTrue(project.jakh_workflow_closed)
        self.assertFalse(project.active)
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['jakh.purchase.request'].with_user(self.manager).create({'name': 'Forbidden late work',
                'project_id': project.id, 'wbs_id': wbs.id, 'material_plan_reference': 'Post-close attempt'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            project.write({'active': True})
