from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestSubcontractWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='subcontract.workflow.manager', groups='jakh_core.group_manager')
        cls.company = cls.env.company
        if not cls.company.account_fiscal_country_id:
            # A deliberately empty fresh pilot has no chart/tax country yet.
            # Set only this rolled-back test fixture, never deployment policy.
            cls.company.country_id = cls.env.ref('base.ae')
        payable = cls.env['account.account'].create({
            'name': 'Subcontract workflow payable', 'code': 'JSC9001', 'account_type': 'liability_payable',
            'reconcile': True, 'company_ids': [Command.set(cls.company.ids)],
        })
        expense = cls.env['account.account'].create({
            'name': 'Subcontract workflow expense', 'code': 'JSC9002', 'account_type': 'expense',
            'company_ids': [Command.set(cls.company.ids)],
        })
        cls.journal = cls.env['account.journal'].create({
            'name': 'Subcontract workflow purchase', 'code': 'JSC', 'type': 'purchase',
            'company_id': cls.company.id, 'default_account_id': expense.id,
        })
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Workflow subcontractor', 'property_account_payable_id': payable.id,
        })
        cls.service = cls.env['product.product'].create({
            'name': 'Subcontract installation work', 'type': 'service', 'purchase_ok': True,
            'purchase_method': 'receive', 'property_account_expense_id': expense.id,
        })
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Subcontract workflow project', 'company_id': cls.company.id, 'user_id': cls.manager.id,
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Subcontract installation', 'code': 'SC-WF-01', 'project_id': cls.project.id, 'budget_amount': 300,
        })
        cls.wbs.action_release()

    def _order(self):
        request = self.env['jakh.purchase.request'].with_user(self.manager).create({
            'name': 'SC-WORKFLOW-RQ', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'material_plan_reference': 'Approved subcontract work package', 'supplier_id': self.vendor.id,
            'line_ids': [Command.create({
                'name': 'Installation work', 'product_id': self.service.id,
                'uom_id': self.service.uom_id.id, 'quantity': 10,
            })],
        })
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({
            'line_id': request.line_ids.id, 'unit_price': 20,
        })
        request.action_submit(); request.action_approve(); request.action_release()
        request.purchase_order_id.button_confirm()
        return request.purchase_order_id

    def _ses(self, line, quantity):
        ses = self.env['jakh.subcontract.service.entry'].with_user(self.manager).create({
            'name': 'Controlled site work', 'purchase_line_id': line.id, 'quantity': quantity,
            'notes': 'Completed work inspected against issued scope.',
        })
        ses.action_submit()
        return ses

    def _bill(self, line, quantity, **changes):
        values = line._prepare_account_move_line()
        values['quantity'] = quantity
        values.update(changes)
        return self.env['account.move'].with_user(self.manager).create({
            'move_type': 'in_invoice', 'company_id': self.company.id,
            'partner_id': self.vendor.id, 'journal_id': self.journal.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create(values)],
        })

    def _reversal(self, ses, quantity):
        reversal = self.env['jakh.subcontract.service.reversal'].with_user(self.manager).create({
            'name': 'Documented work correction', 'service_entry_id': ses.id,
            'quantity': quantity, 'reason': 'Inspected work correction and traceable site record.',
        })
        reversal.action_submit()
        return reversal

    def _amendment(self, line, quantity, price, effect='addition'):
        variation = self.env['jakh.project.variation'].with_user(self.manager).create({
            'name': 'Approved installation variation', 'variation_type': 'subcontract', 'effect': effect,
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'partner_id': self.vendor.id,
            'description': 'Approved changed scope.', 'quantity': quantity,
            'uom_id': line.uom_id.id, 'unit_price': price,
        })
        variation.action_submit(); variation.action_price(); variation.action_approve()
        amendment = self.env['jakh.subcontract.amendment'].with_user(self.manager).create({
            'name': 'Approved PO amendment', 'variation_id': variation.id,
            'purchase_line_id': line.id, 'reason': 'Approved variation applied to the confirmed order.',
        })
        amendment.action_submit()
        return amendment

    def test_service_invoice_cannot_precede_acceptance_or_disable_matching(self):
        order = self._order(); line = order.order_line
        self.assertTrue(line.jakh_requires_ses, 'Every project service line needs matching before its first SES.')
        with self.assertRaises(AccessError):
            line.write({'jakh_requires_ses': False})
        with self.assertRaises(AccessError):
            self.env['purchase.order.line'].with_user(self.manager).create({
                'order_id': order.id, 'name': 'Marker bypass', 'product_id': self.service.id,
                'product_qty': 1, 'price_unit': 20, 'jakh_requires_ses': False,
            })
        with self.assertRaises(AccessError):
            line.write({'qty_received_manual': 10})
        bill = self._bill(line, 1)
        with self.assertRaises(UserError):
            bill.action_post()
        self.assertEqual(bill.state, 'draft')

    def test_partial_service_to_bill_preserves_scope_and_posts_actual_once(self):
        order = self._order(); line = order.order_line
        ses = self._ses(line, 4); ses.action_approve(); ses.action_approve()
        self.assertEqual(line.qty_received, 4)
        self.assertEqual(ses.accepted_amount, 80)
        receipt = self.env['jakh.project.control.entry'].search([
            ('source_model', '=', ses._name), ('source_id', '=', ses.id), ('entry_type', '=', 'receipt'),
        ])
        self.assertEqual(len(receipt), 1)
        ses.action_create_bill(); first_bill = ses.invoice_id
        ses.action_create_bill(); self.assertEqual(ses.invoice_id, first_bill)
        first_bill.write({'journal_id': self.journal.id})
        first_bill.action_post()
        self.assertEqual(first_bill.state, 'posted')
        self.assertEqual(first_bill.invoice_line_ids.quantity, 4)
        self.assertEqual(first_bill.amount_untaxed, 80)
        self.assertAlmostEqual(sum(first_bill.line_ids.mapped('balance')), 0)
        self.assertEqual(line._jakh_service_totals()[2], 4)
        with self.assertRaises(AccessError):
            first_bill.invoice_line_ids.write({'purchase_line_id': False})
        with self.assertRaises(AccessError), self.cr.savepoint():
            first_bill.invoice_line_ids.write({'price_unit': 25})
        with self.assertRaises(AccessError), self.cr.savepoint():
            first_bill.write({'state': 'draft'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).create([
                {'move_id': first_bill.id, 'account_id': first_bill.invoice_line_ids.account_id.id, 'debit': 10},
                {'move_id': first_bill.id, 'account_id': self.vendor.property_account_payable_id.id, 'credit': 10},
            ])

    def test_excess_acceptance_and_invoice_value_are_blocked(self):
        line = self._order().order_line
        ses = self._ses(line, 4); ses.action_approve()
        excess = self._ses(line, 7)
        with self.assertRaises(UserError):
            excess.action_approve()
        self.assertEqual(excess.state, 'submitted')
        excess_bill = self._bill(line, 5)
        with self.assertRaises(UserError):
            excess_bill.action_post()
        high_rate = self._bill(line, 4, price_unit=21)
        with self.assertRaises(UserError):
            high_rate.action_post()
        wrong_product = self.env['product.product'].create({'name': 'Unapproved replacement service', 'type': 'service'})
        wrong_bill = self._bill(line, 4, product_id=wrong_product.id)
        with self.assertRaises(UserError):
            wrong_bill.action_post()
        wrong_uom = self._bill(line, 4, product_uom_id=self.env.ref('uom.product_uom_dozen').id)
        with self.assertRaises(UserError):
            wrong_uom.action_post()
        unexpected_tax = self.env['account.tax'].create({
            'name': 'Unapproved subcontract tax', 'amount': 5, 'type_tax_use': 'purchase',
            'company_id': self.company.id,
            'tax_group_id': self.env['account.tax.group'].create({'name': 'Synthetic test tax group', 'company_id': self.company.id}).id,
        })
        taxed_bill = self._bill(line, 4, tax_ids=[Command.set(unexpected_tax.ids)])
        with self.assertRaises(UserError):
            taxed_bill.action_post()
        negative_bill = self._bill(line, -1)
        with self.assertRaises(UserError):
            negative_bill.action_post()

    def test_unbilled_service_reversal_retains_original_evidence(self):
        line = self._order().order_line
        ses = self._ses(line, 4); ses.action_approve()
        reversal = self._reversal(ses, 2); reversal.action_approve(); reversal.action_approve()
        self.assertEqual(ses.state, 'approved')
        self.assertEqual(ses.quantity, 4)
        self.assertEqual(ses.net_accepted_quantity, 2)
        self.assertEqual(line.qty_received, 2)
        self.assertEqual(reversal.reversed_amount, 40)
        correction_entry = self.env['jakh.project.control.entry'].search([
            ('source_model', '=', reversal._name), ('source_id', '=', reversal.id),
        ])
        self.assertEqual(len(correction_entry), 1)
        self.assertEqual(correction_entry.amount, -40)
        too_much = self._reversal(ses, 3)
        with self.assertRaises(UserError):
            too_much.action_approve()
        with self.assertRaises(AccessError):
            reversal.write({'state': 'draft'})

    def test_billed_service_requires_native_credit_before_work_reversal(self):
        line = self._order().order_line
        ses = self._ses(line, 4); ses.action_approve()
        bill = self._bill(line, 4); bill.action_post()
        reversal = self._reversal(ses, 1)
        with self.assertRaises(UserError):
            reversal.action_approve()
        credit = bill._reverse_moves(default_values_list=[{'date': fields.Date.today(), 'invoice_date': fields.Date.today()}])
        credit.action_post()
        self.assertEqual(credit.reversed_entry_id, bill)
        self.assertEqual(line._jakh_service_totals()[2], 0)
        reversal.action_approve()
        self.assertEqual(line.qty_received, 3)
        duplicate_credit = bill._reverse_moves(default_values_list=[{'date': fields.Date.today(), 'invoice_date': fields.Date.today()}])
        with self.assertRaises(UserError):
            duplicate_credit.action_post()

    def test_variation_applies_budget_controlled_order_and_commitment_amendment(self):
        order = self._order(); line = order.order_line
        amendment = self._amendment(line, 2, 20)
        amendment.action_approve(); amendment.action_approve()
        self.assertEqual(line.product_qty, 12)
        self.assertEqual(order.jakh_request_id.reserved_amount, 240)
        self.assertEqual(amendment.commitment_delta, 40)
        self.assertEqual(self.env['jakh.project.control.entry']._sum(self.wbs, ['commitment']), 240)
        different_rate = self._amendment(line, 1, 30)
        different_rate.action_approve()
        self.assertEqual(len(order.order_line), 2)
        self.assertEqual(different_rate.applied_line_id.price_unit, 30)
        self.assertEqual(different_rate.applied_line_id.product_qty, 1)
        self.assertTrue(different_rate.applied_line_id.jakh_requires_ses)
        over_budget = self._amendment(line, 2, 20)
        with self.assertRaises(UserError):
            over_budget.action_approve()
        self.assertEqual(line.product_qty, 12)
        with self.assertRaises(AccessError):
            line.write({'product_qty': 13})

    def test_omission_and_cancellation_do_not_leave_phantom_commitment(self):
        order = self._order(); line = order.order_line
        ses = self._ses(line, 4); ses.action_approve()
        omission = self._amendment(line, 7, 20, effect='omission')
        with self.assertRaises(UserError):
            omission.action_approve()
        accepted_omission = self._amendment(line, 2, 20, effect='omission')
        accepted_omission.action_approve()
        self.assertEqual(line.product_qty, 8)
        with self.assertRaises(UserError):
            order.jakh_request_id.action_cancel()
        reversal = self._reversal(ses, 4); reversal.action_approve()
        order.jakh_request_id.action_cancel()
        self.assertEqual(order.jakh_request_id.state, 'cancelled')
        self.assertEqual(accepted_omission.state, 'cancelled')
        self.assertEqual(self.env['jakh.project.control.entry']._sum(self.wbs, ['commitment']), 0)
