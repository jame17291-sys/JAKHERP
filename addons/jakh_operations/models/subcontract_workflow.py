"""Subcontract work acceptance, native payable matching and PO amendments.

Approved requisition rates and taxes define the matching policy: the product,
unit, rate and tax scope must agree exactly; there is no hidden tolerance.  SES
acceptance and payable posting touch the same PO-line row so a concurrent
transaction cannot reserve the same accepted work from a stale snapshot.
"""
from collections import defaultdict
from decimal import Decimal

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role
from .procurement import PurchaseOrderLine as ApprovedPurchaseOrderLine
from .procurement import PurchaseRequest as ApprovedPurchaseRequest
from .execution_controls import SubcontractServiceEntry as OriginalServiceEntry


QUANTITY_EPSILON = Decimal('0.000001')
_CONTROLLED_CANCEL = object()


def _decimal(value):
    return Decimal(str(value or 0))


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    jakh_amendment_id = fields.Many2one('jakh.subcontract.amendment', readonly=True, copy=False, check_company=True)

    def _jakh_service_lines(self):
        # Derive authority from the project link, never from a writable flag.
        return self.filtered(lambda line: line.jakh_request_line_id and (line.jakh_requires_ses or line.product_id.type == 'service'))

    @api.model_create_multi
    def create(self, values_list):
        if any(set(values) & {'jakh_requires_ses', 'jakh_amendment_id'} for values in values_list):
            raise AccessError(_('Subcontract matching and amendment references are assigned by server actions.'))
        return super().create(values_list)

    def _create_jakh(self, values_list):
        values_list = [dict(values) for values in values_list]
        for values in values_list:
            if set(values) & {'jakh_requires_ses', 'jakh_amendment_id'}:
                raise AccessError(_('Subcontract matching and amendment references are assigned by server actions.'))
            product = self.env['product.product'].browse(values.get('product_id'))
            if values.get('jakh_request_line_id') and product.type == 'service':
                values['jakh_requires_ses'] = True
        return super()._create_jakh(values_list)

    def write(self, values):
        if set(values) & {'jakh_requires_ses', 'jakh_amendment_id'}:
            raise AccessError(_('Subcontract matching and amendment references are assigned by server actions.'))
        if self._jakh_service_lines() and set(values) & {'qty_received', 'qty_received_manual'}:
            raise AccessError(_('Approve or reverse service entries to change accepted subcontract work.'))
        return super().write(values)

    def _jakh_service_totals(self):
        """Return accepted net work and posted payable work in the PO unit."""
        self.ensure_one(); self.check_access('read')
        accepted = self.env['jakh.subcontract.service.entry'].search([
            ('purchase_line_id', '=', self.id), ('state', '=', 'approved'),
        ])
        reversals = self.env['jakh.subcontract.service.reversal'].search([
            ('purchase_line_id', '=', self.id), ('state', '=', 'approved'),
        ])
        accepted_quantity = sum((_decimal(row.quantity) for row in accepted), Decimal())
        accepted_amount = sum((_decimal(row.accepted_amount) for row in accepted), Decimal())
        accepted_quantity -= sum((_decimal(row.quantity) for row in reversals), Decimal())
        accepted_amount -= sum((_decimal(row.reversed_amount) for row in reversals), Decimal())
        posted = self.env['account.move.line'].search([
            ('purchase_line_id', '=', self.id), ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
        ])
        billed_quantity = sum(((-1 if line.move_id.move_type == 'in_refund' else 1) *
                               _decimal(line.quantity) for line in posted), Decimal())
        billed_amount = sum(((-1 if line.move_id.move_type == 'in_refund' else 1) *
                             _decimal(line.price_subtotal) for line in posted), Decimal())
        return accepted_quantity, accepted_amount, billed_quantity, billed_amount

    def _sync_jakh_service_receipt(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for line in self._jakh_service_lines():
            accepted, _amount, _billed, _billed_amount = line._jakh_service_totals()
            # Bypass the original immutable requisition guard only for this
            # native received quantity; no product/rate/tax field is changed.
            super(ApprovedPurchaseOrderLine, line).write({
                'jakh_requires_ses': True, 'qty_received_manual': float(accepted),
            })


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    jakh_subcontract_amendment_ids = fields.One2many('jakh.subcontract.amendment', 'purchase_order_id', readonly=True)

    def write(self, values):
        if values.get('state') == 'cancel' and self.filtered('jakh_request_id') and self.env.context.get('_jakh_subcontract_cancel') is not _CONTROLLED_CANCEL:
            raise AccessError(_('Cancel a project purchase order through its controlled reservation workflow.'))
        if values.get('state') in ('draft', 'sent', 'to approve', 'purchase') and self.filtered(
                lambda order: order.jakh_request_id and order.jakh_request_id.state == 'cancelled'):
            raise UserError(_('Cancelled project purchase orders cannot be reopened. Create a new approved requisition.'))
        return super().write(values)

    def button_confirm(self):
        for line in self.order_line._jakh_service_lines():
            lock_records(line, touch=True)
            # Includes old installed orders whose matching flag predates SES.
            super(ApprovedPurchaseOrderLine, line).write({'jakh_requires_ses': True})
        return super().button_confirm()

    def button_draft(self):
        if self.jakh_subcontract_amendment_ids.filtered(lambda row: row.state == 'approved'):
            raise UserError(_('An amended subcontract order must be corrected through another approved amendment.'))
        return super().button_draft()

    def _cancel_jakh_subcontract_native(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super(PurchaseOrder, self.with_context(_jakh_subcontract_cancel=_CONTROLLED_CANCEL)).button_cancel()

    def button_cancel(self):
        linked = self.filtered('jakh_request_id')
        lock_records(linked.order_line, touch=True)
        if any(line._jakh_service_totals()[0] > QUANTITY_EPSILON for line in linked.order_line._jakh_service_lines()):
            raise UserError(_('Reverse accepted service work before cancelling its subcontract reservation.'))
        pending = linked.filtered(lambda order: order.state != 'cancel' and order.jakh_request_id.state != 'cancelled')
        if pending:
            # The requisition action cancels natively first, then releases the
            # original and amended commitments in the same transaction.
            pending.jakh_request_id.action_cancel()
        return super(PurchaseOrder, self.with_context(_jakh_subcontract_cancel=_CONTROLLED_CANCEL)).button_cancel()


class SubcontractServiceEntry(models.Model):
    _inherit = 'jakh.subcontract.service.entry'

    reversal_ids = fields.One2many('jakh.subcontract.service.reversal', 'service_entry_id', readonly=True)
    net_accepted_quantity = fields.Float(compute='_compute_net_accepted_quantity', digits=(16, 6))
    invoice_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, groups=FINANCIAL)
    _server_fields = OriginalServiceEntry._server_fields | {'invoice_id'}

    @api.depends('quantity', 'state', 'reversal_ids.state', 'reversal_ids.quantity')
    def _compute_net_accepted_quantity(self):
        for row in self:
            row.net_accepted_quantity = (row.quantity - sum(row.reversal_ids.filtered(
                lambda reversal: reversal.state == 'approved').mapped('quantity'))) if row.state == 'approved' else 0

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        lock_records(self.purchase_line_id, touch=True)
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row)
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.wbs_id.state != 'released':
                raise UserError(_('Approve a submitted service entry against a released WBS.'))
            line = row.purchase_line_id
            if row.company_id not in self.env.companies or row.purchase_order_id.state not in ('purchase', 'done'):
                raise UserError(_('Accept service work only for a confirmed subcontract in the selected company.'))
            if row.purchase_order_id.currency_id != row.company_id.currency_id:
                raise UserError(_('Subcontract service matching currently requires a company-currency purchase order.'))
            accepted, _amount, _billed, _billed_amount = line._jakh_service_totals()
            if accepted + _decimal(row.quantity) > _decimal(line.product_qty) + QUANTITY_EPSILON:
                raise UserError(_('Accepted service quantities cannot exceed the confirmed subcontract quantity.'))
            amount = _decimal(row.quantity) * _decimal(line.price_unit)
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='receipt', amount=amount, source_model=row._name, source_id=row.id,
                source_key='%s:%s:accepted_service' % (row._name, row.id), entry_date=row.service_date,
                quantity=row.quantity, partner=row.partner_id, notes=row.notes,
            )
            row._transition(state='approved', accepted_by=self.env.user.id,
                            accepted_at=fields.Datetime.now(), accepted_amount=float(amount))
            line._sync_jakh_service_receipt()
        return True

    def action_create_bill(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one(); lock_records(self.purchase_line_id, touch=True); lock_records(self)
        if self.invoice_id:
            return form_action(self.invoice_id)
        if self.state != 'approved' or self.net_accepted_quantity <= 0:
            raise UserError(_('Create a supplier bill from approved, unreversed service work.'))
        accepted, _amount, billed, _billed_amount = self.purchase_line_id._jakh_service_totals()
        quantity = min(_decimal(self.net_accepted_quantity), accepted - billed)
        if quantity <= 0:
            raise UserError(_('This subcontract has no accepted quantity left to bill.'))
        values = self.purchase_line_id._prepare_account_move_line()
        values['quantity'] = float(quantity)
        bill = self.env['account.move']._create_jakh_service_bill(self, {
            'move_type': 'in_invoice', 'company_id': self.company_id.id,
            'partner_id': self.partner_id.id, 'currency_id': self.currency_id.id,
            'invoice_date': self.service_date, 'invoice_origin': self.name,
            'invoice_line_ids': [Command.create(values)],
        })
        super(OriginalServiceEntry, self).write({'invoice_id': bill.id})
        self.env['jakh.audit.event']._append(self.company_id, 'subcontract_ses.bill_created', self, {'invoice': bill.id})
        return form_action(bill)

    def action_prepare_reversal(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one(); self.check_access('read')
        if self.state != 'approved' or self.net_accepted_quantity <= 0:
            raise UserError(_('Only approved service work can be reversed.'))
        reversal = self.env['jakh.subcontract.service.reversal'].create({
            'name': self.env._('%s correction', self.name), 'service_entry_id': self.id,
            'quantity': self.net_accepted_quantity, 'reason': self.env._('Document the work correction before submission.'),
        })
        return form_action(reversal)


class SubcontractServiceReversal(models.Model):
    _name = 'jakh.subcontract.service.reversal'
    _description = 'Subcontract service acceptance correction'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New service correction'), tracking=True)
    service_entry_id = fields.Many2one('jakh.subcontract.service.entry', required=True, check_company=True, tracking=True)
    purchase_line_id = fields.Many2one(related='service_entry_id.purchase_line_id', store=True)
    purchase_order_id = fields.Many2one(related='service_entry_id.purchase_order_id', store=True)
    project_id = fields.Many2one(related='service_entry_id.project_id', store=True)
    wbs_id = fields.Many2one(related='service_entry_id.wbs_id', store=True)
    company_id = fields.Many2one(related='service_entry_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    quantity = fields.Float(required=True, digits=(16, 6), tracking=True)
    reason = fields.Text(required=True, tracking=True)
    reversed_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')],
                             default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _server_fields = {'state', 'reversed_amount', 'approved_by', 'approved_at'}
    _positive = models.Constraint('CHECK(quantity > 0)', 'A service correction requires a positive quantity.')

    @api.constrains('service_entry_id', 'date')
    def _check_correction_date(self):
        if self.filtered(lambda row: row.date < row.service_entry_id.service_date):
            raise ValidationError(_('A service correction date cannot precede its original work acceptance date.'))

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_finance'])
        if any(set(values) & self._server_fields for values in values_list):
            raise AccessError(_('Service correction workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if set(values) & (self._server_fields | {'service_entry_id'}):
            raise AccessError(_('Use the controlled service correction actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted service corrections are immutable.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft service corrections can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'subcontract_ses_correction.' + row.state, row,
                                                {'service_entry': row.service_entry_id.id, 'quantity': row.quantity})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row)
            if row.state != 'draft' or row.service_entry_id.state != 'approved' or not row.reason.strip():
                raise UserError(_('Submit a documented correction of approved service work.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        lock_records(self.purchase_line_id, touch=True)
        for row in self:
            lock_records(row)
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.company_id not in self.env.companies:
                raise UserError(_('Approve a submitted service correction in the selected company.'))
            original = row.service_entry_id
            if original.state != 'approved' or _decimal(row.quantity) > _decimal(original.net_accepted_quantity) + QUANTITY_EPSILON:
                raise UserError(_('A correction cannot exceed the remaining accepted quantity of its service entry.'))
            accepted, _amount, billed, _billed_amount = row.purchase_line_id._jakh_service_totals()
            if accepted - _decimal(row.quantity) + QUANTITY_EPSILON < billed:
                raise UserError(_('Credit the posted supplier bill before reversing billed service work.'))
            amount = _decimal(original.accepted_amount) * _decimal(row.quantity) / _decimal(original.quantity)
            self.env['jakh.project.control.entry']._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='receipt', amount=-amount, quantity=-row.quantity,
                source_model=row._name, source_id=row.id, source_key='%s:%s:service_reversal' % (row._name, row.id),
                entry_date=row.date, partner=original.partner_id, notes=row.reason,
            )
            row._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now(), reversed_amount=float(amount))
            row.purchase_line_id._sync_jakh_service_receipt()
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted service corrections can be rejected.'))
            row._transition(state='rejected')
        return True


class AccountMove(models.Model):
    _inherit = 'account.move'

    jakh_service_entry_id = fields.Many2one('jakh.subcontract.service.entry', readonly=True, copy=False, check_company=True)
    _one_service_bill = models.Constraint('UNIQUE(jakh_service_entry_id)', 'Create a service-entry bill only once.')

    @api.model_create_multi
    def create(self, values_list):
        if any(values.get('jakh_service_entry_id') for values in values_list):
            raise AccessError(_('Create service-entry bills through the approved service entry.'))
        return super().create(values_list)

    def write(self, values):
        if 'jakh_service_entry_id' in values:
            raise AccessError(_('A service-entry bill keeps its original acceptance reference.'))
        return super().write(values)

    def _create_jakh_service_bill(self, service_entry, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        service_entry.ensure_one(); service_entry.check_access('read')
        if service_entry.state != 'approved' or values.get('move_type') != 'in_invoice':
            raise UserError(_('Create a supplier bill from approved, unreversed service work.'))
        return super().create({**values, 'jakh_service_entry_id': service_entry.id})

    def _check_jakh_service_entry_match(self):
        incoming = self.filtered(lambda move: move.move_type in ('in_invoice', 'in_refund') and move.state != 'posted')
        linked = incoming.invoice_line_ids.filtered(lambda line: line.purchase_line_id._jakh_service_lines())
        purchase_lines = linked.purchase_line_id
        lock_records(purchase_lines, touch=True)
        quantities = defaultdict(Decimal)
        amounts = defaultdict(Decimal)
        credits = defaultdict(lambda: defaultdict(Decimal))
        for line in linked:
            purchase = line.purchase_line_id
            move = line.move_id
            if (purchase.order_id.state not in ('purchase', 'done') or move.company_id != purchase.company_id
                    or move.partner_id.commercial_partner_id != purchase.order_id.partner_id.commercial_partner_id
                    or move.currency_id != purchase.currency_id or move.currency_id != move.company_id.currency_id):
                raise UserError(_('A subcontract bill must retain the confirmed order supplier, company and currency.'))
            if (line.product_id != purchase.product_id or line.product_uom_id != purchase.uom_id
                    or abs(_decimal(line.price_unit) - _decimal(purchase.price_unit)) > QUANTITY_EPSILON
                    or line.discount or set(line.tax_ids.ids) != set(purchase.tax_ids.ids) or line.quantity <= 0):
                raise UserError(_('A subcontract bill must match the approved product, unit, rate and taxes with a positive quantity.'))
            sign = Decimal(-1) if move.move_type == 'in_refund' else Decimal(1)
            quantities[purchase.id] += sign * _decimal(line.quantity)
            amounts[purchase.id] += sign * _decimal(line.price_subtotal)
            if move.move_type == 'in_refund':
                original = move.reversed_entry_id
                if (not original or original.state != 'posted' or original.move_type != 'in_invoice'
                        or original.company_id != move.company_id
                        or original.partner_id.commercial_partner_id != move.partner_id.commercial_partner_id):
                    raise UserError(_('Correct subcontract billing through a credit linked to the original posted supplier bill.'))
                credits[(original.id, purchase.id)]['quantity'] += _decimal(line.quantity)
                credits[(original.id, purchase.id)]['amount'] += _decimal(line.price_subtotal)
        for purchase in purchase_lines:
            accepted, accepted_amount, billed, billed_amount = purchase._jakh_service_totals()
            total_quantity = billed + quantities[purchase.id]
            total_amount = billed_amount + amounts[purchase.id]
            if total_quantity < -QUANTITY_EPSILON or total_quantity > accepted + QUANTITY_EPSILON:
                raise UserError(_('A subcontract supplier invoice cannot exceed approved service-entry quantities.'))
            if purchase.currency_id.compare_amounts(float(total_amount), 0) < 0 or purchase.currency_id.compare_amounts(float(total_amount), float(accepted_amount)) > 0:
                raise UserError(_('Subcontract billing cannot exceed the approved accepted value or credit more than was billed.'))
        for (original_id, purchase_id), new_credit in credits.items():
            original = self.browse(original_id)
            original_lines = original.invoice_line_ids.filtered(lambda line: line.purchase_line_id.id == purchase_id)
            historical = self.env['account.move.line'].search([
                ('purchase_line_id', '=', purchase_id), ('move_id.state', '=', 'posted'),
                ('move_id.move_type', '=', 'in_refund'), ('move_id.reversed_entry_id', '=', original_id),
            ])
            credit_qty = new_credit['quantity'] + sum((_decimal(line.quantity) for line in historical), Decimal())
            credit_amount = new_credit['amount'] + sum((_decimal(line.price_subtotal) for line in historical), Decimal())
            original_amount = sum((_decimal(line.price_subtotal) for line in original_lines), Decimal())
            if (credit_qty > sum((_decimal(line.quantity) for line in original_lines), Decimal()) + QUANTITY_EPSILON
                    or original.currency_id.compare_amounts(float(credit_amount), float(original_amount)) > 0):
                raise UserError(_('A subcontract credit cannot exceed its original posted supplier bill.'))


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def write(self, values):
        if 'purchase_line_id' in values and self.filtered(lambda line: line.purchase_line_id._jakh_service_lines()
                                                        and line.purchase_line_id.id != values['purchase_line_id']):
            raise AccessError(_('A subcontract bill line keeps its approved purchase-order reference.'))
        return super().write(values)


class SubcontractAmendment(models.Model):
    _name = 'jakh.subcontract.amendment'
    _description = 'Approved subcontract purchase-order amendment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New subcontract amendment'), tracking=True)
    variation_id = fields.Many2one('jakh.project.variation', required=True, check_company=True, tracking=True)
    purchase_line_id = fields.Many2one('purchase.order.line', required=True, check_company=True, tracking=True)
    purchase_order_id = fields.Many2one(related='purchase_line_id.order_id', store=True)
    project_id = fields.Many2one(related='variation_id.project_id', store=True)
    wbs_id = fields.Many2one(related='variation_id.wbs_id', store=True)
    company_id = fields.Many2one(related='variation_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    reason = fields.Text(required=True)
    applied_line_id = fields.Many2one('purchase.order.line', readonly=True, copy=False, check_company=True)
    commitment_delta = fields.Monetary(readonly=True, groups=FINANCIAL)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')],
                             default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _server_fields = {'state', 'applied_line_id', 'commitment_delta', 'approved_by', 'approved_at'}
    _one_variation = models.Constraint('UNIQUE(variation_id)', 'Apply each approved subcontract variation to an order only once.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance'])
        if any(set(values) & self._server_fields for values in values_list):
            raise AccessError(_('Subcontract amendment workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance'])
        if set(values) & (self._server_fields | {'variation_id', 'purchase_line_id'}):
            raise AccessError(_('Use the controlled subcontract amendment actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted subcontract amendments are immutable.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft subcontract amendments can be deleted.'))
        return super().unlink()

    @api.constrains('variation_id', 'purchase_line_id')
    def _check_scope(self):
        for row in self:
            variation = row.variation_id
            line = row.purchase_line_id
            request = line.jakh_request_line_id.request_id
            if (variation.variation_type != 'subcontract' or not line._jakh_service_lines()
                    or request.project_id != variation.project_id or request.wbs_id != variation.wbs_id
                    or request.company_id != variation.company_id or variation.partner_id != line.order_id.partner_id
                    or variation.uom_id != line.uom_id):
                raise ValidationError(_('A subcontract amendment must match the variation project, WBS, supplier and approved service unit.'))

    @api.constrains('variation_id', 'date')
    def _check_amendment_date(self):
        if self.filtered(lambda row: row.date < row.variation_id.date):
            raise ValidationError(_('A subcontract amendment date cannot precede its approved variation date.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'subcontract_amendment.' + row.state, row,
                                                {'variation': row.variation_id.id, 'commitment_delta': row.commitment_delta})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row)
            if row.state != 'draft' or not row.reason.strip() or row.variation_id.state != 'approved':
                raise UserError(_('Submit a documented subcontract amendment after its variation is approved.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        lock_records(self.wbs_id, budget=True)
        lock_records(self.purchase_line_id, touch=True)
        for row in self:
            lock_records(row); lock_records(row.variation_id, touch=True)
            if row.state == 'approved':
                continue
            if (row.state != 'submitted' or row.variation_id.state != 'approved' or row.wbs_id.state != 'released'
                    or row.purchase_order_id.state not in ('purchase', 'done') or row.company_id not in self.env.companies):
                raise UserError(_('Approve a submitted amendment for an approved variation and confirmed subcontract.'))
            line = row.purchase_line_id
            request = line.jakh_request_line_id.request_id
            lock_records(request, touch=True)
            if line.currency_id != row.currency_id:
                raise UserError(_('Subcontract amendments require a company-currency order.'))
            variation = row.variation_id
            delta = _decimal(variation.amount)
            commitments = Entry._sum(row.wbs_id, ['commitment'])
            if commitments + delta > Entry._effective_budget(row.wbs_id) + Decimal('0.000001'):
                raise UserError(_('Approve additional WBS budget before increasing the subcontract commitment.'))
            if variation.effect == 'omission':
                if abs(_decimal(variation.unit_price) - _decimal(line.price_unit)) > QUANTITY_EPSILON:
                    raise UserError(_('An omission must credit the affected subcontract line at its approved rate.'))
                new_quantity = _decimal(line.product_qty) - _decimal(variation.quantity)
                accepted, _accepted_amount, billed, _billed_amount = line._jakh_service_totals()
                if new_quantity < max(accepted, billed, Decimal()) - QUANTITY_EPSILON:
                    raise UserError(_('A subcontract omission cannot remove accepted or billed work; correct the service and bill first.'))
                super(ApprovedPurchaseOrderLine, line).write({'product_qty': float(new_quantity)})
                applied = line
            elif abs(_decimal(variation.unit_price) - _decimal(line.price_unit)) <= QUANTITY_EPSILON:
                super(ApprovedPurchaseOrderLine, line).write({'product_qty': line.product_qty + variation.quantity})
                applied = line
            else:
                # A different approved rate is a separate line so historical
                # accepted work and its invoice rate are never repriced.
                applied = super(ApprovedPurchaseOrderLine, self.env['purchase.order.line']).create({
                    'order_id': line.order_id.id, 'name': variation.name,
                    'product_id': line.product_id.id, 'uom_id': line.uom_id.id,
                    'product_qty': variation.quantity, 'price_unit': variation.unit_price,
                    'tax_ids': [Command.set(line.tax_ids.ids)], 'date_planned': line.date_planned,
                    'jakh_request_line_id': line.jakh_request_line_id.id,
                    'jakh_requires_ses': True, 'jakh_amendment_id': row.id,
                })
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='commitment', amount=delta, quantity=(-variation.quantity if variation.effect == 'omission' else variation.quantity),
                source_model=row._name, source_id=row.id, source_key='%s:%s:approved_amendment' % (row._name, row.id),
                entry_date=row.date, partner=line.order_id.partner_id, notes=row.reason,
            )
            super(ApprovedPurchaseRequest, request).write({'reserved_amount': request.reserved_amount + float(delta)})
            row._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now(),
                            applied_line_id=applied.id, commitment_delta=float(delta))
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted subcontract amendments can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectVariation(models.Model):
    _inherit = 'jakh.project.variation'

    def action_prepare_subcontract_amendment(self):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance'])
        self.ensure_one(); self.check_access('read')
        if self.variation_type != 'subcontract' or self.state != 'approved':
            raise UserError(_('Prepare an order amendment from an approved subcontract variation.'))
        existing = self.env['jakh.subcontract.amendment'].search([('variation_id', '=', self.id)], limit=1)
        if existing:
            return form_action(existing)
        lines = self.env['purchase.order.line'].search([
            ('jakh_request_line_id.request_id.project_id', '=', self.project_id.id),
            ('jakh_request_line_id.request_id.wbs_id', '=', self.wbs_id.id),
            ('order_id.partner_id', '=', self.partner_id.id), ('order_id.state', 'in', ['purchase', 'done']),
            ('product_id.type', '=', 'service'), ('uom_id', '=', self.uom_id.id),
        ])
        if not lines:
            raise UserError(_('Confirm the affected subcontract order before preparing its amendment.'))
        return {
            'type': 'ir.actions.act_window', 'name': self.env._('Subcontract order amendment'),
            'res_model': 'jakh.subcontract.amendment', 'view_mode': 'form', 'target': 'current',
            'context': {'default_variation_id': self.id, 'default_reason': self.description,
                        **({'default_purchase_line_id': lines.id} if len(lines) == 1 else {})},
        }


class PurchaseRequest(models.Model):
    _inherit = 'jakh.purchase.request'

    def action_cancel(self):
        # SES is not a stock picking: include it explicitly in the existing
        # cancellation guard and reverse any unreceived amended commitments.
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        active = self.filtered(lambda request: request.state != 'cancelled')
        active._check_company_scope()
        lock_records(active.wbs_id, budget=True)
        lock_records(active.purchase_order_id.order_line, touch=True)
        for request in active:
            if any(line._jakh_service_totals()[0] > QUANTITY_EPSILON for line in request.purchase_order_id.order_line._jakh_service_lines()):
                raise UserError(_('Reverse accepted service work before cancelling its subcontract reservation.'))
            if (request.purchase_order_id.picking_ids.filtered(lambda picking: picking.state == 'done')
                    or request.purchase_order_id.invoice_ids.filtered(lambda move: move.state == 'posted')):
                raise UserError(_('Received or invoiced commitments require native return/credit workflows; they cannot release this reservation.'))
        # A priced draft or submitted PR has not reserved any commitment yet.
        # Cancelling it must never manufacture a negative ledger fact.
        unreserved = active.filtered(lambda request: request.state in ('draft', 'submitted'))
        for request in unreserved:
            lock_records(request)
            request._transition(state='cancelled', reserved_amount=0)
        reserved = active - unreserved
        for order in reserved.purchase_order_id.filtered(lambda row: row.state != 'cancel'):
            order._cancel_jakh_subcontract_native()
        result = super(PurchaseRequest, reserved).action_cancel() if reserved else True
        for request in reserved:
            for amendment in request.purchase_order_id.jakh_subcontract_amendment_ids.filtered(lambda row: row.state == 'approved'):
                self.env['jakh.project.control.entry']._record(
                    name=amendment.name, company=request.company_id, project=request.project_id, wbs=request.wbs_id,
                    entry_type='commitment', amount=-amendment.commitment_delta,
                    source_model=amendment._name, source_id=amendment.id,
                    source_key='%s:%s:cancelled_amendment' % (amendment._name, amendment.id),
                    entry_date=fields.Date.context_today(self), partner=request.supplier_id,
                    notes=self.env._('Unreceived subcontract amendment cancelled with its requisition.'),
                )
                amendment._transition(state='cancelled')
        return result
