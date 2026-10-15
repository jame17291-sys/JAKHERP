from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestCloseoutWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.closeout.manager', groups='jakh_core.group_manager')
        company = cls.env.company

        def account(code, name, kind, reconcile=False):
            return cls.env['account.account'].create({'code': code, 'name': name, 'account_type': kind,
                                                      'reconcile': reconcile, 'company_ids': [Command.set(company.ids)]})

        cls.receivable = account('JCL9001', 'Closeout receivables', 'asset_receivable', True)
        cls.income = account('JCL9002', 'Closeout revenue', 'income')
        cls.retention = account('JCL9003', 'Closeout retention', 'asset_current')
        cls.advance = account('JCL9004', 'Closeout advance liability', 'liability_current')
        cls.liquidity = account('JCL9005', 'Closeout bank', 'asset_cash')
        cls.sales_journal = cls.env['account.journal'].create({'name': 'Closeout sales', 'code': 'JCLS',
                                                              'type': 'sale', 'company_id': company.id,
                                                              'default_account_id': cls.income.id})
        cls.bank_journal = cls.env['account.journal'].create({'name': 'Closeout bank', 'code': 'JCLB',
                                                             'type': 'bank', 'company_id': company.id,
                                                             'default_account_id': cls.liquidity.id})
        cls.method = cls.bank_journal.inbound_payment_method_line_ids.filtered(lambda row: row.code == 'manual')[:1]
        cls.method.payment_account_id = cls.liquidity
        cls.customer = cls.env['res.partner'].create({'name': 'Closeout customer', 'property_account_receivable_id': cls.receivable.id})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Complete closeout project',
                                                                               'company_id': company.id,
                                                                               'user_id': cls.manager.id})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Closeout work', 'code': 'CL-01',
                                                                   'project_id': cls.project.id, 'budget_amount': 1000})
        cls.wbs.action_release()
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({
            'name': 'Complete closeout contract', 'project_id': cls.project.id, 'partner_id': cls.customer.id,
            'journal_id': cls.sales_journal.id, 'income_account_id': cls.income.id,
            'retention_percent': 10, 'retention_cap': 100, 'retention_account_id': cls.retention.id,
            'recovery_percent': 20, 'advance_cap': 100, 'advance_account_id': cls.advance.id,
            'line_ids': [Command.create({'name': 'Complete scope', 'code': 'CL-BOQ', 'wbs_id': cls.wbs.id,
                                        'quantity': 10, 'unit_price': 100,
                                        'uom_id': cls.env.ref('uom.product_uom_unit').id})],
        })
        cls.contract.action_activate()

    def _cash(self, kind, amount):
        document = self.env['jakh.contract.cash.document'].with_user(self.manager).create({
            'name': 'Approved %s' % kind, 'contract_id': self.contract.id, 'kind': kind, 'amount': amount,
            'reason': 'Contractual entitlement verified against approved control policy.',
        })
        document.action_submit()
        document.action_approve()
        document.action_create_invoice()
        return document

    def _certificate(self):
        certificate = self.env['jakh.payment.certificate'].with_user(self.manager).create({
            'name': 'Final cumulative certificate', 'contract_id': self.contract.id,
        })
        certificate.action_prepare_lines()
        certificate.line_ids.write({'cumulative_quantity': 10})
        certificate.action_submit()
        certificate.action_approve()
        certificate.action_create_invoice()
        certificate.invoice_id.action_post()
        return certificate

    def _settle(self, invoice):
        wizard = self.env['account.payment.register'].with_user(self.manager).with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'journal_id': self.bank_journal.id, 'payment_method_line_id': self.method.id,
                  'payment_date': fields.Date.today()})
        payments = wizard._create_payments()
        self.assertTrue(payments)
        self.assertTrue(all(payment.move_id.state == 'posted' for payment in payments))
        self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
        self.assertTrue(all(invoice.line_ids.filtered(lambda line: line.account_id == self.receivable).mapped('reconciled')))
        return payments

    def _final(self):
        final = self.env['jakh.project.final.account'].with_user(self.manager).create({
            'name': 'Final-account acceptance', 'contract_id': self.contract.id,
            'reason': 'BOQ complete, all commercial balances settled and project actions resolved.',
        })
        final.action_submit()
        return final

    def test_advance_certificate_payment_retention_and_final_closure(self):
        advance = self._cash('advance', 100)
        advance.invoice_id.action_post()
        self.assertEqual(self.contract._jakh_recoverable_advance_cap(), 0)
        advance.action_register_payment()
        self._settle(advance.invoice_id)
        self.assertEqual(self.contract._jakh_recoverable_advance_cap(), 100)
        certificate = self._certificate()
        self.assertEqual(certificate.retention_amount, 100)
        self.assertEqual(certificate.recovery_amount, 100)
        self.assertEqual(certificate.invoice_id.amount_total, 800)
        self._settle(certificate.invoice_id)
        retention = self._cash('retention', 100)
        retention.action_create_invoice()
        self.assertEqual(self.env['account.move'].search_count([('jakh_cash_document_id', '=', retention.id)]), 1)
        retention.invoice_id.action_post()
        self._settle(retention.invoice_id)
        final = self._final()
        final.action_approve()
        final.action_close()
        self.assertEqual(final.state, 'closed')
        self.assertEqual(self.contract.state, 'closed')
        self.assertFalse(self.project.active)
        self.assertEqual(final.certified_gross, 1000)
        self.assertEqual(final.recovered_advance, 100)
        self.assertEqual(final.released_retention, 100)
        moves = certificate.invoice_id | advance.invoice_id | retention.invoice_id
        self.assertAlmostEqual(sum(moves.line_ids.filtered(lambda line: line.account_id == self.retention).mapped('balance')), 0)
        self.assertAlmostEqual(sum(moves.line_ids.filtered(lambda line: line.account_id == self.advance).mapped('balance')), 0)
        with self.assertRaises(UserError), self.cr.savepoint():
            final.action_reopen()
        final.write({'reopen_reason': 'Approved latent-defect correction requires another documented acceptance.'})
        final.action_reopen()
        self.assertEqual(final.state, 'reopened')
        self.assertEqual(self.contract.state, 'active')
        self.assertTrue(self.project.active)
        self.assertTrue(self.env['jakh.audit.event'].search_count([('resource_model', '=', final._name), ('resource_id', '=', final.id)]))

    def test_unpaid_invoices_open_work_and_excess_retention_block_closure(self):
        certificate = self._certificate()
        self.assertEqual(certificate.recovery_amount, 0, 'An unreceived advance ceiling must never authorize recovery.')
        final = self._final()
        with self.assertRaises(UserError), self.cr.savepoint():
            final.action_approve()
        self._settle(certificate.invoice_id)
        excessive = self.env['jakh.contract.cash.document'].with_user(self.manager).create({
            'name': 'Excess release', 'contract_id': self.contract.id, 'kind': 'retention', 'amount': 101,
            'reason': 'Deliberate cap exception test.',
        })
        excessive.action_submit()
        with self.assertRaises(UserError), self.cr.savepoint():
            excessive.action_approve()
        excessive.action_cancel()
        retention = self._cash('retention', 100)
        retention.invoice_id.action_post()
        self._settle(retention.invoice_id)
        task = self.env['project.task'].with_user(self.manager).create({'name': 'Unfinished snag', 'project_id': self.project.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            final.action_approve()
        task.write({'state': '1_done'})
        final.action_approve()
        final.action_close()
        self.assertEqual(final.state, 'closed')

    def test_cash_invoice_identity_and_state_cannot_be_forged(self):
        document = self._cash('advance', 100)
        invoice = document.invoice_id
        with self.assertRaises(AccessError), self.cr.savepoint():
            document.write({'state': 'invoiced'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move'].with_user(self.manager).create({'jakh_cash_document_id': document.id})
        invoice.invoice_line_ids.write({'price_unit': 99})
        with self.assertRaises(UserError), self.cr.savepoint():
            invoice.action_post()
        invoice.invoice_line_ids.write({'price_unit': 100})
        invoice.action_post()
        with self.assertRaises(UserError), self.cr.savepoint():
            invoice.button_draft()

    def test_native_credit_then_replacement_preserves_audit_and_cost_facts(self):
        original = self._certificate()
        with self.assertRaises(UserError), self.cr.savepoint():
            original.action_confirm_reversal()
        credit = original.invoice_id._reverse_moves([{'invoice_date': fields.Date.today(), 'date': fields.Date.today()}], cancel=True)
        self.assertEqual(credit.state, 'posted')
        with self.assertRaises(UserError), self.cr.savepoint():
            original.invoice_id._reverse_moves([{'invoice_date': fields.Date.today(), 'date': fields.Date.today()}], cancel=True)
        original.action_confirm_reversal()
        original.action_confirm_reversal()
        self.assertEqual(original.state, 'cancelled')
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([('project_id', '=', self.project.id)])
        self.assertAlmostEqual(sum(facts.filtered(lambda row: row.entry_type == 'certified_revenue').mapped('amount')), 0)
        self.assertAlmostEqual(sum(facts.filtered(lambda row: row.entry_type == 'billed_revenue').mapped('amount')), 0)
        replacement = self._certificate()
        self.assertEqual(replacement.previous_gross, 0)
        self.assertEqual(replacement.retention_amount, 100)
        self._settle(replacement.invoice_id)
        retention = self._cash('retention', 100)
        retention.invoice_id.action_post()
        self._settle(retention.invoice_id)
        final = self._final()
        final.action_approve()
        final.action_close()
        self.assertEqual(final.state, 'closed')

    def test_posted_owner_sources_and_credits_seal_financial_changes(self):
        advance = self._cash('advance', 100)
        invoice = advance.invoice_id
        with self.assertRaises(AccessError), self.cr.savepoint():
            invoice.with_context(_jakh_owner_finance_post=True).write({'state': 'posted'})
        invoice.action_post()
        for changes in ({'date': fields.Date.today()}, {'invoice_date': fields.Date.today()},
                        {'journal_id': self.sales_journal.id}, {'currency_id': invoice.currency_id.id},
                        {'company_id': invoice.company_id.id}, {'move_type': 'out_refund'},
                        {'state': 'draft'}):
            with self.assertRaises(AccessError), self.cr.savepoint():
                invoice.write(changes)
        self._settle(invoice)
        certificate = self._certificate()
        credit = certificate.invoice_id._reverse_moves([{'invoice_date': fields.Date.today(), 'date': fields.Date.today()}], cancel=True)
        for source in (invoice, certificate.invoice_id, credit):
            product_line = source.invoice_line_ids.filtered(lambda line: line.display_type == 'product')[:1]
            with self.assertRaises(AccessError), self.cr.savepoint():
                product_line.write({'price_unit': 999})
            with self.assertRaises(AccessError), self.cr.savepoint():
                product_line.write({'account_id': self.retention.id})
            with self.assertRaises(AccessError), self.cr.savepoint():
                product_line.unlink()
            with self.assertRaises(AccessError), self.cr.savepoint():
                self.env['account.move.line'].with_user(self.manager).create([
                    {'move_id': source.id, 'name': 'Forbidden balanced append', 'account_id': self.income.id, 'debit': 1},
                    {'move_id': source.id, 'name': 'Forbidden balanced append offset', 'account_id': self.advance.id, 'credit': 1},
                ])
            with self.assertRaises(AccessError), self.cr.savepoint():
                source.write({'state': 'cancel'})
