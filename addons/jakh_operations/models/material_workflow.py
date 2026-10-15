"""Reviewed site demand, shortage procurement and native valued stock issue."""
import math
from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .procurement import form_action, lock_records, require_role
from .construction_dimensions import check_dimensions


# Only native/private stock workflow calls can carry these Python identities.
# An RPC context, even using the same key, cannot forge the capability.
_MATERIAL_FLOW = object()
_MATERIAL_POST = object()
_SOURCE_FIELDS = {'jakh_material_request_id', 'jakh_material_return_id'}
_QUANTITY_FIELDS = ('available_quantity', 'issued_quantity', 'remaining_quantity', 'shortage_quantity')


def _factory_model(model):
    # Approved source actions own their document values. Unrelated native form
    # defaults must not acquire the private stock capability of this factory.
    return model.with_context({key: value for key, value in model.env.context.items()
                               if not (isinstance(key, str) and key.startswith('default_'))})


def _source_create_guard(env, values_list):
    if env.context.get('_jakh_material_flow') is not _MATERIAL_FLOW and (
            any(any(values.get(name) for name in _SOURCE_FIELDS) for values in values_list)
            or any(env.context.get('default_' + name) for name in _SOURCE_FIELDS)):
        raise AccessError(_('Material stock links are created by approved issue or return actions only.'))


def _material_journal(move):
    return bool(move.stock_move_ids.filtered('jakh_material_request_id')
                or move.line_ids.stock_move_id.filtered('jakh_material_request_id'))


class MaterialRequest(models.Model):
    _name = 'jakh.material.request'
    _description = 'Site material demand and issue'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.audit.mixin']
    _check_company_auto = True
    name = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, ondelete='restrict')
    contract_line_id = fields.Many2one('jakh.project.contract.line', required=True, check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', required=True, check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')
    product_id = fields.Many2one('product.product', required=True, check_company=True, ondelete='restrict')
    uom_id = fields.Many2one(related='product_id.uom_id', readonly=True)
    quantity = fields.Float(required=True, digits=(16, 6), default=1)
    required_date = fields.Date(required=True, default=fields.Date.context_today)
    source_location_id = fields.Many2one('stock.location', required=True, check_company=True, ondelete='restrict')
    consumption_location_id = fields.Many2one('stock.location', required=True, check_company=True, ondelete='restrict',
        help='Finance-configured project consumption location with a cost valuation account.')
    issue_quantity = fields.Float(digits=(16, 6), help='Quantity for the next partial issue. Zero uses remaining demand.')
    available_quantity = fields.Float(compute='_compute_quantities', digits=(16, 6))
    issued_quantity = fields.Float(compute='_compute_quantities', digits=(16, 6))
    remaining_quantity = fields.Float(compute='_compute_quantities', digits=(16, 6))
    shortage_quantity = fields.Float(compute='_compute_quantities', digits=(16, 6))
    request_id = fields.Many2one('jakh.purchase.request', readonly=True, copy=False, check_company=True)
    picking_ids = fields.One2many('stock.picking', 'jakh_material_request_id', readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'),
        ('done', 'Fully issued'), ('cancelled', 'Cancelled')], readonly=True, required=True, default='draft', tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    reason = fields.Text(required=True)
    _jakh_audit_fields = ('name', 'quantity', 'required_date', 'reason', 'issue_quantity')
    _server_fields = {'state', 'approved_by', 'request_id', 'picking_ids', 'company_id', 'uom_id'} | set(_QUANTITY_FIELDS)

    def _compute_quantities(self):
        for row in self:
            moves = row.picking_ids.move_ids.filtered(lambda move: move.state == 'done')
            row.issued_quantity = (sum(move.quantity for move in moves.filtered(lambda move: not move.jakh_material_return_id))
                                   - sum(move.quantity for move in moves.filtered('jakh_material_return_id')))
            row.remaining_quantity = max(0, row.quantity - row.issued_quantity)
            row.available_quantity = max(0, self.env['stock.quant']._get_available_quantity(
                row.product_id, row.source_location_id)) if row.product_id and row.source_location_id else 0
            row.shortage_quantity = max(0, row.remaining_quantity - row.available_quantity)

    def _refresh_quantities(self):
        # Let the ORM protect its computed-field assignment. Calling the compute
        # method directly would invoke public write() for these readonly fields.
        self.invalidate_recordset(list(_QUANTITY_FIELDS))
        return self.mapped('remaining_quantity')

    @api.model_create_multi
    def create(self, values_list):
        if (any(set(values) & self._server_fields for values in values_list)
                or any(self.env.context.get('default_' + name) for name in self._server_fields)):
            raise AccessError(_('Create draft site demands; workflow fields are server assigned.'))
        for values in values_list:
            for name in ('quantity', 'issue_quantity'):
                if name in values and not math.isfinite(float(values[name] or 0)):
                    raise ValidationError(_('Site quantities must be finite numbers.'))
        records = super().create(values_list)
        records.project_id._jakh_assert_open()
        return records

    def write(self, values):
        if set(values) & self._server_fields:
            raise AccessError(_('Use controlled site-demand actions.'))
        lock_records(self, touch=True)
        self.project_id._jakh_assert_open()
        if self.filtered(lambda row: row.state not in ('draft', 'approved')):
            raise UserError(_('Submitted and completed site demands are immutable.'))
        if self.filtered(lambda row: row.state == 'approved') and set(values) - {'issue_quantity'}:
            raise UserError(_('Only the next partial-issue quantity can change after approval.'))
        if any(name in values and not math.isfinite(float(values[name] or 0)) for name in ('quantity', 'issue_quantity')):
            raise ValidationError(_('Site quantities must be finite numbers.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Retain submitted site demands and their evidence.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'contract_line_id', 'location_id', 'discipline_id',
        'product_id', 'quantity', 'issue_quantity', 'source_location_id', 'consumption_location_id')
    def _check_scope(self):
        for row in self:
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)
            if row.quantity <= 0 or row.issue_quantity < 0 or not row.product_id.is_storable:
                raise ValidationError(_('Site material demand requires a positive quantity of a stocked product.'))
            if row.source_location_id.usage != 'internal' or row.consumption_location_id.usage != 'inventory':
                raise ValidationError(_('Issue from internal stores to a finance-configured consumption location.'))
            for place in row.source_location_id | row.consumption_location_id:
                if place.company_id != row.company_id:
                    raise ValidationError(_('Site stock locations must belong to the demand legal entity.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'material.' + row.state, row,
                {'quantity': row.quantity, 'boq': row.contract_line_id.id, 'location': row.location_id.id})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); row.project_id._jakh_assert_open()
            if row.state != 'draft' or not (row.reason or '').strip():
                raise UserError(_('Submit a draft site demand with a documented basis.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            lock_records(row); row.project_id._jakh_assert_open()
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.wbs_id.state != 'released':
                raise UserError(_('Approve submitted site demand against a released WBS.'))
            row._transition(state='approved', approved_by=self.env.user.id)
        return True

    def action_prepare_shortage(self):
        require_role(self.env, ['group_manager', 'group_procurement', 'group_project_manager'])
        self.ensure_one(); lock_records(self); self.project_id._jakh_assert_open()
        if self.request_id:
            return form_action(self.request_id)
        if self.state != 'approved':
            raise UserError(_('Approve the site demand before preparing shortage procurement.'))
        self._refresh_quantities()
        if self.shortage_quantity <= 0:
            raise UserError(_('Available stores cover this demand. No shortage requisition is needed.'))
        request = _factory_model(self.env['jakh.purchase.request']).create({'name': self.name,
            'project_id': self.project_id.id, 'wbs_id': self.wbs_id.id, 'required_date': self.required_date,
            'material_plan_reference': self.name,
            'line_ids': [Command.create({'name': self.product_id.display_name, 'product_id': self.product_id.id,
                'uom_id': self.uom_id.id, 'quantity': self.shortage_quantity,
                'contract_line_id': self.contract_line_id.id, 'location_id': self.location_id.id,
                'discipline_id': self.discipline_id.id})]})
        self._transition(request_id=request.id)
        return form_action(request)

    def action_prepare_issue(self):
        require_role(self.env, ['group_manager', 'group_procurement'])
        self.ensure_one(); lock_records(self, touch=True); self.project_id._jakh_assert_open()
        if self.state != 'approved':
            raise UserError(_('Approve the site demand before reserving a material issue.'))
        pending = self.picking_ids.filtered(lambda pick: pick.state not in ('done', 'cancel'))
        if pending:
            return form_action(pending[0])
        self._refresh_quantities()
        quantity = self.issue_quantity or self.remaining_quantity
        if quantity <= 0 or self.uom_id.compare(quantity, self.remaining_quantity) > 0:
            raise UserError(_('The issue quantity cannot exceed remaining approved site demand.'))
        if self.uom_id.compare(quantity, self.available_quantity) > 0:
            raise UserError(_('Insufficient unreserved stock. Prepare shortage procurement or a smaller partial issue.'))
        account = self.consumption_location_id.valuation_account_id
        product_accounts = self.product_id.with_company(self.company_id)._get_product_accounts()
        if (self.product_id.valuation != 'real_time' or not account
                or account.account_type not in ('expense', 'expense_direct_cost')
                or not product_accounts.get('stock_valuation') or not self.company_id.account_stock_journal_id):
            raise UserError(_('Finance must configure perpetual stock valuation, stock journal, inventory asset and site consumption expense accounts.'))
        # Use this store's configured operation, never an arbitrary company
        # warehouse. Native warehouses archive internal operations when storage
        # locations are not enabled; this action must not silently activate one.
        warehouse = self.source_location_id.warehouse_id
        picking_type = warehouse.int_type_id if warehouse else self.env['stock.picking.type']
        if (not picking_type or not picking_type.active or picking_type.code != 'internal'
                or picking_type.company_id != self.company_id):
            raise UserError(_('Configure an internal stock operation for this legal entity.'))
        picking = _factory_model(self.env['stock.picking']).with_context(_jakh_material_flow=_MATERIAL_FLOW).create({'picking_type_id': picking_type.id,
            'company_id': self.company_id.id, 'location_id': self.source_location_id.id,
            'location_dest_id': self.consumption_location_id.id, 'origin': self.name,
            'jakh_material_request_id': self.id,
            'move_ids': [Command.create({'product_id': self.product_id.id, 'origin': self.name,
                'product_uom_qty': quantity, 'uom_id': self.uom_id.id,
                'location_id': self.source_location_id.id, 'location_dest_id': self.consumption_location_id.id,
                'company_id': self.company_id.id, 'jakh_material_request_id': self.id})]})
        picking.action_confirm(); picking.action_assign()
        if picking.state != 'assigned':
            raise UserError(_('Stock could not be fully reserved. Refresh availability and retry.'))
        return form_action(picking)

    def action_prepare_return(self):
        self.ensure_one(); self.check_access('read')
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_procurement'])
        issues = self.picking_ids.move_ids.filtered(lambda move: move.state == 'done' and not move.jakh_material_return_id)
        if not issues:
            raise UserError(_('Complete an original site issue before requesting its material return.'))
        return {'type': 'ir.actions.act_window', 'name': _('Material return'), 'res_model': 'jakh.material.return',
                'view_mode': 'form', 'context': {'default_issue_move_id': issues[-1].id}}

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            lock_records(row)
            if row.picking_ids.filtered(lambda pick: pick.state == 'done') or row.request_id.state in ('approved', 'released'):
                raise UserError(_('Issued or committed demand needs a controlled stock return or requisition correction.'))
            row.picking_ids.filtered(lambda pick: pick.state != 'cancel').action_cancel()
            row._transition(state='cancelled')
        return True


class Picking(models.Model):
    _inherit = 'stock.picking'
    jakh_material_request_id = fields.Many2one('jakh.material.request', readonly=True, copy=True, check_company=True)
    jakh_material_return_id = fields.Many2one('jakh.material.return', readonly=True, copy=True, check_company=True)

    @api.model_create_multi
    def create(self, values_list):
        _source_create_guard(self.env, values_list)
        return super().create(values_list)

    def write(self, values):
        if set(values) & _SOURCE_FIELDS:
            raise AccessError(_('Material picking provenance cannot be reassigned or removed.'))
        return super().write(values)

    def _create_backorder_picking(self):
        return super(Picking, self.with_context(_jakh_material_flow=_MATERIAL_FLOW))._create_backorder_picking()


class StockMove(models.Model):
    _inherit = 'stock.move'
    jakh_material_request_id = fields.Many2one('jakh.material.request', readonly=True, copy=True, check_company=True)
    jakh_material_return_id = fields.Many2one('jakh.material.return', readonly=True, copy=True, check_company=True)

    @api.model_create_multi
    def create(self, values_list):
        _source_create_guard(self.env, values_list)
        for values in values_list:
            picking = self.env['stock.picking'].browse(values.get('picking_id') or self.env.context.get('default_picking_id')).exists()
            if picking and picking.jakh_material_request_id and (
                    values.get('jakh_material_request_id') != picking.jakh_material_request_id.id
                    or (values.get('jakh_material_return_id') or False) != (picking.jakh_material_return_id.id or False)):
                raise AccessError(_('A material move must stay in a picking with matching source provenance.'))
            original = self.env['stock.move'].browse(values.get('origin_returned_move_id') or self.env.context.get('default_origin_returned_move_id')).exists()
            if original and original.jakh_material_request_id and (
                    self.env.context.get('_jakh_material_flow') is not _MATERIAL_FLOW
                    or not values.get('jakh_material_return_id')
                    or values.get('jakh_material_request_id') != original.jakh_material_request_id.id):
                raise AccessError(_('Return a JAKH site issue through its reviewed material-return document.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & _SOURCE_FIELDS:
            raise AccessError(_('Material move provenance cannot be reassigned or removed.'))
        controlled = self.filtered('jakh_material_request_id')
        if controlled and values.get('state') == 'done' and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Complete material moves through native stock validation, not a direct state edit.'))
        if values.get('origin_returned_move_id'):
            original = self.env['stock.move'].browse(values['origin_returned_move_id']).exists()
            if original.jakh_material_request_id and (not controlled or not self.jakh_material_return_id):
                raise AccessError(_('Return a JAKH site issue through its reviewed material-return document.'))
        if 'picking_id' in values:
            picking = self.env['stock.picking'].browse(values['picking_id']).exists()
            picking.check_access('read')
            if (controlled or picking.jakh_material_request_id) and any(picking.jakh_material_request_id != row.jakh_material_request_id
                   or picking.jakh_material_return_id != row.jakh_material_return_id for row in self):
                raise AccessError(_('A material move must stay in a picking with matching source provenance.'))
        protected = controlled.filtered(lambda row: row.state == 'done')
        sensitive = {'product_id', 'uom_id', 'product_uom_qty', 'quantity', 'location_id', 'location_dest_id',
                     'company_id', 'price_unit', 'value', 'remaining_value', 'remaining_qty', 'account_move_id',
                     'state', 'date', 'picking_id', 'origin_returned_move_id'}
        if protected and set(values) & sensitive and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            # A late, correctly matched AP bill can replay native valuation and
            # assign the same company-currency amount. Skip that write entirely:
            # no stock value, source, timestamp, journal or actual fact changes.
            from .material_matching import _native_material_value_refresh
            if (set(values) == {'value'} and _native_material_value_refresh(self.env)
                    and all(not move.company_currency_id.compare_amounts(values['value'], move.value) for move in self)):
                return True
            raise AccessError(_('Posted material issues require an approved return, not edits to their stock or accounting evidence.'))
        if controlled and 'account_move_id' in values and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Material valuation journals are assigned by native stock posting only.'))
        return super().write(values)

    def unlink(self):
        if self.filtered('jakh_material_request_id'):
            raise AccessError(_('Retain material stock evidence; cancel unissued work or use an approved return.'))
        return super().unlink()

    def _create_backorder(self):
        return super(StockMove, self.with_context(_jakh_material_flow=_MATERIAL_FLOW))._create_backorder()

    def _create_account_move(self):
        return super(StockMove, self.with_context(_jakh_material_posting=_MATERIAL_POST))._create_account_move()

    def _get_account_move_line_vals(self):
        values = super()._get_account_move_line_vals()
        if self.jakh_material_request_id:
            for line in values:
                line['stock_move_id'] = self.id
        return values

    def _get_analytic_distribution(self):
        self.ensure_one()
        request = self.jakh_material_request_id
        account = request.project_id.account_id
        return {str(account.id): 100} if account else super()._get_analytic_distribution()

    def _action_done(self, cancel_backorder=False):
        pending = self.filtered(lambda move: move.jakh_material_request_id and move.state not in ('done', 'cancel'))
        issues = pending.filtered(lambda move: not move.jakh_material_return_id)
        returns = pending.filtered('jakh_material_return_id')
        for request, moves in issues.grouped('jakh_material_request_id').items():
            lock_records(request, touch=True); request._refresh_quantities()
            if request.uom_id.compare(sum(moves.mapped('quantity')), request.remaining_quantity) > 0:
                raise UserError(_('Combined stock issues cannot exceed remaining approved site demand.'))
        for document, moves in returns.grouped('jakh_material_return_id').items():
            lock_records(document, touch=True); lock_records(document.issue_move_id, budget=True, touch=True)
            document.material_request_id._refresh_quantities()
            if document.state != 'approved' or document.uom_id.compare(sum(moves.mapped('quantity')), document._remaining_return_quantity()) > 0:
                raise UserError(_('Combined stock returns cannot exceed the approved unreturned issue quantity.'))
        for move in pending:
            row = move.jakh_material_request_id
            lock_records(row, touch=True); row.project_id._jakh_assert_open()
            row._refresh_quantities()
            returned = move.jakh_material_return_id
            source = row.consumption_location_id if returned else row.source_location_id
            destination = row.source_location_id if returned else row.consumption_location_id
            if (move.product_id != row.product_id or move.uom_id != row.uom_id or move.company_id != row.company_id
                    or move.picking_id.jakh_material_request_id != row or move.picking_id.jakh_material_return_id != returned
                    or move.location_id != source or move.location_dest_id != destination
                    or not returned and (row.state != 'approved' or row.uom_id.compare(move.quantity, row.remaining_quantity) > 0)
                    or returned and (returned.state != 'approved' or move.origin_returned_move_id != returned.issue_move_id)):
                raise UserError(_('The stock issue must match its approved site demand and remaining quantity.'))
        result = super(StockMove, self.with_context(_jakh_material_posting=_MATERIAL_POST))._action_done(cancel_backorder=cancel_backorder)
        for move in result.filtered('jakh_material_request_id'):
            row = move.jakh_material_request_id
            journal = move.account_move_id
            expense = journal.line_ids.filtered(lambda line: line.stock_move_id == move and line.account_id == row.consumption_location_id.valuation_account_id)
            if journal.state != 'posted' or len(expense) != 1:
                raise UserError(_('A site issue must produce one posted, balanced native stock-cost journal.'))
            returned = move.jakh_material_return_id
            if expense.balance < 0 and not returned or expense.balance > 0 and returned:
                raise UserError(_('The native material valuation must match the issue or approved return direction.'))
            entry = self.env['jakh.project.control.entry']._record(name=move.reference or row.name,
                company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='actual_cost', amount=expense.balance, quantity=-move.quantity if returned else move.quantity,
                source_model=move._name, source_id=move.id, source_key='stock.move:%s:%s' % (move.id, 'site_return' if returned else 'site_issue'),
                entry_date=journal.date, contract=row.contract_line_id.contract_id, contract_line=row.contract_line_id,
                location=row.location_id, discipline=row.discipline_id)
            entry._attach_posting(expense)
            row._refresh_quantities()
            if returned:
                if returned.uom_id.is_zero(returned._remaining_return_quantity()):
                    returned._transition(state='done')
                if row.state == 'done':
                    row._transition(state='approved')
            if row.uom_id.is_zero(row.remaining_quantity):
                row._transition(state='done')
        return result


class MaterialStockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            for values in values_list:
                move = self.env['stock.move'].browse(values.get('move_id') or self.env.context.get('default_move_id')).exists()
                if move and move.jakh_material_request_id and (
                        move.state == 'done' or 'state' in values or self.env.context.get('default_state')):
                    raise AccessError(_('Material operation-line states derive from native stock validation.'))
        return super().create(values_list)

    def write(self, values):
        controlled = self.filtered('move_id.jakh_material_request_id')
        if controlled and 'state' in values and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Material operation-line states derive from native stock validation.'))
        sensitive = {'quantity', 'product_id', 'uom_id', 'location_id', 'location_dest_id', 'company_id',
                     'picking_id', 'move_id', 'lot_id', 'lot_name', 'package_id', 'result_package_id', 'owner_id', 'state'}
        if (controlled.filtered(lambda line: line.move_id.state == 'done') and set(values) & sensitive
                and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST):
            raise AccessError(_('Completed material operation lines are immutable; use an approved return.'))
        if 'move_id' in values:
            target = self.env['stock.move'].browse(values['move_id']).exists()
            target.check_access('read')
            if (controlled or target.jakh_material_request_id) and any(
                    line.move_id.jakh_material_request_id != target.jakh_material_request_id
                    or line.move_id.jakh_material_return_id != target.jakh_material_return_id for line in self):
                raise AccessError(_('A material operation line must retain matching issue or return provenance.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda line: line.move_id.jakh_material_request_id and line.move_id.state == 'done'):
            raise AccessError(_('Retain completed material operation lines and their source evidence.'))
        return super().unlink()


class MaterialReturn(models.Model):
    _name = 'jakh.material.return'
    _description = 'Reviewed material return and job-cost correction'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.audit.mixin']
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    issue_move_id = fields.Many2one('stock.move', required=True, check_company=True, ondelete='restrict')
    material_request_id = fields.Many2one(related='issue_move_id.jakh_material_request_id', store=True)
    project_id = fields.Many2one(related='material_request_id.project_id', store=True)
    company_id = fields.Many2one(related='material_request_id.company_id', store=True)
    contract_line_id = fields.Many2one(related='material_request_id.contract_line_id', readonly=True)
    location_id = fields.Many2one(related='material_request_id.location_id', readonly=True)
    wbs_id = fields.Many2one(related='material_request_id.wbs_id', readonly=True)
    product_id = fields.Many2one(related='issue_move_id.product_id', readonly=True)
    uom_id = fields.Many2one(related='issue_move_id.uom_id', readonly=True)
    quantity = fields.Float(required=True, digits=(16, 6), default=1)
    reason = fields.Text(required=True, tracking=True)
    evidence_attachment_ids = fields.Many2many('ir.attachment', 'jakh_material_return_evidence_rel',
        'material_return_id', 'attachment_id', check_company=True, string='Return evidence')
    picking_ids = fields.One2many('stock.picking', 'jakh_material_return_id', readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'),
                              ('done', 'Returned'), ('rejected', 'Rejected')], default='draft', readonly=True, required=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _server_fields = {'state', 'approved_by', 'approved_at', 'picking_ids', 'material_request_id',
                      'project_id', 'company_id', 'contract_line_id', 'location_id', 'wbs_id', 'product_id', 'uom_id'}
    _jakh_audit_fields = ('name', 'quantity', 'reason', 'evidence_attachment_ids')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_procurement'])
        if any(self.env.context.get('default_' + name) for name in self._server_fields):
            raise AccessError(_('Return provenance and approval are assigned by the material workflow only.'))
        for values in values_list:
            if set(values) & self._server_fields:
                raise AccessError(_('Return provenance and approval are assigned by the material workflow only.'))
            if 'quantity' in values and not math.isfinite(float(values['quantity'] or 0)):
                raise ValidationError(_('A material return needs a finite positive quantity.'))
        records = super().create(values_list)
        records.project_id._jakh_assert_open()
        return records

    def write(self, values):
        if set(values) & (self._server_fields | {'issue_move_id'}):
            raise AccessError(_('A material return cannot move or rewrite its source and approval evidence.'))
        lock_records(self, touch=True)
        self.project_id._jakh_assert_open()
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted material returns are immutable.'))
        if 'quantity' in values and not math.isfinite(float(values['quantity'] or 0)):
            raise ValidationError(_('A material return needs a finite positive quantity.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Retain submitted material-return evidence.'))
        return super().unlink()

    @api.constrains('issue_move_id', 'quantity')
    def _check_source(self):
        for row in self:
            row.issue_move_id.check_access('read')
            row.material_request_id.check_access('read')
            if (not row.material_request_id or row.issue_move_id.state != 'done' or row.issue_move_id.jakh_material_return_id
                    or row.quantity <= 0 or not math.isfinite(row.quantity)):
                raise ValidationError(_('A material return must reference a completed original site issue with a finite positive quantity.'))

    def _remaining_return_quantity(self):
        self.ensure_one()
        returned = sum(self.picking_ids.move_ids.filtered(lambda move: move.state == 'done').mapped('quantity'))
        return max(0, self.quantity - returned)

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'material_return.' + row.state, row,
                {'issue_move_id': row.issue_move_id.id, 'quantity': row.quantity, 'reason': row.reason})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); row.project_id._jakh_assert_open()
            if row.state != 'draft' or not (row.reason or '').strip() or not row.evidence_attachment_ids:
                raise UserError(_('Submit a draft material return with a reason and supporting evidence.'))
            row.evidence_attachment_ids.check_access('read')
            if row.evidence_attachment_ids.filtered(lambda attachment: attachment.res_model != row._name or attachment.res_id != row.id):
                raise UserError(_('Return evidence must be attached to this return document.'))
            if row.evidence_attachment_ids.filtered('public'):
                raise UserError(_('Material-return evidence must remain private.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            lock_records(row); row.project_id._jakh_assert_open()
            lock_records(row.issue_move_id, budget=True, touch=True)
            if row.state == 'approved':
                continue
            if row.state != 'submitted':
                raise UserError(_('Approve a submitted material return.'))
            original = row.issue_move_id
            returned = self.env['stock.move'].search([
                ('origin_returned_move_id', '=', original.id), ('state', '=', 'done'),
            ])
            pending = self.search([('issue_move_id', '=', original.id), ('state', '=', 'approved'), ('id', '!=', row.id)])
            available = original.quantity - sum(returned.mapped('quantity')) - sum(document._remaining_return_quantity() for document in pending)
            if row.uom_id.compare(row.quantity, available) > 0:
                raise UserError(_('Return approvals cannot exceed the original issue less completed and pending approved returns.'))
            row._transition(state='approved', approved_by=self.env.uid, approved_at=fields.Datetime.now())
        return True

    def action_prepare_picking(self):
        require_role(self.env, ['group_manager', 'group_procurement'])
        self.ensure_one(); lock_records(self, touch=True); self.project_id._jakh_assert_open()
        pending = self.picking_ids.filtered(lambda pick: pick.state not in ('done', 'cancel'))
        if pending:
            return form_action(pending[:1])
        if self.state == 'done' and self.picking_ids:
            return form_action(self.picking_ids.filtered(lambda pick: pick.state == 'done')[-1:])
        if self.state != 'approved':
            raise UserError(_('Approve the material return before preparing its native stock transfer.'))
        quantity = self._remaining_return_quantity()
        if self.uom_id.compare(quantity, 0) <= 0:
            raise UserError(_('Combined stock returns cannot exceed the approved unreturned issue quantity.'))
        previous = self.picking_ids.ids
        row = self.material_request_id
        picking = _factory_model(self.env['stock.picking']).with_context(_jakh_material_flow=_MATERIAL_FLOW).create({
            'picking_type_id': self.issue_move_id.picking_id.picking_type_id.id, 'company_id': self.company_id.id,
            'location_id': row.consumption_location_id.id, 'location_dest_id': row.source_location_id.id,
            'origin': self.name, 'jakh_material_request_id': row.id, 'jakh_material_return_id': self.id,
            'move_ids': [Command.create({'origin': self.name, 'product_id': self.product_id.id,
                'product_uom_qty': quantity, 'uom_id': self.uom_id.id, 'company_id': self.company_id.id,
                'location_id': row.consumption_location_id.id, 'location_dest_id': row.source_location_id.id,
                'origin_returned_move_id': self.issue_move_id.id,
                'jakh_material_request_id': row.id, 'jakh_material_return_id': self.id})],
        })
        picking.action_confirm(); picking.action_assign()
        self.env['jakh.audit.event']._append(self.company_id, 'material_return.transfer_prepared', self,
            {'picking_id': picking.id, 'previous_picking_ids': previous,
             'quantity': quantity, 'issue_move_id': self.issue_move_id.id})
        return form_action(picking)

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted material returns can be rejected.'))
            row._transition(state='rejected')
        return True


class MaterialValuationJournal(models.Model):
    _inherit = 'account.move'

    def write(self, values):
        sealed = self.filtered(lambda move: move.state == 'posted' and _material_journal(move))
        if sealed and set(values) & {'state', 'journal_id', 'company_id', 'currency_id', 'date', 'line_ids', 'stock_move_ids',
                                     'move_type', 'partner_id'} and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Posted material valuation requires an approved stock return, not journal edits.'))
        if 'stock_move_ids' in values and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Stock valuation source links are assigned by native stock posting.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(_material_journal):
            raise AccessError(_('Material valuation evidence cannot be deleted.'))
        return super().unlink()

    def button_draft(self):
        if self.filtered(_material_journal):
            raise UserError(_('Correct material cost through an approved stock return.'))
        return super().button_draft()

    def button_cancel(self):
        if self.filtered(_material_journal):
            raise UserError(_('Correct material cost through an approved stock return.'))
        return super().button_cancel()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        if self.filtered(_material_journal):
            raise UserError(_('Correct material cost through an approved stock return.'))
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    def _post(self, soft=True):
        if self.filtered(_material_journal) and self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            raise AccessError(_('Material valuation is posted by the approved native stock workflow only.'))
        return super()._post(soft=soft)


class MaterialValuationJournalLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            for values in values_list:
                source = self.env['stock.move'].browse(values.get('stock_move_id') or self.env.context.get('default_stock_move_id')).exists()
                if source and source.jakh_material_request_id:
                    raise AccessError(_('Material valuation journal items come from approved native stock posting only.'))
                move = self.env['account.move'].browse(values.get('move_id') or self.env.context.get('default_move_id')).exists()
                if move and move.state == 'posted' and _material_journal(move):
                    raise AccessError(_('Posted material valuation cannot accept additional journal items.'))
        return super().create(values_list)

    def write(self, values):
        if self.env.context.get('_jakh_material_posting') is not _MATERIAL_POST:
            if 'stock_move_id' in values and (self.stock_move_id.filtered('jakh_material_request_id')
                    or self.env['stock.move'].browse(values['stock_move_id']).exists().filtered('jakh_material_request_id')):
                raise AccessError(_('Material valuation source links cannot be reassigned or removed.'))
            destination = self.env['account.move'].browse(values.get('move_id')).exists()
            if destination and destination.state == 'posted' and _material_journal(destination):
                raise AccessError(_('Posted material valuation cannot accept additional journal items.'))
            sealed = self.filtered(lambda line: line.move_id.state == 'posted' and _material_journal(line.move_id))
            if sealed and set(values) & {'move_id', 'account_id', 'partner_id', 'company_id', 'currency_id', 'stock_move_id',
                    'product_id', 'product_uom_id', 'quantity', 'price_unit', 'discount', 'debit', 'credit', 'balance',
                    'amount_currency', 'tax_ids', 'tax_tag_ids', 'analytic_distribution', 'date'}:
                raise AccessError(_('Posted material-cost journal items are immutable; use an approved return.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda line: _material_journal(line.move_id)):
            raise AccessError(_('Retain material valuation journal items and their source evidence.'))
        return super().unlink()


class MaterialReturnAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if values.get('res_model') == 'jakh.material.return' and values.get('res_id'):
                document = self.env['jakh.material.return'].browse(values['res_id']).exists()
                document.check_access('read')
                if values.get('public'):
                    raise AccessError(_('Material-return evidence must remain private.'))
                if document and document.state != 'draft':
                    raise UserError(_('Return evidence is locked after submission.'))
        return super().create(values_list)

    def write(self, values):
        for attachment in self:
            targets = [(attachment.res_model, attachment.res_id),
                       (values.get('res_model', attachment.res_model), values.get('res_id', attachment.res_id))]
            for model, identifier in targets:
                if model == 'jakh.material.return' and identifier:
                    document = self.env[model].browse(identifier).exists()
                    document.check_access('read')
                    if document and document.state != 'draft':
                        raise UserError(_('Return evidence is locked after submission.'))
                    if values.get('public'):
                        raise AccessError(_('Material-return evidence must remain private.'))
        return super().write(values)

    def unlink(self):
        for attachment in self.filtered(lambda row: row.res_model == 'jakh.material.return' and row.res_id):
            document = self.env[attachment.res_model].browse(attachment.res_id).exists()
            document.check_access('read')
            if document and document.state != 'draft':
                raise UserError(_('Return evidence is locked after submission.'))
        return super().unlink()
