from odoo import Command, fields
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestExecutionControls(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='execution.manager', groups='jakh_core.group_manager')
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Execution-control project', 'company_id': cls.env.company.id, 'user_id': cls.manager.id,
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Execution WBS', 'code': 'EXEC-01', 'project_id': cls.project.id, 'budget_amount': 2000,
        })
        cls.wbs.action_release()
        cls.vendor = cls.env['res.partner'].create({'name': 'Execution subcontractor'})
        cls.service = cls.env['product.product'].create({
            'name': 'Execution subcontract service', 'type': 'service', 'purchase_ok': True,
        })
        cls.cost_account = cls.env['account.account'].create({'name': 'Execution labour expense', 'code': 'EX901',
            'account_type': 'expense_direct_cost', 'company_ids': [Command.set(cls.env.company.ids)]})
        cls.clearing_account = cls.env['account.account'].create({'name': 'Execution cost clearing', 'code': 'EX902',
            'account_type': 'liability_current', 'company_ids': [Command.set(cls.env.company.ids)]})
        cls.cost_journal = cls.env['account.journal'].create({'name': 'Execution labour costs', 'code': 'EXC',
            'type': 'general', 'company_id': cls.env.company.id})

    def _confirmed_subcontract_order(self):
        request = self.env['jakh.purchase.request'].with_user(self.manager).create({
            'name': 'EXEC-SES-RQ', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'material_plan_reference': 'Approved subcontract work package', 'supplier_id': self.vendor.id,
            'line_ids': [Command.create({
                'name': 'Subcontract installation', 'product_id': self.service.id,
                'uom_id': self.service.uom_id.id, 'quantity': 10,
            })],
        })
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({
            'line_id': request.line_ids.id, 'unit_price': 20,
        })
        request.action_submit()
        request.action_approve()
        request.action_release()
        request.purchase_order_id.button_confirm()
        return request.purchase_order_id

    def test_ses_feeds_and_reports_are_traceable(self):
        order = self._confirmed_subcontract_order()
        service_entry = self.env['jakh.subcontract.service.entry'].with_user(self.manager).create({
            'name': 'SES-001', 'purchase_line_id': order.order_line.id, 'quantity': 4,
            'notes': 'Installed and inspected work package.',
        })
        service_entry.action_submit()
        service_entry.action_approve()
        self.assertEqual(service_entry.state, 'approved')
        self.assertEqual(service_entry.accepted_amount, 80)
        self.assertTrue(order.order_line.jakh_requires_ses)

        labour = self.env['jakh.project.actual.feed'].with_user(self.manager).create({
            'name': 'Jisr payroll feed', 'source_system': 'jisr', 'actual_type': 'labour',
            'external_id': 'JISR-EXEC-001', 'source_reference': 'Approved Jisr payroll batch 001',
            'payload_digest': 'sha256:test', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'resource_reference': 'Crew A', 'quantity': 16, 'unit_label': 'hours', 'amount': 200,
            'journal_id': self.cost_journal.id, 'expense_account_id': self.cost_account.id,
            'offset_account_id': self.clearing_account.id,
        })
        labour.action_submit()
        labour.action_accept()
        self.assertEqual(labour.state, 'accepted')

        progress = self.env['jakh.project.schedule.progress'].with_user(self.manager).create({
            'name': 'P6 status update', 'source_system': 'p6', 'external_id': 'P6-EXEC-001',
            'source_reference': 'P6 data date status 001', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'planned_value': 200, 'earned_value': 150, 'progress_percent': 75,
        })
        progress.action_submit()
        progress.action_accept()
        self.assertEqual(progress.state, 'accepted')

        cash = self.env['jakh.project.cash.forecast'].with_user(self.manager).create({
            'name': 'Subcontract cash out', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'cash_direction': 'out', 'amount': 125, 'basis': 'Approved service-entry payment plan',
            'source_reference': 'SES-001',
        })
        cash.action_submit()
        cash.action_approve()

        cvr = self.env['jakh.project.control.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'as_of_date': fields.Date.today(),
        })
        cvr.action_generate()
        line = cvr.line_ids
        self.assertEqual(line.received_cost, 80)
        self.assertEqual(line.actual_cost, 200)
        self.assertEqual(line.planned_value, 200)
        self.assertEqual(line.earned_value, 150)
        self.assertEqual(line.cost_performance_index, 0.75)
        self.assertEqual(line.schedule_performance_index, 0.75)
        self.assertEqual(line.forecast_cash_net, -125)

        cashflow = self.env['jakh.project.cashflow.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'date_from': fields.Date.today(), 'date_to': fields.Date.today(),
        })
        cashflow.action_generate()
        self.assertEqual(cashflow.line_ids.cash_out, 125)
        self.assertEqual(cashflow.line_ids.net_cash, -125)

        schedule = self.env['jakh.report.schedule'].with_user(self.manager).create({
            'name': 'Weekly execution CVR', 'project_id': self.project.id, 'report_type': 'cvr', 'frequency': 'weekly',
        })
        schedule.action_run_now()
        self.assertEqual(len(schedule.delivery_ids), 1)
        self.assertIn('actual_cost', schedule.delivery_ids.snapshot)

    def test_production_readiness_requires_evidence_and_measured_recovery(self):
        attachment = self.env['ir.attachment'].with_user(self.manager).create({
            'name': 'Acceptance evidence.txt', 'raw': b'controlled evidence',
            'mimetype': 'text/plain',
        })
        readiness = self.env['jakh.production.readiness'].with_user(self.manager).create({
            'name': 'UAE customer production acceptance', 'company_id': self.env.company.id,
            'customer_reference': 'tenant-uae-test', 'country': 'ae',
            'evidence_attachment_ids': [Command.link(attachment.id)],
            'tenant_isolation_verified': True, 'backup_restore_verified': True, 'sso_mfa_verified': True,
            'security_review_verified': True, 'finance_uat_verified': True, 'localization_verified': True,
            'zero_critical_high_defects': True, 'rpo_minutes': 60, 'rto_minutes': 480,
        })
        readiness.action_mark_ready()
        readiness.action_accept()
        self.assertEqual(readiness.state, 'accepted')
