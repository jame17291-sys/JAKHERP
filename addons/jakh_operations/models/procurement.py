from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError
from ..services import commitment, availability

FINANCIAL = 'jakh_core.group_financial'


def require_role(env, names):
    if not any(env.user.has_group('jakh_core.' + name) for name in names):
        raise AccessError(env._('Your JAKH role cannot perform this action.'))


def form_action(record):
    """Open a native record without the pre-Odoo-20 formview helper."""
    record.ensure_one()
    record.check_access('read')
    return {'type': 'ir.actions.act_window', 'res_model': record._name,
            'res_id': record.id, 'view_mode': 'form',
            'views': [(False, 'form')], 'target': 'current'}


def lock_records(records, budget=False, touch=False):
    records.check_access('read' if budget else 'write')
    if records:
        records.flush_recordset()
        # Only authorized IDs are locked. No financial values bypass ORM access.
        records.env.cr.execute('SELECT id FROM "%s" WHERE id = ANY(%%s) ORDER BY id FOR UPDATE' % records._table, [records.ids])
        if budget or touch:
            # Updating identity to itself changes only PostgreSQL row version.
            # A concurrent REPEATABLE READ approval retries rather than seeing
            # old reservations after waiting on a lock. No budget value changes.
            records.env.cr.execute('UPDATE "%s" SET id=id WHERE id = ANY(%%s)' % records._table, [records.ids])
        records.invalidate_recordset()


def creation_parents(model, values, field, parent_model):
    """Resolve normal ORM defaults before a child insert; never grant access."""
    default = model.default_get([field]).get(field)
    return model.env[parent_model].browse([vals.get(field, default) for vals in values if vals.get(field, default)])


def lock_editable(parents, allowed_states, message):
    # A child edit must update the parent's row version. A concurrent approval
    # under REPEATABLE READ will then retry instead of reading stale children.
    lock_records(parents, touch=True)
    if parents.filtered(lambda parent: parent.state not in allowed_states):
        raise UserError(message)


class PurchaseRequest(models.Model):
    _name = 'jakh.purchase.request'
    _description = 'Project purchase requisition'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    name = fields.Char(required=True, default=lambda self: self.env._('New requisition'), tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    required_date = fields.Date(required=True, default=fields.Date.context_today)
    material_plan_reference = fields.Char(required=True, help='Reference to the approved planning demand for this requisition.')
    supplier_id = fields.Many2one('res.partner', check_company=True, groups=FINANCIAL)
    line_ids = fields.One2many('jakh.purchase.request.line', 'request_id')
    cost_ids = fields.One2many('jakh.purchase.request.cost', 'request_id', groups=FINANCIAL)
    attachment_ids = fields.Many2many('ir.attachment', 'jakh_request_attachment_rel', 'request_id', 'attachment_id', check_company=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('released', 'PO created'), ('cancelled', 'Cancelled')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    reserved_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    purchase_order_id = fields.Many2one('purchase.order', readonly=True, check_company=True, groups=FINANCIAL)

    @api.model_create_multi
    def create(self, values_list):
        for vals in values_list:
            if any(key in vals for key in ('approved_by', 'approved_at', 'reserved_amount', 'purchase_order_id')) or vals.get('state', 'draft') != 'draft':
                raise AccessError(_('Workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, vals):
        if set(vals) & {'state', 'approved_by', 'approved_at', 'reserved_amount', 'purchase_order_id'}:
            raise AccessError(_('Use the requisition workflow actions.'))
        if self.filtered(lambda rec: rec.state not in ('draft', 'submitted')):
            raise UserError(_('Approved requisitions are immutable. Cancel and create a revised request.'))
        if self.env.user.has_group('jakh_core.group_technical') and not self.env.user.has_group(FINANCIAL) and set(vals) - {'name', 'required_date', 'material_plan_reference', 'line_ids', 'attachment_ids'}:
            raise AccessError(_('Technical users can edit project quantities and supporting documents only.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda rec: rec.state != 'draft'):
            raise UserError(_('Only draft requisitions can be deleted.'))
        return super().unlink()

    @api.constrains('wbs_id', 'project_id')
    def _check_project(self):
        for rec in self:
            if rec.wbs_id.project_id != rec.project_id:
                raise ValidationError(_('The WBS must belong to the selected project.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for rec in self:
            self.env['jakh.audit.event']._append(rec.company_id, 'requisition.' + values.get('state', 'updated'), rec, {'state': rec.state})
        return result

    def _check_company_scope(self):
        if any(rec.company_id not in self.env.companies for rec in self):
            raise AccessError(_('Select the requisition company before acting.'))

    def action_submit(self):
        for rec in self:
            lock_records(rec); rec._check_company_scope()
            if rec.state != 'draft' or not rec.line_ids:
                raise UserError(_('Submit a draft request containing quantities.'))
            rec._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for rec in self:
            lock_records(rec); rec._check_company_scope(); lock_records(rec.wbs_id, budget=True)
            if rec.state == 'approved':
                continue
            if rec.state != 'submitted' or rec.wbs_id.state != 'released':
                raise UserError(_('A submitted requisition and released WBS budget are required.'))
            if not rec.supplier_id or len(rec.cost_ids) != len(rec.line_ids):
                raise UserError(_('Procurement must select a supplier and price every quantity line.'))
            if set(rec.cost_ids.line_id.ids) != set(rec.line_ids.ids):
                raise UserError(_('Every requisition line needs exactly one approved cost record.'))
            if rec.cost_ids.tax_ids.filtered('price_include'):
                raise UserError(_('This first release requires tax-exclusive approved unit prices.'))
            others = self.search([('wbs_id', '=', rec.wbs_id.id), ('state', 'in', ['approved', 'released']), ('id', '!=', rec.id)])
            try:
                amount = commitment([{'quantity': cost.line_id.quantity, 'unit_price': cost.unit_price} for cost in rec.cost_ids], rec.currency_id.decimal_places, translate=self.env._)
                effective_budget = self.env['jakh.project.control.entry']._effective_budget(rec.wbs_id)
                availability(effective_budget, sum(others.mapped('reserved_amount')), amount, rec.currency_id.decimal_places, translate=self.env._)
            except ValueError as exc:
                raise UserError(str(exc))
            rec._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now(), reserved_amount=float(amount))
            Entry = self.env['jakh.project.control.entry']
            for cost in rec.cost_ids:
                line_amount = commitment([{'quantity': cost.line_id.quantity, 'unit_price': cost.unit_price}], rec.currency_id.decimal_places, translate=self.env._)
                Entry._record(
                    name=rec.name, company=rec.company_id, project=rec.project_id, wbs=rec.wbs_id,
                    entry_type='commitment', amount=line_amount, source_model=cost._name, source_id=cost.id,
                    source_key='%s:%s:approved_commitment' % (cost._name, cost.id),
                    entry_date=rec.required_date, quantity=cost.line_id.quantity, partner=rec.supplier_id,
                    notes=cost.line_id.name,
                )
        return True

    def action_release(self):
        require_role(self.env, ['group_manager', 'group_procurement'])
        self.ensure_one(); lock_records(self); self._check_company_scope()
        if self.purchase_order_id:
            return form_action(self.purchase_order_id)
        if self.state != 'approved':
            raise UserError(_('Approve the budget reservation before creating the PO.'))
        order = self.env['purchase.order']._create_jakh({
            'partner_id': self.supplier_id.id, 'company_id': self.company_id.id,
            'currency_id': self.currency_id.id, 'origin': self.name,
            'jakh_request_id': self.id,
        })
        self.env['purchase.order.line']._create_jakh([{'order_id': order.id, 'name': cost.line_id.name, 'product_id': cost.line_id.product_id.id,
                'product_qty': cost.line_id.quantity, 'uom_id': cost.line_id.uom_id.id,
                'price_unit': cost.unit_price, 'tax_ids': [Command.set(cost.tax_ids.ids)],
                'date_planned': fields.Datetime.to_datetime(self.required_date),
                'jakh_request_line_id': cost.line_id.id} for cost in self.cost_ids])
        self._transition(state='released', purchase_order_id=order.id)
        return form_action(order)

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for rec in self:
            lock_records(rec); rec._check_company_scope(); lock_records(rec.wbs_id, budget=True)
            if rec.purchase_order_id:
                if rec.purchase_order_id.picking_ids.filtered(lambda picking: picking.state == 'done') or rec.purchase_order_id.invoice_ids.filtered(lambda move: move.state == 'posted'):
                    raise UserError(_('Received or invoiced commitments require native return/credit workflows; they cannot release this reservation.'))
                rec.purchase_order_id.button_cancel()
            for cost in rec.cost_ids:
                amount = commitment([{'quantity': cost.line_id.quantity, 'unit_price': cost.unit_price}], rec.currency_id.decimal_places, translate=self.env._)
                self.env['jakh.project.control.entry']._record(
                    name=self.env._('%s cancellation', rec.name), company=rec.company_id,
                    project=rec.project_id, wbs=rec.wbs_id, entry_type='commitment', amount=-amount,
                    source_model=rec._name, source_id=rec.id,
                    source_key='%s:%s:commitment_reversal:%s' % (rec._name, rec.id, cost.id),
                    entry_date=fields.Date.context_today(self), quantity=-cost.line_id.quantity,
                    partner=rec.supplier_id, notes=self.env._('Approved requisition cancelled before receipt or billing.'),
                    contract=cost.line_id.contract_line_id.contract_id, contract_line=cost.line_id.contract_line_id,
                    location=cost.line_id.location_id, discipline=cost.line_id.discipline_id,
                )
            rec._transition(state='cancelled', reserved_amount=0)
        return True


class RequestLine(models.Model):
    _name = 'jakh.purchase.request.line'
    _description = 'Project requested quantity'
    _check_company_auto = True
    request_id = fields.Many2one('jakh.purchase.request', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='request_id.company_id', store=True)
    name = fields.Char(required=True)
    product_id = fields.Many2one('product.product', required=True, check_company=True)
    uom_id = fields.Many2one('uom.uom', required=True)
    quantity = fields.Float(required=True, default=1, digits=(16, 6))

    @api.onchange('product_id')
    def _onchange_product(self):
        if self.product_id:
            self.uom_id = self.product_id.uom_id
            self.name = self.name or self.product_id.display_name

    def _editable(self):
        lock_editable(self.request_id, ('draft', 'submitted'), _('Approved quantities are immutable.'))

    @api.model_create_multi
    def create(self, values):
        parents = creation_parents(self, values, 'request_id', 'jakh.purchase.request')
        lock_editable(parents, ('draft', 'submitted'), _('Approved quantities are immutable.'))
        records = super().create(values); records._editable(); return records

    def write(self, values):
        self._editable()
        if 'request_id' in values:
            raise AccessError(_('Quantity lines cannot be moved between requisitions.'))
        return super().write(values)

    def unlink(self):
        self._editable(); return super().unlink()

    @api.constrains('quantity', 'uom_id', 'product_id')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0 or line.uom_id != line.product_id.uom_id:
                raise ValidationError(_('This release requires a positive quantity in the product primary unit.'))


class RequestCost(models.Model):
    _name = 'jakh.purchase.request.cost'
    _description = 'Restricted requisition pricing'
    _check_company_auto = True
    line_id = fields.Many2one('jakh.purchase.request.line', required=True, ondelete='cascade', check_company=True)
    request_id = fields.Many2one(related='line_id.request_id', store=True)
    company_id = fields.Many2one(related='request_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    unit_price = fields.Float(required=True, digits=(16, 6))
    tax_ids = fields.Many2many('account.tax', check_company=True, domain=[('type_tax_use', '=', 'purchase')])
    _one_price = models.Constraint('UNIQUE(line_id)', 'One cost record per requisition line.')
    _positive_price = models.Constraint('CHECK(unit_price >= 0)', 'Unit prices must not be negative.')

    @api.model_create_multi
    def create(self, values):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance'])
        lines = creation_parents(self, values, 'line_id', 'jakh.purchase.request.line')
        lines.check_access('read'); lines._editable()
        records = super().create(values); records.line_id._editable(); return records

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance']); self.line_id._editable()
        if 'line_id' in values:
            raise AccessError(_('Pricing cannot move between quantity lines.'))
        return super().write(values)

    def unlink(self):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_finance']); self.line_id._editable(); return super().unlink()


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'
    jakh_request_id = fields.Many2one('jakh.purchase.request', readonly=True, copy=False, check_company=True)
    _one_request = models.Constraint('UNIQUE(jakh_request_id)', 'A requisition can create only one PO.')

    @api.model_create_multi
    def create(self, values):
        if any(vals.get('jakh_request_id') for vals in values) or self.env.context.get('default_jakh_request_id'):
            raise AccessError(_('Create linked POs through the approved requisition.'))
        return super().create(values)

    def _create_jakh(self, values):
        require_role(self.env, ['group_manager', 'group_procurement'])
        return super().create(values)

    def write(self, values):
        if 'jakh_request_id' in values or (self.filtered('jakh_request_id') and set(values) & {'partner_id', 'company_id', 'currency_id'}):
            raise AccessError(_('Linked requisition identity and commercial headers are immutable.'))
        return super().write(values)

    def unlink(self):
        if self.filtered('jakh_request_id'):
            raise AccessError(_('Cancel the approved requisition instead of deleting its PO.'))
        return super().unlink()

    def button_confirm(self):
        for order in self.filtered('jakh_request_id'):
            request = order.jakh_request_id; lock_records(request)
            if request.state != 'released' or order.company_id != request.company_id or order.currency_id != request.currency_id:
                raise UserError(_('The PO must retain its approved requisition company and currency.'))
            lines = order.order_line.filtered(lambda line: not line.display_type)
            if len(lines) != len(request.line_ids) or set(lines.jakh_request_line_id.ids) != set(request.line_ids.ids):
                raise UserError(_('PO lines must match the approved requisition.'))
            for line in lines:
                source = line.jakh_request_line_id
                cost = request.cost_ids.filtered(lambda cost: cost.line_id == source)
                if line.product_id != source.product_id or line.uom_id != source.uom_id or abs(line.product_qty - source.quantity) > 0.000001 or abs(line.price_unit - cost.unit_price) > 0.000001 or line.discount or line.tax_ids != cost.tax_ids:
                    raise UserError(_('Revise and reapprove the requisition before changing PO quantities, prices or taxes.'))
        return super().button_confirm()


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'
    jakh_request_line_id = fields.Many2one('jakh.purchase.request.line', readonly=True, copy=False, check_company=True)

    @api.model_create_multi
    def create(self, values):
        if any(vals.get('jakh_request_line_id') for vals in values) or self.env.context.get('default_jakh_request_line_id'):
            raise AccessError(_('Create linked PO lines through the approved requisition.'))
        orders = creation_parents(self, values, 'order_id', 'purchase.order')
        orders.check_access('read')
        linked = orders.filtered('jakh_request_id')
        if linked and any(
                not vals.get('display_type')
                and (vals.get('order_id', self.env.context.get('default_order_id')) in linked.ids)
                for vals in values):
            raise AccessError(_('Create linked PO lines through the approved requisition.'))
        return super().create(values)

    def _create_jakh(self, values):
        require_role(self.env, ['group_manager', 'group_procurement'])
        return super().create(values)

    def write(self, values):
        if ('jakh_request_line_id' in values
                or (self.filtered(lambda line: line.jakh_request_line_id or line.order_id.jakh_request_id)
                    and set(values) & {'product_qty', 'product_id', 'uom_id', 'price_unit', 'tax_ids', 'discount', 'order_id', 'display_type'})
                or ('order_id' in values and self.env['purchase.order'].browse(values['order_id']).jakh_request_id)):
            raise AccessError(_('Approved requisition quantities and prices cannot be amended on a PO.'))
        return super().write(values)

    def unlink(self):
        if self.filtered('jakh_request_line_id'):
            raise AccessError(_('Cancel the approved requisition instead of deleting its PO lines.'))
        return super().unlink()

    @api.constrains('jakh_request_line_id', 'order_id')
    def _check_jakh_source_order(self):
        for line in self.filtered('jakh_request_line_id'):
            if line.jakh_request_line_id.request_id != line.order_id.jakh_request_id:
                raise ValidationError(_('A linked PO line must retain its approved requisition order.'))
