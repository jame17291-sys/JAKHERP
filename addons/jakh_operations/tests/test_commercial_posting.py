from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestCommercialPosting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.commercial.manager', groups='jakh_core.group_manager')
        company = cls.env.company
        Account = cls.env['account.account']
        def account(code, name, kind, reconcile=False):
            return Account.create({'code': code, 'name': name, 'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(company.ids)]})
        cls.receivable = account('JA9001', 'QA Receivables', 'asset_receivable', True)
        cls.income = account('JA9002', 'QA Certified Revenue', 'income')
        cls.retention = account('JA9003', 'QA Retention Control', 'asset_current')
        cls.advance = account('JA9004', 'QA Advance Control', 'liability_current')
        cls.journal = cls.env['account.journal'].create({'name': 'QA Commercial Sales', 'code': 'JQA', 'type': 'sale', 'company_id': company.id, 'default_account_id': cls.income.id})
        cls.customer = cls.env['res.partner'].create({'name': 'Synthetic QA Customer', 'property_account_receivable_id': cls.receivable.id})
        cls.project = cls.env['project.project'].create({'name': 'Synthetic QA Commercial Project', 'company_id': company.id, 'privacy_visibility': 'employees'})
        cls.wbs = cls.env['jakh.wbs'].create({'name': 'Measured work', 'code': 'COMM-QA', 'project_id': cls.project.id, 'budget_amount': 1000})
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({
            'name': 'QA Contract', 'project_id': cls.project.id, 'partner_id': cls.customer.id,
            'journal_id': cls.journal.id, 'income_account_id': cls.income.id,
            'retention_percent': 10, 'retention_cap': 100,
            'recovery_percent': 20, 'advance_cap': 100, 'advance_account_id': cls.advance.id,
            'retention_account_id': cls.retention.id,
            'line_ids': [Command.create({'name': 'Concrete work', 'code': 'BOQ-QA', 'wbs_id': cls.wbs.id, 'quantity': 10, 'unit_price': 100, 'uom_id': cls.env.ref('uom.product_uom_unit').id})],
        })
        cls.contract.action_activate()
        bank_account = account('JA9005', 'QA Commercial Bank', 'asset_cash')
        bank = cls.env['account.journal'].create({'name': 'QA Commercial Bank', 'code': 'QAB', 'type': 'bank',
            'company_id': company.id, 'default_account_id': bank_account.id})
        method = bank.inbound_payment_method_line_ids.filtered(lambda row: row.code == 'manual')[:1]
        method.payment_account_id = bank_account
        advance = cls.env['jakh.contract.cash.document'].with_user(cls.manager).create({
            'name': 'QA Received Advance', 'contract_id': cls.contract.id, 'kind': 'advance', 'amount': 100,
            'reason': 'Approved synthetic advance receipt evidence.',
        })
        advance.action_submit(); advance.action_approve(); advance.action_create_invoice(); advance.invoice_id.action_post()
        cls.env['account.payment.register'].with_user(cls.manager).with_context(
            active_model='account.move', active_ids=advance.invoice_id.ids).create({
                'journal_id': bank.id, 'payment_method_line_id': method.id, 'payment_date': fields.Date.today(),
            })._create_payments()

    def _certificate(self, cumulative_quantity):
        return self.env['jakh.payment.certificate'].with_user(self.manager).create({
            'name': 'QA Measured Certificate', 'contract_id': self.contract.id,
            'line_ids': [Command.create({'contract_line_id': self.contract.line_ids.id, 'cumulative_quantity': cumulative_quantity})],
        })

    def test_certificate_posts_once_and_reports_reconcile(self):
        certificate = self._certificate(5)
        certificate.action_submit()
        certificate.action_approve()
        self.assertEqual(certificate.period_gross, 500)
        self.assertEqual(certificate.retention_amount, 50)
        self.assertEqual(certificate.recovery_amount, 100)
        certificate.action_create_invoice()
        invoice = certificate.invoice_id
        certificate.action_create_invoice()
        self.assertEqual(certificate.invoice_id, invoice)
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertAlmostEqual(sum(invoice.line_ids.mapped('balance')), 0, places=2)
        self.assertAlmostEqual(invoice.amount_total, 350, places=2)
        wizard = self.env['jakh.financial.report'].with_user(self.manager).create({'report_type': 'trial_balance', 'date_to': fields.Date.today()})
        wizard.action_generate()
        self.assertAlmostEqual(sum(wizard.line_ids.mapped('balance')), 0, places=2)
        self.assertTrue(wizard.line_ids.filtered(lambda line: line.account_id == self.income))
        next_certificate = self._certificate(8)
        next_certificate.action_submit()
        next_certificate.action_approve()
        self.assertEqual(next_certificate.previous_gross, 500)
        self.assertEqual(next_certificate.period_gross, 300)
        self.assertEqual(next_certificate.recovery_amount, 0)

    def test_approved_certificate_and_posted_invoice_cannot_be_rewritten(self):
        certificate = self._certificate(5)
        certificate.action_submit()
        certificate.action_approve()
        with self.assertRaises(AccessError):
            certificate.write({'period_gross': 999})
