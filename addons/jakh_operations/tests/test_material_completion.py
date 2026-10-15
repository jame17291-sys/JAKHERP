from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestMaterialCompletion(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='material.completion.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='material.completion.technical', groups='jakh_core.group_technical')
        cls.company = cls.env.company
        def account(code, kind, reconcile=False):
            return cls.env['account.account'].create({'name': 'Synthetic material ' + kind, 'code': code,
                'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(cls.company.ids)]})
        cls.inventory = account('MAT901', 'asset_current')
        cls.expense = account('MAT902', 'expense_direct_cost')
        cls.ap = account('MAT903', 'liability_payable', True)
        cls.ar = account('MAT904', 'asset_receivable', True)
        cls.income = account('MAT905', 'income')
        cls.stock_journal = cls.env['account.journal'].create({'name': 'Material stock', 'code': 'MATV', 'type': 'general', 'company_id': cls.company.id})
        cls.sales = cls.env['account.journal'].create({'name': 'Material sales', 'code': 'MATS', 'type': 'sale', 'company_id': cls.company.id})
        cls.purchase = cls.env['account.journal'].create({'name': 'Material purchasing', 'code': 'MATP', 'type': 'purchase', 'company_id': cls.company.id})
        cls.company.write({'account_stock_journal_id': cls.stock_journal.id, 'account_stock_valuation_id': cls.inventory.id})
        cls.category = cls.env['product.category'].create({'name': 'Synthetic perpetual materials',
            'property_valuation': 'real_time', 'property_cost_method': 'standard',
            'property_stock_valuation_account_id': cls.inventory.id, 'property_account_expense_categ_id': cls.expense.id})
        cls.product = cls.env['product.product'].create({'name': 'Synthetic structural consumable', 'type': 'consu',
            'is_storable': True, 'categ_id': cls.category.id, 'standard_price': 10, 'supplier_taxes_id': [Command.clear()]})
        cls.customer = cls.env['res.partner'].create({'name': 'Synthetic material client', 'property_account_receivable_id': cls.ar.id})
        cls.vendor = cls.env['res.partner'].create({'name': 'Synthetic material supplier', 'property_account_payable_id': cls.ap.id})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Synthetic site material project',
            'company_id': cls.company.id, 'user_id': cls.manager.id, 'privacy_visibility': 'employees'})
        cls.project.message_subscribe(partner_ids=cls.technical.partner_id.ids)
        cls.project._jakh_prepare_cost_centre()
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Structure', 'code': 'MAT-01',
            'project_id': cls.project.id, 'budget_amount': 1000})
        cls.wbs.action_release()
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({'name': 'Synthetic material contract',
            'project_id': cls.project.id, 'partner_id': cls.customer.id, 'journal_id': cls.sales.id, 'income_account_id': cls.income.id,
            'line_ids': [Command.create({'code': 'MAT-BOQ', 'name': 'Measured structural work', 'wbs_id': cls.wbs.id,
                'quantity': 10, 'unit_price': 100, 'uom_id': cls.product.uom_id.id})]})
        cls.location = cls.env['jakh.project.location'].with_user(cls.manager).create({'name': 'Building A', 'code': 'A',
            'project_id': cls.project.id, 'kind': 'building'})
        cls.contract.line_ids.write({'location_id': cls.location.id})
        cls.contract.action_activate()
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        # Explicit synthetic warehouse setup. Native one-step warehouses archive
        # this operation until storage locations are configured by an operator.
        cls.warehouse.int_type_id.write({'active': True})
        cls.store = cls.warehouse.lot_stock_id
        cls.consumption = cls.env['stock.location'].create({'name': 'Synthetic project consumption', 'usage': 'inventory',
            'company_id': cls.company.id, 'valuation_account_id': cls.expense.id})

    def _demand(self, quantity=5):
        demand = self.env['jakh.material.request'].with_user(self.manager).create({'name': 'Synthetic material demand',
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'contract_line_id': self.contract.line_ids.id,
            'location_id': self.location.id, 'product_id': self.product.id, 'quantity': quantity,
            'source_location_id': self.store.id, 'consumption_location_id': self.consumption.id,
            'reason': 'Approved measured structural work.'})
        demand.action_submit(); demand.action_approve()
        return demand

    def test_stocked_material_is_billed_to_asset_and_issued_once_to_boq_cost(self):
        demand = self._demand()
        first = demand.action_prepare_shortage()
        self.assertEqual(demand.action_prepare_shortage()['res_id'], first['res_id'])
        request = demand.request_id
        self.assertEqual(request.line_ids.contract_line_id, self.contract.line_ids)
        self.assertEqual(request.line_ids.location_id, self.location)
        request.write({'supplier_id': self.vendor.id})
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({'line_id': request.line_ids.id, 'unit_price': 10})
        request.action_submit(); request.action_approve(); request.action_release()
        order = request.purchase_order_id; order.button_confirm()
        incoming = order.picking_ids
        incoming.move_ids.write({'quantity': 5, 'picked': True})
        incoming.move_ids._action_done()
        order.action_create_invoice()
        bill = order.invoice_ids
        bill.write({'invoice_date': fields.Date.today(), 'journal_id': self.purchase.id})
        bill.action_post()
        self.assertEqual(bill.invoice_line_ids.account_id, self.inventory)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager)
        self.assertFalse(facts.search([('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')]))
        action = demand.action_prepare_issue()
        issue = self.env['stock.picking'].with_user(self.manager).browse(action['res_id'])
        self.assertEqual(demand.action_prepare_issue()['res_id'], issue.id)
        issue.move_ids.write({'quantity': 5, 'picked': True})
        issue.move_ids._action_done()
        actual = facts.search([('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual.amount, 50)
        self.assertEqual(actual.contract_line_id, self.contract.line_ids)
        self.assertEqual(actual.location_id, self.location)
        self.assertEqual(actual.account_move_line_id.balance, 50)
        self.assertEqual(demand.state, 'done')
        self.assertEqual(demand.remaining_quantity, 0)

    def test_receipt_issue_then_late_bill_preserves_posted_cost_and_only_allows_private_noop_refresh(self):
        demand = self._demand()
        demand.action_prepare_shortage()
        request = demand.request_id
        request.write({'supplier_id': self.vendor.id})
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({'line_id': request.line_ids.id, 'unit_price': 10})
        request.action_submit(); request.action_approve(); request.action_release()
        order = request.purchase_order_id; order.button_confirm()
        order.picking_ids.move_ids.write({'quantity': 5, 'picked': True})
        order.picking_ids.move_ids._action_done()
        order.action_create_invoice()
        bill = order.invoice_ids
        bill.write({'invoice_date': fields.Date.today(), 'journal_id': self.purchase.id})
        issue = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id']).move_ids
        issue.write({'quantity': 5, 'picked': True}); issue._action_done()
        facts = self.env['jakh.project.control.entry'].search([('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(len(facts), 1)
        journal = issue.account_move_id
        before_value = issue.value
        before_move_date = issue.write_date
        before_journal = journal.read(['state', 'date', 'write_date', 'line_ids'])[0]
        before_items = journal.line_ids.read(['balance', 'account_id', 'stock_move_id', 'write_date'])
        before_fact = facts.read(['amount', 'quantity', 'account_move_line_id', 'write_date'])[0]
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertEqual(issue.value, before_value)
        self.assertEqual(issue.write_date, before_move_date)
        self.assertEqual(journal.read(['state', 'date', 'write_date', 'line_ids'])[0], before_journal)
        self.assertEqual(journal.line_ids.read(['balance', 'account_id', 'stock_move_id', 'write_date']), before_items)
        self.assertEqual(facts.read(['amount', 'quantity', 'account_move_line_id', 'write_date'])[0], before_fact)
        self.assertEqual(self.env['jakh.project.control.entry'].search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')]), facts)
        from odoo.addons.jakh_operations.models.material_matching import _VALUE_POST
        private = issue.with_context(_jakh_material_receipt_value=_VALUE_POST)
        private.write({'value': before_value + issue.company_currency_id.rounding / 10})
        self.assertEqual(issue.value, before_value, 'Even currency-equivalent refreshes do not rewrite source evidence.')
        with self.assertRaises(AccessError), self.cr.savepoint():
            private.write({'value': before_value + 1})
        with self.assertRaises(AccessError), self.cr.savepoint():
            private.write({'quantity': 4})
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.with_context(_jakh_material_receipt_value=True).write({'value': before_value})
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.sudo().write({'value': before_value})

    def test_partial_issue_and_shortage_use_unreserved_available_stock(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, 3)
        demand = self._demand(5)
        self.assertEqual(demand.shortage_quantity, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            demand.action_prepare_issue()
        demand.write({'issue_quantity': 3})
        issue = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id'])
        issue.move_ids.write({'quantity': 3, 'picked': True}); issue.move_ids._action_done()
        self.assertEqual(demand.issued_quantity, 3)
        self.assertEqual(demand.remaining_quantity, 2)
        self.assertEqual(demand.state, 'approved')
        demand.action_prepare_shortage()
        self.assertEqual(demand.request_id.line_ids.quantity, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            demand.action_cancel()

    def test_issue_requires_active_operation_for_its_source_warehouse(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, 5)
        demand = self._demand()
        self.warehouse.int_type_id.write({'active': False})
        with self.assertRaisesRegex(UserError, 'Configure an internal stock operation'), self.cr.savepoint():
            demand.action_prepare_issue()
        self.assertFalse(demand.picking_ids)
        self.assertFalse(self.warehouse.int_type_id.active)

    def test_permission_scope_and_approved_identity_guards(self):
        demand = self._demand()
        with self.assertRaises(AccessError), self.cr.savepoint():
            demand.with_user(self.technical).action_prepare_issue()
        with self.assertRaises(UserError), self.cr.savepoint():
            demand.write({'quantity': 999})
        other = self.env['project.project'].with_user(self.manager).create({'name': 'Other material project', 'company_id': self.company.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env['jakh.material.request'].with_user(self.manager).create({'name': 'Invalid scope',
                'project_id': other.id, 'wbs_id': self.wbs.id, 'contract_line_id': self.contract.line_ids.id,
                'location_id': self.location.id, 'product_id': self.product.id, 'quantity': 1,
                'source_location_id': self.store.id, 'consumption_location_id': self.consumption.id, 'reason': 'Invalid fixture'})

    def test_before_after_quantity_audit_keeps_original_value(self):
        request = self.env['jakh.purchase.request'].with_user(self.manager).create({'name': 'Audited request',
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'material_plan_reference': 'Approved work',
            'line_ids': [Command.create({'name': 'Material', 'product_id': self.product.id, 'uom_id': self.product.uom_id.id, 'quantity': 2})]})
        line = request.line_ids
        line.with_user(self.technical).write({'quantity': 3})
        event = self.env['jakh.audit.event'].with_user(self.manager).search([('resource_model', '=', line._name),
            ('resource_id', '=', line.id), ('operation', '=', 'document.edited')], limit=1)
        import json
        self.assertEqual(json.loads(event.details)['changes']['quantity'], {'before': 2, 'after': 3})
        self.assertEqual(event.actor_id, self.technical)

    def _issued_demand(self, quantity=5):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, quantity)
        demand = self._demand(quantity)
        picking = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id'])
        picking.move_ids.write({'quantity': quantity, 'picked': True})
        picking.move_ids._action_done()
        return demand, picking.move_ids

    def _return_document(self, issue, quantity=2):
        document = self.env['jakh.material.return'].with_user(self.manager).create({
            'name': 'Reviewed unused material return', 'issue_move_id': issue.id,
            'quantity': quantity, 'reason': 'Verified unused site material returned to stores.'})
        evidence = self.env['ir.attachment'].with_user(self.manager).create({
            'name': 'Return evidence.txt', 'raw': b'Synthetic verified return evidence',
            'res_model': document._name, 'res_id': document.id})
        document.write({'evidence_attachment_ids': [Command.set(evidence.ids)]})
        document.action_submit()
        return document

    def test_material_source_and_derived_dimension_links_cannot_be_forged(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, 5)
        demand = self._demand()
        picking = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id'])
        move = picking.move_ids
        with self.assertRaises(AccessError), self.cr.savepoint():
            move.write({'state': 'done'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            picking.write({'jakh_material_request_id': False})
        with self.assertRaises(AccessError), self.cr.savepoint():
            move.write({'jakh_material_request_id': False})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['stock.move'].with_user(self.manager).with_context(_jakh_material_flow=True).create({
                'origin': 'Forged source', 'jakh_material_request_id': demand.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['stock.picking'].with_user(self.manager).with_context(default_jakh_material_request_id=demand.id).create({})
        with self.assertRaises(AccessError), self.cr.savepoint():
            move.write({'jakh_location_id': self.location.id})
        for model, values in [
                ('stock.move', {'jakh_location_id': self.location.id}),
                ('purchase.order.line', {'jakh_contract_line_id': self.contract.line_ids.id}),
                ('account.move.line', {'jakh_location_id': self.location.id}),
                ('jakh.subcontract.service.entry', {'contract_line_id': self.contract.line_ids.id})]:
            with self.assertRaises(AccessError), self.cr.savepoint():
                self.env[model].with_user(self.manager).create(values)
        with self.assertRaises(AccessError), self.cr.savepoint():
            demand.write({'issued_quantity': 999})

    def test_native_partial_validation_keeps_backorder_source_and_total_cost(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, 5)
        demand = self._demand()
        picking = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id'])
        picking.move_ids.write({'quantity': 3, 'picked': True})
        picking.move_ids._action_done()
        self.assertEqual(demand.issued_quantity, 3)
        backorder = demand.picking_ids.filtered(lambda row: row.state not in ('done', 'cancel'))
        self.assertEqual(len(backorder), 1)
        self.assertEqual(backorder.jakh_material_request_id, demand)
        self.assertEqual(backorder.move_ids.jakh_material_request_id, demand)
        self.assertEqual(demand.action_prepare_issue()['res_id'], backorder.id)
        backorder.move_ids.write({'quantity': 2, 'picked': True})
        backorder.move_ids._action_done()
        self.assertEqual(demand.state, 'done')
        self.assertEqual(demand.issued_quantity, 5)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(sum(facts.mapped('amount')), 50)
        identities = facts.ids
        backorder.move_ids._action_done()
        self.assertEqual(facts.search([('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')]).ids, identities)

    def test_excess_issue_fails_before_stock_or_ledger_posting(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.store, 6)
        demand = self._demand(5)
        picking = self.env['stock.picking'].with_user(self.manager).browse(demand.action_prepare_issue()['res_id'])
        with self.assertRaises(UserError), self.cr.savepoint():
            picking.move_ids.write({'quantity': 6, 'picked': True})
            picking.move_ids._action_done()
        self.assertEqual(demand.issued_quantity, 0)
        self.assertFalse(self.env['jakh.project.control.entry'].search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')]))

    def test_completed_issue_and_native_cost_journal_are_sealed(self):
        demand, issue = self._issued_demand()
        journal = issue.account_move_id
        expense = journal.line_ids.filtered(lambda line: line.account_id == self.expense)
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.write({'quantity': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.move_line_ids.write({'quantity': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.write({'account_move_id': False})
        with self.assertRaises(AccessError), self.cr.savepoint():
            issue.unlink()
        with self.assertRaises(AccessError), self.cr.savepoint():
            journal.write({'state': 'draft'})
        with self.assertRaises(UserError), self.cr.savepoint():
            journal.button_draft()
        with self.assertRaises(UserError), self.cr.savepoint():
            journal._reverse_moves()
        with self.assertRaises(AccessError), self.cr.savepoint():
            expense.write({'debit': 99})
        with self.assertRaises(AccessError), self.cr.savepoint():
            expense.write({'stock_move_id': False})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).create({
                'move_id': journal.id, 'name': 'Forbidden appended row', 'account_id': self.inventory.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['stock.move'].with_user(self.manager).create({
                'origin': 'Unreviewed native return', 'origin_returned_move_id': issue.id})
        self.assertEqual(expense.balance, 50)
        self.assertEqual(demand.issued_quantity, 5)

    def test_approved_return_posts_negative_cost_without_rewriting_original(self):
        demand, issue = self._issued_demand()
        original_journal = issue.account_move_id
        document = self._return_document(issue, 2)
        with self.assertRaises(UserError), self.cr.savepoint():
            document.evidence_attachment_ids.write({'raw': b'Changed submitted evidence'})
        document.action_approve()
        picking = self.env['stock.picking'].with_user(self.manager).browse(document.action_prepare_picking()['res_id'])
        self.assertEqual(document.action_prepare_picking()['res_id'], picking.id)
        picking.move_ids.write({'quantity': 2, 'picked': True})
        picking.move_ids._action_done()
        self.assertEqual(document.state, 'done')
        self.assertEqual(demand.state, 'approved')
        self.assertEqual(demand.issued_quantity, 3)
        self.assertEqual(demand.remaining_quantity, 2)
        self.assertEqual(issue.quantity, 5)
        self.assertEqual(issue.account_move_id, original_journal)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(sorted(facts.mapped('amount')), [-20, 50])
        self.assertEqual(sum(facts.mapped('amount')), sum(facts.account_move_line_id.mapped('balance')))
        self.assertEqual(facts.contract_line_id, self.contract.line_ids)
        self.assertEqual(facts.location_id, self.location)
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['ir.attachment'].with_user(self.manager).create({'name': 'Public return evidence', 'raw': b'private',
                'res_model': 'jakh.material.return', 'res_id': document.id, 'public': True})
        excess = self._return_document(issue, 4)
        with self.assertRaises(UserError), self.cr.savepoint():
            excess.action_approve()

    def test_material_factories_do_not_inherit_form_state_or_source_defaults(self):
        demand, issue = self._issued_demand()
        document = self._return_document(issue, 2)
        document.action_approve()
        picking = self.env['stock.picking'].with_user(self.manager).browse(document.with_context(
            default_state='done', default_origin_returned_move_id=999999,
            default_jakh_material_request_id=999999).action_prepare_picking()['res_id'])
        self.assertNotEqual(picking.state, 'done')
        self.assertNotEqual(picking.move_ids.state, 'done')
        self.assertEqual(picking.jakh_material_request_id, demand)
        self.assertEqual(picking.move_ids.origin_returned_move_id, issue)
        self.assertFalse(picking.move_ids.account_move_id)
        picking.move_ids.write({'quantity': 2, 'picked': True})
        picking.move_ids._action_done()
        issue_again = self.env['stock.picking'].with_user(self.manager).browse(demand.with_context(
            default_state='done', default_origin_returned_move_id=999999,
            default_jakh_material_return_id=999999).action_prepare_issue()['res_id'])
        self.assertNotEqual(issue_again.move_ids.state, 'done')
        self.assertEqual(issue_again.move_ids.jakh_material_request_id, demand)
        self.assertFalse(issue_again.move_ids.origin_returned_move_id)
        self.assertFalse(issue_again.move_ids.jakh_material_return_id)
        self.assertFalse(issue_again.move_ids.account_move_id)

    def test_cancelled_unposted_return_transfer_can_be_reprepared_with_audit(self):
        import json
        demand, issue = self._issued_demand()
        original_journal = issue.account_move_id
        document = self._return_document(issue, 2)
        document.action_approve()
        Picking = self.env['stock.picking'].with_user(self.manager)
        first = Picking.browse(document.action_prepare_picking()['res_id'])
        first.action_cancel()
        self.assertEqual(first.state, 'cancel')
        replacement = Picking.browse(document.action_prepare_picking()['res_id'])
        self.assertNotEqual(first, replacement)
        self.assertEqual(replacement.move_ids.product_uom_qty, 2)
        self.assertEqual(document.action_prepare_picking()['res_id'], replacement.id)
        evidence = self.env['jakh.audit.event'].sudo().search([
            ('resource_model', '=', document._name), ('resource_id', '=', document.id),
            ('operation', '=', 'material_return.transfer_prepared')], order='id desc', limit=1)
        self.assertEqual(json.loads(evidence.details)['previous_picking_ids'], first.ids)
        replacement.move_ids.write({'quantity': 2, 'picked': True})
        replacement.move_ids._action_done()
        self.assertEqual(document.state, 'done')
        self.assertEqual(document.action_prepare_picking()['res_id'], replacement.id)
        self.assertEqual(issue.quantity, 5)
        self.assertEqual(issue.account_move_id, original_journal)
        self.assertEqual(demand.issued_quantity, 3)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(sorted(facts.mapped('amount')), [-20, 50])

    def test_reprepared_partial_return_uses_only_unreturned_approved_quantity(self):
        demand, issue = self._issued_demand()
        document = self._return_document(issue, 3)
        document.action_approve()
        Picking = self.env['stock.picking'].with_user(self.manager)
        first = Picking.browse(document.action_prepare_picking()['res_id'])
        first.move_ids.write({'quantity': 1, 'picked': True})
        first.move_ids._action_done(cancel_backorder=True)
        self.assertEqual(document.state, 'approved')
        remaining = Picking.browse(document.action_prepare_picking()['res_id'])
        self.assertNotEqual(first, remaining)
        self.assertEqual(remaining.move_ids.product_uom_qty, 2)
        remaining.move_ids.write({'quantity': 2, 'picked': True})
        remaining.move_ids._action_done()
        self.assertEqual(document.state, 'done')
        self.assertEqual(demand.issued_quantity, 2)
        self.assertEqual(document._remaining_return_quantity(), 0)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'actual_cost')])
        self.assertEqual(sorted(facts.mapped('amount')), [-20, -10, 50])

    def test_project_and_wbs_accounting_identity_cannot_be_reassigned(self):
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.wbs.write({'analytic_account_id': False})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.project.write({'account_id': False})
        other_company = self.env['res.company'].create({'name': 'Synthetic different legal entity'})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.project.write({'company_id': other_company.id})
