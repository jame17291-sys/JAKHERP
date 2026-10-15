from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestMaterialMatching(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='material.matching.manager', groups='jakh_core.group_manager')
        cls.company = cls.env.company
        if not cls.company.account_fiscal_country_id:
            cls.company.country_id = cls.env.ref('base.ae')
        def account(code, kind, reconcile=False):
            return cls.env['account.account'].create({'name': 'Synthetic material matching ' + kind,
                'code': code, 'account_type': kind, 'reconcile': reconcile,
                'company_ids': [Command.set(cls.company.ids)]})
        cls.inventory = account('MM901', 'asset_current')
        cls.expense = account('MM902', 'expense_direct_cost')
        cls.payable = account('MM903', 'liability_payable', True)
        cls.stock_journal = cls.env['account.journal'].create({'name': 'Synthetic matching stock', 'code': 'MMV',
            'type': 'general', 'company_id': cls.company.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Synthetic matching purchase', 'code': 'MMP',
            'type': 'purchase', 'company_id': cls.company.id, 'default_account_id': cls.expense.id})
        cls.company.write({'account_stock_journal_id': cls.stock_journal.id, 'account_stock_valuation_id': cls.inventory.id})
        cls.category = cls.env['product.category'].create({'name': 'Synthetic matching materials',
            'property_valuation': 'real_time', 'property_cost_method': 'standard',
            'property_stock_valuation_account_id': cls.inventory.id, 'property_account_expense_categ_id': cls.expense.id})
        cls.product = cls.env['product.product'].create({'name': 'Synthetic approved building material',
            'type': 'consu', 'is_storable': True, 'purchase_method': 'receive', 'standard_price': 10,
            'categ_id': cls.category.id, 'supplier_taxes_id': [Command.clear()]})
        cls.substitute = cls.env['product.product'].create({'name': 'Synthetic unapproved substitute service',
            'type': 'service', 'property_account_expense_id': cls.expense.id, 'supplier_taxes_id': [Command.clear()]})
        cls.vendor = cls.env['res.partner'].create({'name': 'Synthetic approved material supplier',
            'property_account_payable_id': cls.payable.id})
        cls.other_vendor = cls.env['res.partner'].create({'name': 'Synthetic other material supplier',
            'property_account_payable_id': cls.payable.id})
        cls.tax = cls.env['account.tax'].create({'name': 'Synthetic material purchase tax', 'amount': 5,
            'amount_type': 'percent', 'type_tax_use': 'purchase', 'company_id': cls.company.id})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Synthetic material matching site',
            'company_id': cls.company.id, 'user_id': cls.manager.id})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Material matching package',
            'code': 'MM-01', 'project_id': cls.project.id, 'budget_amount': 20000000})
        cls.wbs.action_release()

    def _order(self, quantity=5, taxed=False):
        request = self.env['jakh.purchase.request'].with_user(self.manager).create({
            'name': 'Synthetic controlled material order', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'material_plan_reference': 'Reviewed material requirement', 'supplier_id': self.vendor.id,
            'line_ids': [Command.create({'name': self.product.name, 'product_id': self.product.id,
                'uom_id': self.product.uom_id.id, 'quantity': quantity})]})
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({'line_id': request.line_ids.id,
            'unit_price': 10, 'tax_ids': [Command.set(self.tax.ids if taxed else [])]})
        request.action_submit(); request.action_approve(); request.action_release()
        order = request.purchase_order_id
        order.button_confirm()
        return order

    def _receive(self, order, quantity=5):
        picking = order.picking_ids.filtered(lambda row: row.state not in ('done', 'cancel'))[:1]
        picking.move_ids.write({'quantity': quantity, 'picked': True})
        picking.move_ids._action_done()
        return picking

    def _bill(self, line, quantity, changes=None, header=None, quantities=None):
        rows = []
        for amount in quantities or [quantity]:
            values = line._prepare_account_move_line()
            values.update(quantity=amount)
            values.update(changes or {})
            rows.append(Command.create(values))
        values = {'move_type': 'in_invoice', 'company_id': self.company.id, 'partner_id': self.vendor.id,
            'journal_id': self.journal.id, 'currency_id': line.currency_id.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': rows}
        values.update(header or {})
        return self.env['account.move'].with_user(self.manager).create(values)

    def _credit(self, bill, quantity):
        credit = bill._reverse_moves(default_values_list=[{'date': fields.Date.today(), 'invoice_date': fields.Date.today()}])
        credit.invoice_line_ids.filtered('purchase_line_id').write({'quantity': quantity})
        return credit

    def test_material_bill_rejects_substituted_product_rate_discount_unit_and_tax(self):
        order = self._order(); self._receive(order)
        for changes in ({'product_id': self.substitute.id, 'account_id': self.expense.id}, {'price_unit': 11},
                        {'discount': 5}, {'product_uom_id': self.env.ref('uom.product_uom_dozen').id},
                        {'tax_ids': [Command.set(self.tax.ids)]}):
            with self.subTest(changes=changes), self.cr.savepoint():
                bill = self._bill(order.order_line, 1, changes)
                with self.assertRaises(UserError):
                    bill.action_post()
                self.assertEqual(bill.state, 'draft')
                self.assertFalse(self.env['jakh.project.control.entry'].search([
                    ('source_model', '=', 'account.move.line'), ('source_id', 'in', bill.invoice_line_ids.ids),
                    ('entry_type', '=', 'actual_cost')]))

    def test_material_bill_rejects_wrong_supplier_currency_and_omitted_approved_tax(self):
        order = self._order(taxed=True); self._receive(order)
        currency = self.env.ref('base.USD') if self.company.currency_id != self.env.ref('base.USD') else self.env.ref('base.AED')
        for changes, header in (({}, {'partner_id': self.other_vendor.id}), ({}, {'currency_id': currency.id}),
                                ({'tax_ids': [Command.clear()]}, {})):
            with self.subTest(header=header), self.cr.savepoint():
                bill = self._bill(order.order_line, 1, changes, header)
                with self.assertRaises(UserError):
                    bill.action_post()
                self.assertEqual(bill.state, 'draft')

    def test_sub_micro_rate_delta_cannot_amplify_into_large_order_overbilling(self):
        order = self._order(quantity=1000000); self._receive(order, 1000000)
        bill = self._bill(order.order_line, 1000000, {'price_unit': 10.0000005})
        with self.assertRaises(UserError), self.cr.savepoint():
            bill.action_post()
        self.assertEqual(bill.state, 'draft')

    def test_actual_partial_receipt_caps_billing_and_rejects_manual_received_counter(self):
        order = self._order(); self._receive(order, 2)
        with self.assertRaises(AccessError), self.cr.savepoint():
            order.order_line.write({'qty_received_manual': 99})
        with self.assertRaises(AccessError), self.cr.savepoint():
            order.order_line.write({'qty_received': 99})
        bill = self._bill(order.order_line, 3)
        with self.assertRaises(UserError), self.cr.savepoint():
            bill.action_post()
        accepted = self._bill(order.order_line, 2); accepted.action_post()
        self.assertEqual(accepted.state, 'posted')
        self.assertEqual(accepted.invoice_line_ids.account_id, self.inventory)

    def test_duplicate_lines_and_multi_bill_posting_are_counted_together(self):
        order = self._order(); self._receive(order, 3)
        duplicate = self._bill(order.order_line, 0, quantities=[2, 2])
        with self.assertRaises(UserError), self.cr.savepoint():
            duplicate.action_post()
        first, second = self._bill(order.order_line, 2), self._bill(order.order_line, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            (first | second).action_post()
        self.assertTrue(all(move.state == 'draft' for move in first | second | duplicate))

    def test_native_credits_are_allowed_but_original_and_batch_credit_caps_are_enforced(self):
        order = self._order(); self._receive(order)
        first, second = self._bill(order.order_line, 3), self._bill(order.order_line, 2)
        (first | second).action_post()
        one = self._credit(first, 1); one.action_post()
        self.assertEqual(one.state, 'posted')
        too_much = self._credit(first, 3)
        with self.assertRaises(UserError), self.cr.savepoint():
            too_much.action_post()
        batch_a, batch_b = self._credit(first, 2), self._credit(first, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            (batch_a | batch_b).action_post()
        remaining = self._credit(first, 2); remaining.action_post()
        self.assertEqual(remaining.state, 'posted')

    def test_credit_needs_posted_original_and_posted_commercial_sources_are_immutable(self):
        order = self._order(); self._receive(order)
        bill = self._bill(order.order_line, 2)
        with self.assertRaises(AccessError), self.cr.savepoint():
            bill.write({'state': 'posted'})
        bill.action_post()
        for values in ({'quantity': 3}, {'price_unit': 11}, {'purchase_line_id': False}, {'display_type': 'line_note'}):
            with self.subTest(values=values), self.assertRaises(AccessError), self.cr.savepoint():
                bill.invoice_line_ids.write(values)
        with self.assertRaises(AccessError), self.cr.savepoint():
            bill.write({'partner_id': self.other_vendor.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            bill.button_draft()
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).create({'move_id': bill.id,
                'name': 'Forbidden material appendix', 'account_id': self.inventory.id})
        detached = self._bill(order.order_line, 1, header={'move_type': 'in_refund'})
        with self.assertRaises(UserError), self.cr.savepoint():
            detached.action_post()

    def test_material_over_receipt_fails_before_stock_completion_or_project_fact(self):
        order = self._order()
        picking = order.picking_ids
        picking.move_ids.write({'quantity': 6, 'picked': True})
        with self.assertRaises(UserError), self.cr.savepoint():
            picking.move_ids._action_done()
        self.assertFalse(picking.move_ids.filtered(lambda move: move.state == 'done'))
        self.assertFalse(self.env['jakh.project.control.entry'].search([
            ('source_model', '=', 'stock.move'), ('source_id', 'in', picking.move_ids.ids)]))

    def test_native_quantity_quantum_over_receipt_is_not_a_hidden_tolerance(self):
        order = self._order(); picking = order.picking_ids
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        picking.move_ids.write({'quantity': 5 + 10 ** -precision, 'picked': True})
        self.assertGreater(picking.move_ids.quantity, 5, 'Exercise an actual native operation quantity, not a delta removed by its inverse rounding.')
        with self.assertRaises(UserError), self.cr.savepoint():
            picking.move_ids._action_done()
        self.assertFalse(picking.move_ids.filtered(lambda move: move.state == 'done'))

    def test_receipt_matches_product_and_cumulative_batch_quantities(self):
        order = self._order()
        picking = order.picking_ids
        with self.cr.savepoint():
            picking.move_ids.write({'product_id': self.substitute.id, 'quantity': 1, 'picked': True})
            with self.assertRaises(UserError):
                picking.move_ids._action_done()
        # Roll back the intentionally wrong product before building a separate batch.
        picking.move_ids.write({'product_id': self.product.id, 'quantity': 3, 'picked': True})
        another = picking.copy({'move_ids': []})
        extra = picking.move_ids.copy({'picking_id': another.id, 'product_uom_qty': 3, 'quantity': 3, 'picked': True})
        another.action_confirm()
        with self.assertRaises(UserError), self.cr.savepoint():
            (picking.move_ids | extra)._action_done()
        self.assertFalse((picking.move_ids | extra).filtered(lambda move: move.state == 'done'))

    def test_supplier_returns_use_original_receipt_and_cannot_over_return_or_edit_history(self):
        order = self._order(); picking = self._receive(order, 3)
        original = picking.move_ids.filtered(lambda move: move.state == 'done')
        returned = picking._create_return(); returned.action_confirm(); returned.action_assign()
        returned.move_ids.write({'quantity': 2, 'picked': True}); returned.move_ids._action_done()
        self.assertEqual(order.order_line.qty_received, 1)
        too_much = picking._create_return(); too_much.action_confirm(); too_much.action_assign()
        too_much.move_ids.write({'quantity': 2, 'picked': True})
        with self.assertRaises(UserError), self.cr.savepoint():
            too_much.move_ids._action_done()
        detached = picking._create_return(); detached.action_confirm()
        detached.move_ids.write({'origin_returned_move_id': False, 'quantity': 1, 'picked': True})
        with self.assertRaises(UserError), self.cr.savepoint():
            detached.move_ids._action_done()
        for values in ({'quantity': 4}, {'purchase_line_id': False}, {'state': 'draft'}):
            with self.subTest(values=values), self.assertRaises(AccessError), self.cr.savepoint():
                original.write(values)
        with self.assertRaises(AccessError), self.cr.savepoint():
            original.with_context(_jakh_material_receipt_value=True).write({'value': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            original.sudo().write({'value': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            original.move_line_ids.write({'quantity': 4})
        bill = self._bill(order.order_line, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            bill.action_post()
        valid = self._bill(order.order_line, 1); valid.action_post()
        self.assertEqual(valid.state, 'posted')

    def test_two_step_receiving_records_supplier_leg_once_not_internal_storage_leg(self):
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        warehouse.write({'reception_steps': 'two_steps'})
        order = self._order()
        incoming = order.picking_ids.filtered(lambda picking: picking.picking_type_code == 'incoming')
        self.assertEqual(incoming.location_dest_id, warehouse.wh_input_stock_loc_id)
        incoming.move_ids.write({'quantity': 5, 'picked': True})
        incoming.move_ids._action_done()
        facts = self.env['jakh.project.control.entry'].search([('project_id', '=', self.project.id),
            ('entry_type', '=', 'receipt'), ('source_model', '=', 'stock.move')])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts.quantity, 5)
        self.assertEqual(facts.amount, 50)
        internal = self.env['stock.picking'].search([('company_id', '=', self.company.id),
            ('location_id', '=', warehouse.wh_input_stock_loc_id.id), ('location_dest_id', '=', warehouse.lot_stock_id.id),
            ('state', 'not in', ['done', 'cancel'])])
        self.assertEqual(len(internal), 1)
        internal.move_ids.write({'quantity': 5, 'picked': True})
        internal.move_ids._action_done()
        after = self.env['jakh.project.control.entry'].search([('project_id', '=', self.project.id),
            ('entry_type', '=', 'receipt'), ('source_model', '=', 'stock.move')])
        self.assertEqual(after, facts)
        self.assertEqual(sum(after.mapped('amount')), 50)
        self.assertEqual(order.order_line.qty_received, 5)
        bill = self._bill(order.order_line, 5); bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_unlinked_native_supplier_bill_is_not_subject_to_project_material_matching(self):
        bill = self.env['account.move'].with_user(self.manager).create({'move_type': 'in_invoice',
            'company_id': self.company.id, 'partner_id': self.vendor.id, 'journal_id': self.journal.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({'name': 'Unlinked native expense',
                'product_id': self.substitute.id, 'account_id': self.expense.id, 'quantity': 1, 'price_unit': 10,
                'tax_ids': [Command.clear()]})]})
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
