"""Three-way material matching against approved PO and actual native receipts.

Authority comes from the approved requisition/PO product, not the submitted bill
product or manually editable received counters. PO-line row versions serialize
receipt, return, invoice and credit batches under native repeatable-read isolation.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import math

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError

from .procurement import lock_records

QUANTITY_UNIT = Decimal('0.000001')
_BILL_POST = object()
_STOCK_POST = object()
_VALUE_POST = object()


def _native_material_value_refresh(env):
    """Private capability granted only by validated native valuation calls."""
    return env.context.get('_jakh_material_receipt_value') is _VALUE_POST


def _number(value):
    if not math.isfinite(float(value or 0)):
        raise UserError(_('Material quantities and rates must be finite numbers.'))
    return Decimal(str(value or 0))


def _quantity(value):
    number = _number(value)
    try:
        normalized = number.quantize(QUANTITY_UNIT)
    except InvalidOperation as error:
        raise UserError(_('Material quantities must use the approved six-decimal precision.')) from error
    # Accommodate binary-float noise only, not an extra commercial tolerance.
    if abs(number - normalized) > Decimal('0.000000000001'):
        raise UserError(_('Material quantities must use the approved six-decimal precision.'))
    return normalized


def _materials(lines):
    # Never let a substituted service product on a bill bypass material control.
    return lines.filtered(lambda line: line.jakh_request_line_id and not line._jakh_service_lines())


def _material_bill(moves):
    return moves.filtered(lambda move: _materials(move.invoice_line_ids.purchase_line_id))


def _approved_order(purchase):
    purchase.ensure_one(); purchase.check_access('read')
    order = purchase.order_id
    source = purchase.jakh_request_line_id
    request = source.request_id
    request.project_id._jakh_assert_open()
    if (order.state not in ('purchase', 'done') or request.state != 'released'
            or request.purchase_order_id != order or order.jakh_request_id != request
            or purchase.company_id != request.company_id or order.currency_id != request.currency_id
            or purchase.product_id != source.product_id or purchase.uom_id != source.uom_id
            or _quantity(purchase.product_qty) != _quantity(source.quantity)):
        raise UserError(_('Material matching requires the confirmed, unchanged approved requisition and purchase order.'))


def _counted_move(move):
    return bool(move._is_purchase_return() or move._should_count_for_quantity_received())


def _move_scope(move, purchase):
    if (move.purchase_line_id != purchase or move.product_id != purchase.product_id
            or move.uom_id != purchase.uom_id or move.company_id != purchase.company_id
            or not move.picking_id or move.picking_id.company_id != purchase.company_id
            or move.picking_id.partner_id.commercial_partner_id != purchase.order_id.partner_id.commercial_partner_id
            or _quantity(move.quantity) <= 0):
        raise UserError(_('A material receipt or supplier return must match its approved PO product, unit, company and supplier.'))
    if move._is_purchase_return():
        original = move.origin_returned_move_id
        if (not original or original.state != 'done' or original.purchase_line_id != purchase
                or original._is_purchase_return() or original.product_id != purchase.product_id
                or original.uom_id != purchase.uom_id or original.company_id != purchase.company_id
                or move.location_dest_id != original.location_id):
            raise UserError(_('Return purchased material against its original completed PO receipt; do not infer or replace historical source links.'))


def _received(purchase):
    total = Decimal()
    for move in purchase.move_ids.filtered(lambda row: row.state == 'done' and _counted_move(row)):
        _move_scope(move, purchase)
        total += (-1 if move._is_purchase_return() else 1) * _quantity(move.quantity)
    return total


class MaterialPurchaseLine(models.Model):
    _inherit = 'purchase.order.line'

    def write(self, values):
        if (_materials(self) and set(values) & {'qty_received', 'qty_received_manual', 'qty_received_method'}
                and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST):
            raise AccessError(_('Material received quantities derive from completed native receipts and supplier returns.'))
        return super().write(values)

    def _update_qty_received_method(self):
        return super(MaterialPurchaseLine, self.with_context(_jakh_material_receipt_post=_STOCK_POST))._update_qty_received_method()


class MaterialBill(models.Model):
    _inherit = 'account.move'

    def _check_material_three_way_match(self):
        if _material_bill(self).filtered(lambda move: move.move_type not in ('in_invoice', 'in_refund')):
            raise UserError(_('Material PO references belong to supplier bills or their controlled native credits.'))
        incoming = self.filtered(lambda move: move.move_type in ('in_invoice', 'in_refund') and move.state != 'posted')
        lock_records(incoming, touch=True)
        lines = incoming.invoice_line_ids.filtered(lambda line: _materials(line.purchase_line_id))
        purchases = lines.purchase_line_id
        lock_records(purchases, touch=True)
        quantities = defaultdict(Decimal)
        amounts = defaultdict(Decimal)
        credits = defaultdict(Decimal)
        credit_amounts = defaultdict(Decimal)
        for line in lines:
            purchase, move = line.purchase_line_id, line.move_id
            _approved_order(purchase)
            if (move.company_id != purchase.company_id or move.company_id not in self.env.companies
                    or move.partner_id.commercial_partner_id != purchase.order_id.partner_id.commercial_partner_id
                    or move.currency_id != purchase.currency_id or move.journal_id.company_id != purchase.company_id
                    or move.journal_id.type != 'purchase'):
                raise UserError(_('A material supplier bill must retain the confirmed PO supplier, company, currency and purchase journal.'))
            if (line.display_type not in (False, 'product') or line.product_id != purchase.product_id or line.product_uom_id != purchase.uom_id
                    or _number(line.price_unit) != _number(purchase.price_unit)
                    or _number(line.discount) != _number(purchase.discount)
                    or set(line.tax_ids.ids) != set(purchase.tax_ids.ids) or _quantity(line.quantity) <= 0):
                raise UserError(_('A material bill must exactly match its approved PO product, unit, rate, discount and taxes with positive quantities.'))
            expected = _quantity(line.quantity) * _number(purchase.price_unit) * (1 - _number(purchase.discount) / 100)
            if move.currency_id.compare_amounts(line.price_subtotal, float(expected)):
                raise UserError(_('Material bill value must equal approved quantity and rate at accounting currency precision.'))
            sign = -1 if move.move_type == 'in_refund' else 1
            quantities[purchase.id] += sign * _quantity(line.quantity)
            amounts[purchase.id] += sign * _number(line.price_subtotal)
            if sign < 0:
                original = move.reversed_entry_id
                if (not original or original.state != 'posted' or original.move_type != 'in_invoice'
                        or original.company_id != move.company_id or original.currency_id != move.currency_id
                        or original.partner_id.commercial_partner_id != move.partner_id.commercial_partner_id):
                    raise UserError(_('Correct material billing through a native supplier credit linked to the original posted bill.'))
                credits[(original.id, purchase.id)] += _quantity(line.quantity)
                credit_amounts[(original.id, purchase.id)] += _number(line.price_subtotal)
        Line = self.env['account.move.line']
        for purchase in purchases:
            historical = Line.search([('purchase_line_id', '=', purchase.id), ('move_id.state', '=', 'posted'),
                                      ('move_id.move_type', 'in', ['in_invoice', 'in_refund'])])
            billed = sum(((-1 if line.move_id.move_type == 'in_refund' else 1) * _quantity(line.quantity)
                          for line in historical), Decimal())
            total = billed + quantities[purchase.id]
            received = _received(purchase)
            if total < 0 or total > received or total > _quantity(purchase.product_qty):
                raise UserError(_('Net material billing cannot exceed actual completed receipts less supplier returns or the approved PO quantity.'))
            billed_amount = sum(((-1 if line.move_id.move_type == 'in_refund' else 1) * _number(line.price_subtotal)
                                 for line in historical), Decimal()) + amounts[purchase.id]
            receipt_value = received * _number(purchase.price_unit) * (1 - _number(purchase.discount) / 100)
            if (purchase.currency_id.compare_amounts(float(billed_amount), 0) < 0
                    or purchase.currency_id.compare_amounts(float(billed_amount), float(receipt_value)) > 0
                    or purchase.currency_id.compare_amounts(float(billed_amount), purchase.price_subtotal) > 0):
                raise UserError(_('Net material bill value cannot exceed approved received value or the PO value at accounting currency precision.'))
        for (original_id, purchase_id), current_credit in credits.items():
            original = self.browse(original_id)
            original_quantity = sum((_quantity(line.quantity) for line in original.invoice_line_ids
                                     if line.purchase_line_id.id == purchase_id), Decimal())
            historical = Line.search([('purchase_line_id', '=', purchase_id), ('move_id.state', '=', 'posted'),
                                      ('move_id.move_type', '=', 'in_refund'), ('move_id.reversed_entry_id', '=', original_id)])
            original_amount = sum((_number(line.price_subtotal) for line in original.invoice_line_ids
                                   if line.purchase_line_id.id == purchase_id), Decimal())
            credited_amount = credit_amounts[(original_id, purchase_id)] + sum((_number(line.price_subtotal) for line in historical), Decimal())
            if (current_credit + sum((_quantity(line.quantity) for line in historical), Decimal()) > original_quantity
                    or original.currency_id.compare_amounts(float(credited_amount), float(original_amount)) > 0):
                raise UserError(_('Material credits cannot exceed the uncredited quantities of their original posted supplier bill.'))

    def _post(self, soft=True):
        self._check_material_three_way_match()
        return super(MaterialBill, self.with_context(_jakh_material_bill_post=_BILL_POST))._post(soft=soft)

    def write(self, values):
        controlled = _material_bill(self)
        trusted = self.env.context.get('_jakh_material_bill_post') is _BILL_POST
        if controlled and values.get('state') == 'posted' and not trusted:
            raise AccessError(_('Post material bills through native validated three-way matching.'))
        protected = {'state', 'company_id', 'journal_id', 'currency_id', 'partner_id', 'move_type',
                     'date', 'invoice_date', 'reversed_entry_id', 'invoice_payment_term_id', 'fiscal_position_id',
                     'invoice_currency_rate', 'line_ids', 'invoice_line_ids'}
        if controlled.filtered(lambda move: move.state == 'posted') and set(values) & protected and not trusted:
            raise AccessError(_('Posted material bills preserve their approved source and commercial identity. Use a native supplier credit.'))
        return super().write(values)

    def button_draft(self):
        if _material_bill(self).filtered(lambda move: move.state == 'posted'):
            raise UserError(_('Correct a posted material bill through a native supplier credit.'))
        return super().button_draft()

    def button_cancel(self):
        if _material_bill(self).filtered(lambda move: move.state == 'posted'):
            raise UserError(_('Correct a posted material bill through a native supplier credit.'))
        return super().button_cancel()

    def unlink(self):
        if _material_bill(self).filtered(lambda move: move.state == 'posted'):
            raise AccessError(_('Retain posted material bill and credit evidence.'))
        return super().unlink()


class MaterialBillLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, values_list):
        move_ids = [values.get('move_id') or self.env.context.get('default_move_id') for values in values_list]
        moves = self.env['account.move'].browse([value for value in move_ids if value]).exists()
        purchase_ids = [values.get('purchase_line_id') or self.env.context.get('default_purchase_line_id') for values in values_list]
        purchases = _materials(self.env['purchase.order.line'].browse([value for value in purchase_ids if value]).exists())
        if purchases or _material_bill(moves):
            lock_records(moves, touch=True); lock_records(purchases, budget=True, touch=True)
        if self.env.context.get('_jakh_material_bill_post') is not _BILL_POST:
            for values in values_list:
                move = self.env['account.move'].browse(values.get('move_id') or self.env.context.get('default_move_id')).exists()
                purchase = self.env['purchase.order.line'].browse(values.get('purchase_line_id') or self.env.context.get('default_purchase_line_id')).exists()
                if move and move.state == 'posted' and (_material_bill(move) or _materials(purchase)):
                    raise AccessError(_('Posted material bills cannot receive additional journal lines.'))
        return super().create(values_list)

    def write(self, values):
        scoped = self.filtered(lambda line: _materials(line.purchase_line_id) or _material_bill(line.move_id))
        if scoped and self.env.context.get('_jakh_material_bill_post') is not _BILL_POST:
            lock_records(scoped.move_id, touch=True); lock_records(_materials(scoped.purchase_line_id), budget=True, touch=True)
        if 'purchase_line_id' in values and self.filtered(lambda line: _materials(line.purchase_line_id)
                                                        and line.purchase_line_id.id != values['purchase_line_id']):
            raise AccessError(_('A material bill line retains its approved purchase-order source.'))
        if ('purchase_line_id' in values and _materials(self.env['purchase.order.line'].browse(values['purchase_line_id']).exists())
                and self.filtered(lambda line: line.move_id.state == 'posted')):
            raise AccessError(_('A material bill line retains its approved purchase-order source.'))
        trusted = self.env.context.get('_jakh_material_bill_post') is _BILL_POST
        target = self.env['account.move'].browse(values.get('move_id')).exists()
        if target and target.state == 'posted' and _material_bill(target) and not trusted:
            raise AccessError(_('Posted material bills cannot receive additional journal lines.'))
        protected = {'move_id', 'account_id', 'partner_id', 'company_id', 'currency_id', 'journal_id', 'purchase_line_id',
                     'product_id', 'product_uom_id', 'quantity', 'price_unit', 'discount', 'debit', 'credit', 'balance',
                     'amount_currency', 'tax_ids', 'tax_tag_ids', 'analytic_distribution', 'date', 'parent_state', 'display_type'}
        if self.filtered(lambda line: line.move_id.state == 'posted' and _material_bill(line.move_id)) and set(values) & protected and not trusted:
            raise AccessError(_('Posted material bill amounts and source links are immutable. Use a native supplier credit.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda line: line.move_id.state == 'posted' and _material_bill(line.move_id)):
            raise AccessError(_('Retain posted material bill lines and their source evidence.'))
        return super().unlink()


class MaterialReceiptMove(models.Model):
    _inherit = 'stock.move'

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST:
            for values in values_list:
                purchase = self.env['purchase.order.line'].browse(values.get('purchase_line_id') or self.env.context.get('default_purchase_line_id')).exists()
                if _materials(purchase) and (values.get('state') == 'done' or self.env.context.get('default_state') == 'done'):
                    raise AccessError(_('Create draft material transfers and validate them through native stock matching.'))
        return super().create(values_list)

    def _action_done(self, cancel_backorder=False):
        pending = self.filtered(lambda move: move.state not in ('done', 'cancel') and _materials(move.purchase_line_id))
        purchases = pending.purchase_line_id
        lock_records(purchases, budget=True, touch=True)
        quantities = defaultdict(Decimal)
        returns = defaultdict(Decimal)
        for move in pending:
            purchase = move.purchase_line_id
            _approved_order(purchase)
            if not _counted_move(move):
                if move.product_id != purchase.product_id or move.uom_id != purchase.uom_id or move.company_id != purchase.company_id:
                    raise UserError(_('A material receipt or supplier return must match its approved PO product, unit, company and supplier.'))
                continue  # Native internal routing legs do not count twice.
            _move_scope(move, purchase)
            sign = -1 if move._is_purchase_return() else 1
            quantities[purchase.id] += sign * _quantity(move.quantity)
            if sign < 0:
                returns[move.origin_returned_move_id.id] += _quantity(move.quantity)
        for purchase in purchases:
            net = _received(purchase) + quantities[purchase.id]
            if net < 0 or net > _quantity(purchase.product_qty):
                raise UserError(_('Combined material receipts less supplier returns cannot exceed the approved PO quantity or become negative.'))
        for original_id, quantity in returns.items():
            original = self.browse(original_id)
            historical = self.search([('origin_returned_move_id', '=', original_id), ('state', '=', 'done')])
            returned = sum((_quantity(move.quantity) for move in historical if move._is_purchase_return()), Decimal())
            if quantity + returned > _quantity(original.quantity):
                raise UserError(_('Supplier returns cannot exceed the unreturned quantity of the original completed material receipt.'))
        return super(MaterialReceiptMove, self.with_context(_jakh_material_receipt_post=_STOCK_POST))._action_done(cancel_backorder=cancel_backorder)

    def _set_value(self, recompute_date=None, skip_check=False):
        # Native supplier-bill posting refreshes incoming stock values after its
        # journal posts. Permit that private, already-matched calculation only;
        # it cannot grant permission to edit quantity, provenance or GL evidence.
        trusted = (self.env.context.get('_jakh_material_bill_post') is _BILL_POST
                   or self.env.context.get('_jakh_material_receipt_post') is _STOCK_POST)
        target = self.with_context(_jakh_material_receipt_value=_VALUE_POST) if trusted else self
        return super(MaterialReceiptMove, target)._set_value(recompute_date=recompute_date, skip_check=skip_check)

    def write(self, values):
        controlled = self.filtered(lambda move: _materials(move.purchase_line_id))
        if controlled and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST:
            lock_records(controlled.purchase_line_id, budget=True, touch=True)
        if 'purchase_line_id' in values and controlled.filtered(lambda move: move.purchase_line_id.id != values['purchase_line_id']):
            raise AccessError(_('Material receipt and supplier-return PO links cannot be reassigned or removed.'))
        if ('purchase_line_id' in values and _materials(self.env['purchase.order.line'].browse(values['purchase_line_id']).exists())
                and self.filtered(lambda move: move.state == 'done')):
            raise AccessError(_('Material receipt and supplier-return PO links cannot be reassigned or removed.'))
        protected = {'state', 'product_id', 'uom_id', 'product_uom_qty', 'quantity', 'company_id', 'location_id',
                     'location_dest_id', 'picking_id', 'origin_returned_move_id', 'purchase_line_id', 'date', 'value', 'price_unit', 'account_move_id', 'to_refund'}
        if controlled and values.get('state') == 'done' and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST:
            raise AccessError(_('Validate material receipts through the native stock action, not a direct state edit.'))
        value_only = self.env.context.get('_jakh_material_receipt_value') is _VALUE_POST and set(values) <= {'value'}
        if (controlled.filtered(lambda move: move.state == 'done') and set(values) & protected
                and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST and not value_only):
            raise AccessError(_('Completed material receipts are immutable. Use their native supplier-return workflow.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda move: move.state == 'done' and _materials(move.purchase_line_id)):
            raise AccessError(_('Retain completed material receipts and their source evidence.'))
        return super().unlink()


class MaterialReceiptLine(models.Model):
    _inherit = 'stock.move.line'

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST:
            for values in values_list:
                move = self.env['stock.move'].browse(values.get('move_id') or self.env.context.get('default_move_id')).exists()
                if move and _materials(move.purchase_line_id):
                    lock_records(move, budget=True, touch=True); lock_records(move.purchase_line_id, budget=True, touch=True)
                    if move.state == 'done' or 'state' in values or self.env.context.get('default_state'):
                        raise AccessError(_('Material receipt operation lines derive from native validated stock moves.'))
        return super().create(values_list)

    def write(self, values):
        scoped = self.filtered(lambda line: _materials(line.move_id.purchase_line_id))
        if scoped and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST:
            lock_records(scoped.move_id, budget=True, touch=True); lock_records(scoped.move_id.purchase_line_id, budget=True, touch=True)
        if (self.filtered(lambda line: _materials(line.move_id.purchase_line_id)) and 'state' in values
                and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST):
            raise AccessError(_('Material receipt operation-line states derive from native stock validation.'))
        if 'move_id' in values:
            target = self.env['stock.move'].browse(values['move_id']).exists()
            if _materials(target.purchase_line_id) and (target.state == 'done' or self.filtered(lambda line: line.move_id.state == 'done')):
                raise AccessError(_('Completed material receipt operation lines are immutable. Use a native supplier return.'))
        protected = {'move_id', 'picking_id', 'quantity', 'product_id', 'uom_id', 'company_id',
                     'location_id', 'location_dest_id', 'lot_id', 'lot_name', 'package_id', 'result_package_id', 'owner_id', 'state'}
        if (self.filtered(lambda line: line.move_id.state == 'done' and _materials(line.move_id.purchase_line_id))
                and set(values) & protected and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST):
            raise AccessError(_('Completed material receipt operation lines are immutable. Use a native supplier return.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda line: line.move_id.state == 'done' and _materials(line.move_id.purchase_line_id)):
            raise AccessError(_('Retain completed material receipt operation lines.'))
        return super().unlink()


class MaterialReceiptPicking(models.Model):
    _inherit = 'stock.picking'

    def write(self, values):
        if (self.filtered(lambda picking: picking.state == 'done' and _materials(picking.move_ids.purchase_line_id))
                and set(values) & {'company_id', 'partner_id', 'location_id', 'location_dest_id', 'picking_type_id', 'move_ids', 'move_line_ids'}
                and self.env.context.get('_jakh_material_receipt_post') is not _STOCK_POST):
            raise AccessError(_('Completed material picking identity is immutable. Use its native supplier-return workflow.'))
        return super().write(values)
