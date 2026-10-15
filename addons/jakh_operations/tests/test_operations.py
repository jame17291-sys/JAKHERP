from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.exceptions import AccessError, UserError


@tagged('post_install', '-at_install', 'jakh')
class OperationsTests(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='ops_manager', groups='jakh_core.group_manager', company_id=cls.env.company.id)
        cls.technical = new_test_user(cls.env, login='ops_technical', groups='jakh_core.group_technical', company_id=cls.env.company.id)
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Operations test project', 'company_id': cls.env.company.id, 'user_id': cls.manager.id})
        cls.project.message_subscribe(partner_ids=[cls.technical.partner_id.id])
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Concrete', 'code': 'OPS-01', 'project_id': cls.project.id, 'budget_amount': 1000})
        cls.wbs.action_release()
        cls.product = cls.env['product.product'].create({'name': 'Operations material', 'type': 'consu', 'purchase_ok': True})
        cls.vendor = cls.env['res.partner'].create({'name': 'Operations supplier'})

    def request(self, price=100):
        request = self.env['jakh.purchase.request'].with_user(self.manager).create({'name': 'OPS-RQ', 'project_id': self.project.id, 'wbs_id': self.wbs.id, 'material_plan_reference': 'Approved plan TEST', 'supplier_id': self.vendor.id,
            'line_ids': [Command.create({'name': 'Concrete supply', 'product_id': self.product.id, 'uom_id': self.product.uom_id.id, 'quantity': 5})]})
        self.env['jakh.purchase.request.cost'].with_user(self.manager).create({'line_id': request.line_ids.id, 'unit_price': price})
        return request

    def test_technical_can_edit_quantities_but_not_cost_or_state(self):
        request = self.request()
        self.cr.execute('SELECT ctid::text FROM jakh_purchase_request WHERE id=%s', [request.id])
        previous_version = self.cr.fetchone()[0]
        request.line_ids.with_user(self.technical).write({'quantity': 4})
        self.cr.execute('SELECT ctid::text FROM jakh_purchase_request WHERE id=%s', [request.id])
        self.assertNotEqual(self.cr.fetchone()[0], previous_version, 'A child edit must advance the approval parent row version.')
        with self.assertRaises(AccessError), self.cr.savepoint():
            request.cost_ids.with_user(self.technical).read(['unit_price'])
        with self.assertRaises(AccessError), self.cr.savepoint():
            request.with_user(self.technical).write({'state': 'approved'})
        request.action_submit(); request.action_approve()
        with self.assertRaises(UserError), self.cr.savepoint():
            request.line_ids.with_user(self.technical).write({'quantity': 9})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.env['jakh.purchase.request.line'].with_user(self.technical).create({'request_id': request.id,
                'name': 'Forbidden late quantity', 'product_id': self.product.id, 'uom_id': self.product.uom_id.id, 'quantity': 1})
        with self.assertRaises(UserError), self.cr.savepoint():
            request.cost_ids.write({'unit_price': 999})

    def test_budget_reservations_block_overspend_and_po_handover_is_idempotent(self):
        first = self.request(); first.action_submit(); first.action_approve()
        first.action_release(); order = first.purchase_order_id
        first.action_release()
        self.assertEqual(first.purchase_order_id, order)
        self.assertEqual(order.order_line.jakh_request_line_id, first.line_ids)
        order.button_confirm()
        self.assertEqual(order.state, 'purchase')
        self.assertTrue(order.picking_ids)
        second = self.request(price=101); second.action_submit()
        with self.assertRaises(UserError), self.cr.savepoint():
            second.action_approve()
        with self.assertRaises(AccessError), self.cr.savepoint():
            order.order_line.write({'price_unit': 999})

    def test_server_fields_and_financial_report_permissions(self):
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.payment.certificate'].with_user(self.manager).create({'state': 'approved'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.financial.report'].with_user(self.technical).create({'report_type': 'trial_balance'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.financial.report.line'].with_user(self.manager).create({'balance': 999})
        wizard = self.env['jakh.financial.report'].with_user(self.manager).create({'report_type': 'trial_balance'})
        account = self.env['account.account'].create({'name': 'Synthetic output guard account', 'code': 'OPS-OUTPUT',
            'account_type': 'asset_current', 'company_ids': [Command.set(self.env.company.ids)]})
        output = self.env['jakh.financial.report.line'].with_user(self.manager)._create_report([
            {'wizard_id': wizard.id, 'account_id': account.id, 'balance': 1}])
        with self.assertRaises(AccessError), self.cr.savepoint():
            output.with_user(self.manager).unlink()
        with self.assertRaises(AccessError), self.cr.savepoint():
            output.with_user(self.technical).read(['balance'])
        other_manager = new_test_user(self.env, login='ops_other_manager', groups='jakh_core.group_manager', company_id=self.env.company.id)
        with self.assertRaises(AccessError), self.cr.savepoint():
            wizard.with_user(other_manager).read(['report_type'])
        with self.assertRaises(AccessError), self.cr.savepoint():
            output.with_user(other_manager).read(['balance'])

    def test_po_source_context_aliases_append_move_and_deletion_are_blocked(self):
        request = self.request(); request.action_submit(); request.action_approve(); request.action_release()
        orders = self.env['purchase.order'].with_user(self.manager)
        lines = self.env['purchase.order.line'].with_user(self.manager)
        with self.assertRaises(AccessError), self.cr.savepoint():
            orders.with_context(default_jakh_request_id=request.id).create({'partner_id': self.vendor.id})
        manual = orders.create({'partner_id': self.vendor.id, 'company_id': self.env.company.id})
        values = {'order_id': manual.id, 'name': 'Manual supply', 'product_id': self.product.id,
                  'uom_id': self.product.uom_id.id, 'product_qty': 1, 'price_unit': 1}
        with self.assertRaises(AccessError), self.cr.savepoint():
            lines.with_context(default_jakh_request_line_id=request.line_ids.id).create(values)
        ordinary = lines.create(values)
        with self.assertRaises(AccessError), self.cr.savepoint():
            ordinary.write({'order_id': request.purchase_order_id.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            lines.create({**values, 'order_id': request.purchase_order_id.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            request.purchase_order_id.unlink()
        self.assertEqual(request.purchase_order_id.order_line.jakh_request_line_id, request.line_ids)
