from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from .test_subcontract_workflow import TestSubcontractWorkflow
from ..models.subcontract_cash_workflow import AccountMoveLine as GuardedControlLine


@tagged('post_install', '-at_install', 'jakh')
class TestSubcontractCashWorkflow(TransactionCase):
    _order = TestSubcontractWorkflow._order
    _ses = TestSubcontractWorkflow._ses
    _bill = TestSubcontractWorkflow._bill

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='subcontract.cash.manager', groups='jakh_core.group_manager')
        cls.company = cls.env.company
        def account(code, name, account_type, reconcile=False):
            return cls.env['account.account'].create({'code': code, 'name': name, 'account_type': account_type,
                'reconcile': reconcile, 'company_ids': [Command.set(cls.company.ids)]})
        cls.payable = account('JSA9001', 'Subcontract cash payable', 'liability_payable', True)
        cls.expense = account('JSA9002', 'Subcontract cash job cost', 'expense')
        cls.retention = account('JSA9003', 'Subcontract retention held', 'liability_current')
        cls.advance = account('JSA9004', 'Subcontract prepaid advance', 'asset_prepayments')
        cls.bank = account('JSA9005', 'Subcontract bank cash', 'asset_cash', True)
        cls.journal = cls.env['account.journal'].create({'name': 'Subcontract cash AP', 'code': 'JSA', 'type': 'purchase',
            'company_id': cls.company.id, 'default_account_id': cls.expense.id})
        cls.bank_journal = cls.env['account.journal'].create({'name': 'Subcontract cash bank', 'code': 'JSB', 'type': 'bank',
            'company_id': cls.company.id, 'default_account_id': cls.bank.id})
        (cls.bank_journal.outbound_payment_method_line_ids | cls.bank_journal.inbound_payment_method_line_ids).write({'payment_account_id': cls.bank.id})
        cls.vendor = cls.env['res.partner'].create({'name': 'Subcontract cash vendor', 'property_account_payable_id': cls.payable.id})
        cls.service = cls.env['product.product'].create({'name': 'Subcontract cash work', 'type': 'service',
            'purchase_ok': True, 'purchase_method': 'receive', 'property_account_expense_id': cls.expense.id})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Subcontract cash project',
            'company_id': cls.company.id, 'user_id': cls.manager.id})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Subcontract cash WBS',
            'code': 'SC-CASH-01', 'project_id': cls.project.id, 'budget_amount': 300})
        cls.wbs.action_release()

    def _agreement(self, order):
        agreement = self.env['jakh.subcontract.finance.agreement'].with_user(self.manager).create({
            'name': 'Approved subcontract cash terms', 'purchase_order_id': order.id, 'journal_id': self.journal.id,
            'retention_percent': 10, 'retention_cap': 20, 'advance_percent': 20, 'advance_cap': 40,
            'recovery_percent': 20, 'retention_account_id': self.retention.id, 'advance_account_id': self.advance.id,
        })
        agreement.action_activate()
        return agreement

    def _cash_document(self, agreement, kind, amount):
        document = self.env['jakh.subcontract.cash.document'].with_user(self.manager).create({
            'name': 'Approved subcontract cash payment', 'agreement_id': agreement.id,
            'document_type': kind, 'amount': amount, 'reason': 'Approved finance evidence and contractual terms.',
        })
        document.action_submit()
        return document

    def _pay(self, invoice):
        method = (self.bank_journal.inbound_payment_method_line_ids if invoice.move_type == 'in_refund'
                  else self.bank_journal.outbound_payment_method_line_ids)[:1]
        register = self.env['account.payment.register'].with_user(self.manager).with_context(
            active_model='account.move', active_ids=invoice.ids).create({
                'journal_id': self.bank_journal.id, 'payment_method_line_id': method.id,
                'payment_date': fields.Date.today(), 'amount': abs(invoice.amount_residual),
            })
        payments = register._create_payments()
        self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
        self.assertTrue(payments)
        return payments

    def _certificate(self, agreement, line, quantity=10):
        ses = self._ses(line, quantity); ses.action_approve()
        action = ses.action_create_bill()
        self.assertEqual(action['res_model'], 'jakh.subcontract.payment.certificate')
        certificate = self.env['jakh.subcontract.payment.certificate'].with_user(self.manager).browse(action['res_id'])
        self.assertEqual(certificate.agreement_id, agreement)
        certificate.action_submit(); certificate.action_approve()
        return certificate

    def test_paid_advance_ses_certificate_retention_release_reconciles_native_ap(self):
        order = self._order(); agreement = self._agreement(order)
        advance = self._cash_document(agreement, 'advance', 40)
        advance.action_approve(); advance.action_create_bill(); advance.invoice_id.action_post()
        self.assertEqual(agreement._available_paid_advance(), 0, 'A posted unpaid bill is not cash advanced.')
        self._pay(advance.invoice_id)
        self.assertEqual(agreement._available_paid_advance(), 40)
        certificate = self._certificate(agreement, order.order_line)
        self.assertEqual(certificate.gross_amount, 200)
        self.assertEqual(certificate.retention_amount, 20)
        self.assertEqual(certificate.recovery_amount, 40)
        self.assertEqual(certificate.net_before_tax, 140)
        certificate.action_create_bill(); first_bill = certificate.invoice_id
        certificate.action_create_bill(); self.assertEqual(certificate.invoice_id, first_bill)
        self.assertEqual(certificate.service_entry_id.invoice_id, first_bill)
        first_bill.action_post(); self._pay(first_bill)
        self.assertEqual(agreement._account_balances(), (20, 0))
        release = self._cash_document(agreement, 'release', 20)
        release.action_approve(); release.action_create_bill(); release.invoice_id.action_post()
        self._pay(release.invoice_id)
        self.assertEqual(agreement._account_balances(), (0, 0))
        expense_lines = (advance.invoice_id | first_bill | release.invoice_id).line_ids.filtered(lambda line: line.account_id == self.expense)
        self.assertEqual(sum(expense_lines.mapped('balance')), 200)
        self.assertEqual(self.env['jakh.project.control.entry']._sum(self.wbs, ['actual_cost']), 200)
        self.assertFalse(agreement._closing_obstacles())
        agreement.action_close(); self.assertEqual(agreement.state, 'closed')

    def test_caps_and_unpaid_advance_do_not_forge_recovery_or_release(self):
        order = self._order(); agreement = self._agreement(order)
        excess = self._cash_document(agreement, 'advance', 41)
        with self.assertRaises(UserError):
            excess.action_approve()
        premature = self._cash_document(agreement, 'release', 1)
        with self.assertRaises(UserError):
            premature.action_approve()
        advance = self._cash_document(agreement, 'advance', 40)
        advance.action_approve(); advance.action_create_bill(); advance.invoice_id.action_post()
        certificate = self._certificate(agreement, order.order_line)
        self.assertEqual(certificate.recovery_amount, 0)
        with self.assertRaises(AccessError):
            certificate.write({'recovery_amount': 40})
        with self.assertRaises(UserError):
            agreement.action_close()
        additional = self._cash_document(agreement, 'advance', 1)
        with self.assertRaises(UserError):
            additional.action_approve()

    def test_agreement_blocks_manual_po_bill_and_tampered_control_lines(self):
        order = self._order(); agreement = self._agreement(order)
        certificate = self._certificate(agreement, order.order_line)
        manual = self._bill(order.order_line, 10)
        with self.assertRaises(UserError):
            manual.action_post()
        certificate.action_create_bill()
        retention = certificate.invoice_id.invoice_line_ids.filtered(lambda line: line.jakh_subcontract_control == 'retention')
        retention.write({'price_unit': -19})
        with self.assertRaises(UserError):
            certificate.invoice_id.action_post()
        retention.write({'price_unit': -20})
        gross = certificate.invoice_id.invoice_line_ids.filtered(lambda line: line.jakh_subcontract_control == 'gross')
        gross.write({'account_id': self.advance.id})
        with self.assertRaises(UserError):
            certificate.invoice_id.action_post()
        gross.write({'account_id': self.expense.id})
        certificate.invoice_id.action_post()
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.manager).create({'move_type': 'in_invoice',
                'partner_id': self.vendor.id, 'jakh_subcontract_certificate_id': certificate.id})

    def test_native_certificate_credit_restores_controls_and_preserves_history(self):
        order = self._order(); agreement = self._agreement(order)
        advance = self._cash_document(agreement, 'advance', 40)
        advance.action_approve(); advance.action_create_bill(); advance.invoice_id.action_post(); self._pay(advance.invoice_id)
        certificate = self._certificate(agreement, order.order_line)
        certificate.action_create_bill(); bill = certificate.invoice_id; bill.action_post()
        credit = bill._reverse_moves(default_values_list=[{'date': fields.Date.today(), 'invoice_date': fields.Date.today()}])
        credit.action_post()
        self.assertEqual(agreement._account_balances(), (0, 40))
        self.assertEqual(self.env['jakh.project.control.entry']._sum(self.wbs, ['actual_cost']), 0)
        payables = (bill | credit).line_ids.filtered(lambda line: line.account_id.account_type == 'liability_payable' and not line.reconciled)
        if payables:
            payables.reconcile()
        certificate.action_cancel()
        self.assertEqual(certificate.state, 'cancelled')
        self.assertEqual(bill.state, 'posted')
        self.assertEqual(credit.state, 'posted')
        self.assertTrue(agreement._closing_obstacles(), 'Outstanding cash advanced still blocks final closure.')

    def test_general_journal_cannot_manufacture_retention_liability(self):
        agreement = self._agreement(self._order())
        journal = self.env['account.journal'].create({'name': 'Subcontract abuse proof', 'code': 'JSX',
            'type': 'general', 'company_id': self.company.id})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['account.move'].with_user(self.manager).create({'move_type': 'entry', 'journal_id': journal.id,
                'line_ids': [Command.create({'name': 'Forged retained liability', 'account_id': self.retention.id,
                    'credit': 1000, 'jakh_subcontract_agreement_id': agreement.id, 'jakh_subcontract_control': 'retention'}),
                    Command.create({'name': 'Forged counterpart', 'account_id': self.expense.id, 'debit': 1000})]})
        # Simulate an unposted legacy row already carrying the old markers;
        # posting validation must reject it even though it is a general entry.
        legacy = self.env['account.move'].with_user(self.manager).create({'move_type': 'entry', 'journal_id': journal.id,
            'line_ids': [Command.create({'name': 'Legacy marker', 'account_id': self.retention.id, 'credit': 1000}),
                Command.create({'name': 'Legacy counterpart', 'account_id': self.expense.id, 'debit': 1000})]})
        marker = legacy.line_ids.filtered(lambda line: line.account_id == self.retention)
        super(GuardedControlLine, marker).write({'jakh_subcontract_agreement_id': agreement.id, 'jakh_subcontract_control': 'retention'})
        with self.assertRaises(UserError):
            legacy.action_post()
        self.assertEqual(agreement._account_balances(), (0, 0))
        release = self._cash_document(agreement, 'release', 1000)
        with self.assertRaises(UserError):
            release.action_approve()

    def test_posted_finance_and_credit_lines_are_sealed_but_native_payment_still_reconciles(self):
        order = self._order(); agreement = self._agreement(order)
        certificate = self._certificate(agreement, order.order_line)
        certificate.action_create_bill(); bill = certificate.invoice_id; bill.action_post()
        line = bill.invoice_line_ids.filtered(lambda item: item.jakh_subcontract_control == 'gross')
        for values in ({'price_unit': 21}, {'account_id': self.advance.id}, {'debit': 201}, {'amount_residual': 0}):
            with self.assertRaises(AccessError):
                line.write(values)
        with self.assertRaises(AccessError):
            bill.write({'date': fields.Date.today(), 'journal_id': self.journal.id})
        with self.assertRaises(AccessError):
            bill.write({'state': 'draft'})
        with self.assertRaises(AccessError):
            line.unlink()
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).create([
                {'move_id': bill.id, 'name': 'Balanced append debit', 'account_id': self.expense.id, 'debit': 1},
                {'move_id': bill.id, 'name': 'Balanced append credit', 'account_id': self.payable.id, 'credit': 1},
            ])
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).with_context(default_move_id=bill.id).create([
                {'name': 'Default-parent debit', 'account_id': self.expense.id, 'debit': 1},
                {'name': 'Default-parent credit', 'account_id': self.payable.id, 'credit': 1},
            ])
        self._pay(bill)
        self.assertTrue(bill.currency_id.is_zero(bill.amount_residual))
        credit = bill._reverse_moves(default_values_list=[{'date': fields.Date.today(), 'invoice_date': fields.Date.today()}])
        credit.action_post()
        with self.assertRaises(AccessError):
            credit.invoice_line_ids[:1].write({'price_unit': 999})
