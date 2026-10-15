"""Controlled construction execution feeds, service entries and release gates.

These models deliberately keep specialist systems as sources of evidence.  A
P6 progress message, a Jisr-approved labour feed or a plant allocation does
not gain accounting or project-control authority until an authorized JAKH
user accepts it.  Acceptance adds a single idempotent project-control fact;
the inbound evidence then remains available for audit and replay.
"""
import json
import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role


def _finite_input(values, names):
    """Reject unsupported numbers before native precision conversion occurs."""
    try:
        return all(math.isfinite(float(values[name] or 0)) for name in names if name in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _has_server_defaults(env, names):
    """RPC defaults are input too; readonly is not a server-side safeguard."""
    return any('default_' + name in env.context for name in names)


def _require_project_scope(record):
    """Check the reusable customer/company/project/WBS boundary."""
    if record.company_id not in record.env.companies:
        raise AccessError(_('Select the matching legal entity before acting.'))
    if record.wbs_id.project_id != record.project_id or record.wbs_id.company_id != record.company_id:
        raise ValidationError(_('The WBS must belong to the selected project and legal entity.'))
    record.project_id._jakh_assert_open()


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    # This is intentionally not exposed as a normal editable PO field.  The
    # first submitted SES turns on matching for that subcontract line.
    jakh_requires_ses = fields.Boolean(readonly=True, copy=False, string='Service entry matching required')


class SubcontractServiceEntry(models.Model):
    _name = 'jakh.subcontract.service.entry'
    _description = 'Controlled subcontract service entry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'service_date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New service entry'), tracking=True)
    purchase_line_id = fields.Many2one('purchase.order.line', required=True, check_company=True, tracking=True)
    purchase_order_id = fields.Many2one(related='purchase_line_id.order_id', store=True, readonly=True)
    project_id = fields.Many2one(related='purchase_line_id.jakh_request_line_id.request_id.project_id', store=True, readonly=True)
    wbs_id = fields.Many2one(related='purchase_line_id.jakh_request_line_id.request_id.wbs_id', store=True, readonly=True)
    company_id = fields.Many2one(related='purchase_line_id.jakh_request_line_id.request_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    partner_id = fields.Many2one(related='purchase_line_id.order_id.partner_id', store=True, readonly=True)
    quantity = fields.Float(required=True, default=1, digits=(16, 6), tracking=True)
    uom_id = fields.Many2one(related='purchase_line_id.uom_id', readonly=True)
    service_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    notes = fields.Text(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected'),
    ], default='draft', required=True, readonly=True, tracking=True)
    accepted_by = fields.Many2one('res.users', readonly=True)
    accepted_at = fields.Datetime(readonly=True)
    accepted_amount = fields.Monetary(readonly=True, groups=FINANCIAL)

    _server_fields = {'state', 'accepted_by', 'accepted_at', 'accepted_amount'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_procurement', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list)):
            raise AccessError(_('Service-entry workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_procurement', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled service-entry actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted service entries are immutable. Create a correcting entry instead.'))
        if 'purchase_line_id' in values:
            raise AccessError(_('A service entry cannot be moved to another subcontract order line.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft service entries can be deleted.'))
        return super().unlink()

    @api.constrains('purchase_line_id', 'quantity')
    def _check_subcontract_scope(self):
        for row in self:
            request_line = row.purchase_line_id.jakh_request_line_id
            if not request_line:
                raise ValidationError(_('A subcontract service entry must reference a JAKH project purchase-order line.'))
            _require_project_scope(row)
            if row.purchase_line_id.product_id.type != 'service':
                raise ValidationError(_('A subcontract service entry can be used only for a service product.'))
            if row.quantity <= 0:
                raise ValidationError(_('A subcontract service entry requires a positive accepted quantity.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'subcontract_ses.' + row.state, row, {
                'purchase_line': row.purchase_line_id.id, 'quantity': row.quantity,
            })
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state != 'draft' or not row.notes.strip():
                raise UserError(_('Submit a draft service entry with documented work performed.'))
            if row.purchase_order_id.state not in ('purchase', 'done'):
                raise UserError(_('Confirm the subcontract purchase order before submitting a service entry.'))
            # Server-only marker that makes later supplier invoice matching mandatory.
            super(PurchaseOrderLine, row.purchase_line_id).write({'jakh_requires_ses': True})
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); lock_records(row.purchase_line_id)
            _require_project_scope(row)
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.wbs_id.state != 'released':
                raise UserError(_('Approve a submitted service entry against a released WBS.'))
            order = row.purchase_order_id
            if order.currency_id != row.company_id.currency_id:
                raise UserError(_('Subcontract service matching currently requires a company-currency purchase order.'))
            prior = self.search([
                ('purchase_line_id', '=', row.purchase_line_id.id), ('state', '=', 'approved'), ('id', '!=', row.id),
            ])
            accepted_quantity = sum(prior.mapped('quantity')) + row.quantity
            if accepted_quantity > row.purchase_line_id.product_qty + 1e-6:
                raise UserError(_('Accepted service quantities cannot exceed the confirmed subcontract quantity.'))
            amount = Decimal(str(row.quantity)) * Decimal(str(row.purchase_line_id.price_unit))
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='receipt', amount=amount, source_model=row._name, source_id=row.id,
                source_key='%s:%s:accepted_service' % (row._name, row.id), entry_date=row.service_date,
                quantity=row.quantity, partner=row.partner_id,
                notes=_('Accepted subcontract service entry'),
            )
            row._transition(state='approved', accepted_by=self.env.user.id,
                            accepted_at=fields.Datetime.now(), accepted_amount=float(amount))
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted service entries can be rejected.'))
            row._transition(state='rejected')
        return True


class AccountMoveServiceEntryMatch(models.Model):
    _inherit = 'account.move'

    def _check_jakh_service_entry_match(self):
        """Block an SES-controlled subcontract bill above accepted quantity.

        Vendor invoices remain native payable transactions; this only verifies
        that their purchasing quantity is backed by approved service evidence.
        """
        lines_by_purchase = defaultdict(Decimal)
        for move in self.filtered(lambda item: item.move_type in ('in_invoice', 'in_refund')):
            sign = Decimal(-1) if move.move_type == 'in_refund' else Decimal(1)
            for line in move.invoice_line_ids.filtered(lambda item: item.purchase_line_id.jakh_requires_ses):
                lines_by_purchase[line.purchase_line_id.id] += sign * Decimal(str(line.quantity))
        for purchase_line_id, new_quantity in lines_by_purchase.items():
            purchase_line = self.env['purchase.order.line'].browse(purchase_line_id)
            service_entries = self.env['jakh.subcontract.service.entry'].search([
                ('purchase_line_id', '=', purchase_line.id), ('state', '=', 'approved'),
            ])
            accepted = sum(Decimal(str(value)) for value in service_entries.mapped('quantity'))
            historical = self.env['account.move.line'].search([
                ('purchase_line_id', '=', purchase_line.id),
                ('move_id.state', '=', 'posted'), ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
            ])
            previously_billed = sum(
                (Decimal(-1) if line.move_id.move_type == 'in_refund' else Decimal(1)) * Decimal(str(line.quantity))
                for line in historical
            )
            if previously_billed + new_quantity > accepted + Decimal('0.000001'):
                raise UserError(_('A subcontract supplier invoice cannot exceed approved service-entry quantities.'))

    def _post(self, soft=True):
        self._check_jakh_service_entry_match()
        return super()._post(soft=soft)


class ProjectActualFeed(models.Model):
    _name = 'jakh.project.actual.feed'
    _description = 'Verified labour or plant actual-cost feed'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'work_date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New verified actual-cost feed'), tracking=True)
    source_system = fields.Selection([
        ('jisr', 'Jisr approved labour feed'), ('plant', 'Plant/equipment feed'), ('verified', 'Verified manual feed'),
    ], required=True, default='jisr', tracking=True)
    actual_type = fields.Selection([('labour', 'Labour'), ('plant', 'Plant / equipment')], required=True, default='labour', tracking=True)
    external_id = fields.Char(required=True, copy=False, index=True, tracking=True)
    source_reference = fields.Char(required=True, copy=False, tracking=True)
    payload_digest = fields.Char(copy=False, readonly=True, help='Checksum or immutable source payload reference; raw payroll data is not stored here.')
    project_id = fields.Many2one('project.project', required=True, check_company=True, tracking=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, tracking=True)
    work_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    resource_reference = fields.Char(required=True, tracking=True)
    quantity = fields.Float(required=True, default=1, digits=(16, 6), tracking=True)
    unit_label = fields.Char(required=True, default='hours', tracking=True)
    amount = fields.Monetary(required=True, tracking=True, groups=FINANCIAL)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Validated'), ('accepted', 'Accepted'), ('rejected', 'Rejected'),
    ], default='draft', required=True, readonly=True, tracking=True)
    accepted_by = fields.Many2one('res.users', readonly=True)
    accepted_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'accepted_by', 'accepted_at'}
    _source_once = models.Constraint('UNIQUE(company_id,source_system,external_id)', 'An external labour or plant cost feed can be accepted only once.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list)):
            raise AccessError(_('Actual-cost feed workflow values are assigned by server actions.'))
        if any(not _finite_input(values, ('quantity', 'amount')) for values in values_list):
            raise ValidationError(_('An actual-cost feed needs a unique source ID, positive quantity and non-negative company-currency cost.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled actual-cost feed actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Validated actual-cost feeds are immutable. Submit a correcting feed instead.'))
        if set(values) & {'project_id', 'company_id', 'wbs_id', 'source_system', 'external_id'}:
            raise AccessError(_('Create a new source feed instead of moving its project scope or identity.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft actual-cost feeds can be deleted.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'quantity', 'amount', 'source_system', 'actual_type', 'external_id')
    def _check_feed(self):
        for row in self:
            _require_project_scope(row)
            if not all(math.isfinite(value) for value in (row.quantity, row.amount)) or row.quantity <= 0 or row.amount < 0 or not row.external_id.strip():
                raise ValidationError(_('An actual-cost feed needs a unique source ID, positive quantity and non-negative company-currency cost.'))
            if row.actual_type == 'labour' and row.source_system == 'plant':
                raise ValidationError(_('A plant source cannot be recorded as labour.'))
            if row.actual_type == 'plant' and row.source_system == 'jisr':
                raise ValidationError(_('A Jisr labour source cannot be recorded as plant.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'actual_feed.' + row.state, row, {
                'source_system': row.source_system, 'external_id': row.external_id, 'amount': row.amount,
            })
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state != 'draft' or row.wbs_id.state != 'released':
                raise UserError(_('Validate a draft actual-cost feed against a released WBS.'))
            row._transition(state='submitted')
        return True

    def action_accept(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state == 'accepted':
                continue
            if row.state != 'submitted':
                raise UserError(_('Accept a validated labour or plant actual-cost feed.'))
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='actual_cost', amount=row.amount, source_model=row._name, source_id=row.id,
                source_key='%s:%s:actual_cost' % (row._name, row.id), entry_date=row.work_date,
                quantity=row.quantity, provenance='integration' if row.source_system != 'verified' else 'native',
                notes=_('%s · %s') % (dict(row._fields['source_system'].selection).get(row.source_system), row.resource_reference),
            )
            row._transition(state='accepted', accepted_by=self.env.user.id, accepted_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only validated actual-cost feeds can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectScheduleProgress(models.Model):
    _name = 'jakh.project.schedule.progress'
    _description = 'Accepted project schedule progress'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'as_of_date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New schedule progress'), tracking=True)
    source_system = fields.Selection([('p6', 'P6 schedule master'), ('verified', 'Verified manual update')], required=True, default='p6', tracking=True)
    external_id = fields.Char(required=True, copy=False, index=True, tracking=True)
    source_reference = fields.Char(required=True, copy=False, tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, tracking=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, tracking=True)
    as_of_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    planned_value = fields.Monetary(required=True, tracking=True, groups=FINANCIAL)
    earned_value = fields.Monetary(required=True, tracking=True, groups=FINANCIAL)
    progress_percent = fields.Float(required=True, default=0, digits=(16, 4), tracking=True)
    forecast_finish = fields.Date(tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Validated'), ('accepted', 'Accepted'), ('rejected', 'Rejected'),
    ], default='draft', required=True, readonly=True, tracking=True)
    accepted_by = fields.Many2one('res.users', readonly=True)
    accepted_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'accepted_by', 'accepted_at'}
    _source_once = models.Constraint('UNIQUE(company_id,source_system,external_id)', 'A schedule-progress source can be accepted only once.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list)):
            raise AccessError(_('Schedule-progress workflow values are assigned by server actions.'))
        if any(not _finite_input(values, ('planned_value', 'earned_value', 'progress_percent')) for values in values_list):
            raise ValidationError(_('Schedule progress needs a unique source ID and non-negative planned and earned values.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled schedule-progress actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Validated schedule progress is immutable. Submit a correcting update instead.'))
        if set(values) & {'project_id', 'company_id', 'wbs_id', 'source_system', 'external_id'}:
            raise AccessError(_('Create a new schedule update instead of moving its project scope or source identity.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft schedule progress can be deleted.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'planned_value', 'earned_value', 'progress_percent', 'external_id')
    def _check_progress(self):
        for row in self:
            _require_project_scope(row)
            if not all(math.isfinite(value) for value in (row.planned_value, row.earned_value, row.progress_percent)) or not row.external_id.strip() or row.planned_value < 0 or row.earned_value < 0:
                raise ValidationError(_('Schedule progress needs a unique source ID and non-negative planned and earned values.'))
            if row.progress_percent < 0 or row.progress_percent > 100:
                raise ValidationError(_('Schedule progress must be between 0 and 100 percent.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'schedule_progress.' + row.state, row, {
                'source_system': row.source_system, 'external_id': row.external_id,
                'planned_value': row.planned_value, 'earned_value': row.earned_value,
            })
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state != 'draft' or row.wbs_id.state != 'released':
                raise UserError(_('Validate draft schedule progress against a released WBS.'))
            row._transition(state='submitted')
        return True

    def action_accept(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state == 'accepted':
                continue
            if row.state != 'submitted':
                raise UserError(_('Accept a validated P6 or verified schedule-progress update.'))
            provenance = 'integration' if row.source_system == 'p6' else 'native'
            for entry_type, amount, suffix in (
                ('planned_value', row.planned_value, 'planned_value'),
                ('earned_value', row.earned_value, 'earned_value'),
            ):
                Entry._record(
                    name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                    entry_type=entry_type, amount=amount, source_model=row._name, source_id=row.id,
                    source_key='%s:%s:%s' % (row._name, row.id, suffix), entry_date=row.as_of_date,
                    provenance=provenance,
                    notes=_('Schedule progress from %s; forecast finish %s') % (row.source_system, row.forecast_finish or ''),
                )
            row._transition(state='accepted', accepted_by=self.env.user.id, accepted_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only validated schedule progress can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectCashForecast(models.Model):
    _name = 'jakh.project.cash.forecast'
    _description = 'Controlled project cash forecast'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'expected_date, id'

    name = fields.Char(required=True, default=lambda self: self.env._('New project cash forecast'), tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, tracking=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, tracking=True)
    cash_direction = fields.Selection([('in', 'Cash in'), ('out', 'Cash out')], required=True, default='out', tracking=True)
    expected_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    amount = fields.Monetary(required=True, tracking=True, groups=FINANCIAL)
    basis = fields.Text(required=True, tracking=True)
    source_reference = fields.Char(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected'),
    ], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'approved_by', 'approved_at'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list)):
            raise AccessError(_('Cash-forecast workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled cash-forecast actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted cash forecasts are immutable. Create a revised forecast instead.'))
        if set(values) & {'project_id', 'company_id', 'wbs_id'}:
            raise AccessError(_('Create a new cash forecast instead of moving it to another project scope.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft cash forecasts can be deleted.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'amount', 'basis', 'source_reference')
    def _check_cash_forecast(self):
        for row in self:
            _require_project_scope(row)
            if row.amount < 0 or not row.basis.strip() or not row.source_reference.strip():
                raise ValidationError(_('A cash forecast requires a non-negative amount, documented basis and source reference.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'cash_forecast.' + row.state, row, {
                'direction': row.cash_direction, 'expected_date': str(row.expected_date), 'amount': row.amount,
            })
        return result

    def action_submit(self):
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state != 'draft' or row.wbs_id.state != 'released':
                raise UserError(_('Submit a draft cash forecast against a released WBS.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state == 'approved':
                continue
            if row.state != 'submitted':
                raise UserError(_('Approve a submitted cash forecast.'))
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='cash_in_forecast' if row.cash_direction == 'in' else 'cash_out_forecast',
                amount=row.amount, source_model=row._name, source_id=row.id,
                source_key='%s:%s:approved_cash_forecast' % (row._name, row.id), entry_date=row.expected_date,
                notes=row.source_reference,
            )
            row._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted cash forecasts can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectCashflowReport(models.TransientModel):
    _name = 'jakh.project.cashflow.report'
    _description = 'Project cash forecast report'

    name = fields.Char(compute='_compute_name', string='Report')
    project_id = fields.Many2one('project.project', required=True, string='Project')
    company_id = fields.Many2one(related='project_id.company_id', string='Company')
    currency_id = fields.Many2one(related='company_id.currency_id')
    date_from = fields.Date(required=True, default=fields.Date.context_today, string='From date')
    date_to = fields.Date(required=True, default=lambda self: fields.Date.context_today(self) + timedelta(days=180), string='To date')
    opening_cash = fields.Monetary(default=0, string='Opening project cash position', groups=FINANCIAL)
    line_ids = fields.One2many('jakh.project.cashflow.report.line', 'wizard_id', readonly=True, string='Cash forecast lines')
    scope_note = fields.Text(readonly=True, string='Calculation basis')

    @api.depends('project_id')
    def _compute_name(self):
        for row in self:
            row.name = self.env._('Project cash forecast · %s') % (row.project_id.name or '')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, {'line_ids', 'scope_note'})
                or any(set(values) & {'line_ids', 'scope_note'} for values in values_list)):
            raise AccessError(_('Generate cash-flow output from approved cash forecasts.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'line_ids', 'scope_note'}:
            raise AccessError(_('Cash-flow output is generated from approved cash forecasts.'))
        if set(values) & {'project_id', 'date_from', 'date_to', 'opening_cash'}:
            self.line_ids._clear_report()
            values = {**values, 'scope_note': _('Regenerate this report after changing its scope.')}
        return super().write(values)

    def action_generate(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        self.ensure_one()
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select this project company in the company switcher.'))
        if self.date_from > self.date_to:
            raise UserError(_('The cash forecast end date must be on or after the start date.'))
        entries = self.env['jakh.project.control.entry'].search([
            ('project_id', '=', self.project_id.id), ('entry_type', 'in', ['cash_in_forecast', 'cash_out_forecast']),
            ('date', '>=', self.date_from), ('date', '<=', self.date_to),
        ])
        monthly = defaultdict(lambda: defaultdict(Decimal))
        for entry in entries:
            month = entry.date.replace(day=1)
            monthly[month][entry.entry_type] += Decimal(str(entry.amount))
        running = Decimal(str(self.opening_cash))
        values = []
        for month in sorted(monthly):
            cash_in = monthly[month]['cash_in_forecast']
            cash_out = monthly[month]['cash_out_forecast']
            net = cash_in - cash_out
            running += net
            values.append({'wizard_id': self.id, 'period': month, 'cash_in': float(cash_in),
                           'cash_out': float(cash_out), 'net_cash': float(net), 'closing_cash': float(running)})
        self.line_ids._clear_report()
        self.env['jakh.project.cashflow.report.line']._create_report(values)
        super().write({'scope_note': self.env._('Company-currency, approved project cash forecasts only. Cash-in and cash-out are dated by their approved expected date. This forecast does not represent bank cash, statutory indirect cash flow, or unposted supplier/customer transactions.')})
        self.env['jakh.audit.event']._append(self.company_id, 'cash_forecast.report_generated', self, {'rows': len(values)})
        return form_action(self)


class ProjectCashflowReportLine(models.TransientModel):
    _name = 'jakh.project.cashflow.report.line'
    _description = 'Project cash forecast detail'
    _order = 'period'

    wizard_id = fields.Many2one('jakh.project.cashflow.report', required=True, ondelete='cascade', string='Report')
    company_id = fields.Many2one(related='wizard_id.company_id', string='Company')
    currency_id = fields.Many2one(related='company_id.currency_id')
    period = fields.Date(required=True, string='Month')
    cash_in = fields.Monetary(string='Cash in')
    cash_out = fields.Monetary(string='Cash out')
    net_cash = fields.Monetary(string='Net cash')
    closing_cash = fields.Monetary(string='Closing cash')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Generate cash-forecast report rows through the report wizard.'))

    def _create_report(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().create(values_list) if values_list else self.browse()

    def write(self, values):
        raise AccessError(_('Generated cash-forecast rows are immutable.'))

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Clear cash-forecast output by regenerating its report.'))

    def _clear_report(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().unlink()


class ReportSchedule(models.Model):
    _name = 'jakh.report.schedule'
    _description = 'Permission-aware scheduled project report'
    _check_company_auto = True
    _order = 'next_run_on, id'

    name = fields.Char(required=True, default=lambda self: self.env._('New report schedule'))
    report_type = fields.Selection([('cvr', 'CVR control snapshot'), ('cash_flow', 'Cash forecast snapshot')], required=True, default='cvr')
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True)
    owner_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user)
    frequency = fields.Selection([('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')], required=True, default='weekly')
    next_run_on = fields.Date(required=True, default=fields.Date.context_today)
    state = fields.Selection([('active', 'Active'), ('paused', 'Paused')], required=True, default='active')
    last_run_at = fields.Datetime(readonly=True)
    delivery_ids = fields.One2many('jakh.report.delivery', 'schedule_id', readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, {'last_run_at', 'delivery_ids'})
                or any(set(values) & {'last_run_at', 'delivery_ids'} for values in values_list)):
            raise AccessError(_('Report delivery history is created by the scheduler.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & {'last_run_at', 'delivery_ids'}:
            raise AccessError(_('Report delivery history is created by the scheduler.'))
        return super().write(values)

    def _next_date(self, current):
        if self.frequency == 'daily':
            return current + timedelta(days=1)
        if self.frequency == 'weekly':
            return current + timedelta(days=7)
        return current + relativedelta(months=1)

    def _snapshot(self):
        Entry = self.env['jakh.project.control.entry']
        if self.report_type == 'cvr':
            entries = Entry.search([('project_id', '=', self.project_id.id)])
            facts = defaultdict(Decimal)
            for entry in entries:
                facts[entry.entry_type] += Decimal(str(entry.amount))
            return {'basis': 'approved project-control facts and posted native actuals', 'as_of': str(fields.Date.context_today(self)),
                    'entries': len(entries), 'commitments': float(facts['commitment']), 'actual_cost': float(facts['actual_cost']),
                    'forecast_final_cost': float(facts['forecast_final_cost']), 'earned_value': float(facts['earned_value'])}
        entries = Entry.search([('project_id', '=', self.project_id.id), ('entry_type', 'in', ['cash_in_forecast', 'cash_out_forecast'])])
        return {'basis': 'approved project cash forecast facts', 'as_of': str(fields.Date.context_today(self)),
                'cash_in_forecast': float(sum(Decimal(str(item.amount)) for item in entries.filtered(lambda entry: entry.entry_type == 'cash_in_forecast'))),
                'cash_out_forecast': float(sum(Decimal(str(item.amount)) for item in entries.filtered(lambda entry: entry.entry_type == 'cash_out_forecast')))}

    def _run_once(self):
        self.ensure_one()
        if self.state != 'active':
            return self.env['jakh.report.delivery'].browse()
        snapshot = self._snapshot()
        delivery = self.env['jakh.report.delivery']._record(self, snapshot)
        # This release records an in-app, permission-aware run.  Outbound mail
        # remains intentionally off until the customer enables a controlled
        # mail gateway and recipient policy.
        super(ReportSchedule, self).write({'last_run_at': fields.Datetime.now(), 'next_run_on': self._next_date(self.next_run_on)})
        self.env['jakh.audit.event']._append(self.company_id, 'report_schedule.generated', self, {'delivery': delivery.id, 'type': self.report_type})
        return delivery

    def action_run_now(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for row in self:
            if row.company_id not in self.env.companies:
                raise AccessError(_('Select the matching legal entity before running a scheduled report.'))
            row._run_once()
        return True

    def action_pause(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        self.write({'state': 'paused'})
        return True

    def action_resume(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        self.write({'state': 'active'})
        return True

    @api.model
    def _cron_run_due(self):
        # Cron is intentionally a pure in-app scheduler; it never sends a
        # report outside the current customer's permission boundary.
        today = fields.Date.context_today(self)
        for row in self.sudo().search([('state', '=', 'active'), ('next_run_on', '<=', today)]):
            row._run_once()


class ReportDelivery(models.Model):
    _name = 'jakh.report.delivery'
    _description = 'Generated scheduled report snapshot'
    _check_company_auto = True
    _order = 'generated_at desc, id desc'

    schedule_id = fields.Many2one('jakh.report.schedule', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='schedule_id.company_id', store=True, readonly=True)
    project_id = fields.Many2one(related='schedule_id.project_id', store=True, readonly=True)
    owner_id = fields.Many2one(related='schedule_id.owner_id', store=True, readonly=True)
    report_type = fields.Selection(related='schedule_id.report_type', store=True, readonly=True)
    generated_at = fields.Datetime(required=True, readonly=True)
    snapshot = fields.Text(required=True, readonly=True)
    state = fields.Selection([('generated', 'Generated')], default='generated', required=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Scheduled report snapshots are created by the scheduler.'))

    @api.model
    def _record(self, schedule, snapshot):
        self.check_access('create')
        return super(ReportDelivery, self).create({
            'schedule_id': schedule.id, 'generated_at': fields.Datetime.now(),
            'snapshot': json.dumps(snapshot, sort_keys=True),
        })

    def write(self, values):
        raise AccessError(_('Scheduled report snapshots are immutable.'))

    def unlink(self):
        raise AccessError(_('Scheduled report snapshots cannot be deleted.'))


class ProductionReadiness(models.Model):
    _name = 'jakh.production.readiness'
    _description = 'Customer production and localization acceptance gate'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New production acceptance'))
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    customer_reference = fields.Char(required=True, help='Customer tenant or deployment reference; never enter production credentials.')
    country = fields.Selection([('ae', 'United Arab Emirates'), ('sa', 'Saudi Arabia')], required=True, default='ae')
    evidence_attachment_ids = fields.Many2many('ir.attachment', 'jakh_production_readiness_attachment_rel', 'readiness_id', 'attachment_id', string='Acceptance evidence')
    tenant_isolation_verified = fields.Boolean(string='Separate customer database and filestore verified')
    backup_restore_verified = fields.Boolean(string='Timed database and filestore restore verified')
    sso_mfa_verified = fields.Boolean(string='SSO/MFA enrollment verified')
    security_review_verified = fields.Boolean(string='Independent security review accepted')
    finance_uat_verified = fields.Boolean(string='Finance and project reconciliation UAT accepted')
    localization_verified = fields.Boolean(string='Country localization and e-invoicing production acceptance evidenced')
    zero_critical_high_defects = fields.Boolean(string='No open critical or high defects')
    rpo_minutes = fields.Integer(required=True, default=60, string='Measured RPO (minutes)')
    rto_minutes = fields.Integer(required=True, default=480, string='Measured RTO (minutes)')
    state = fields.Selection([('draft', 'Draft'), ('ready', 'Evidence complete'), ('accepted', 'Production accepted'), ('rejected', 'Rejected')], default='draft', required=True, readonly=True, tracking=True)
    accepted_by = fields.Many2one('res.users', readonly=True)
    accepted_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'accepted_by', 'accepted_at'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list)):
            raise AccessError(_('Production acceptance workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled production-acceptance actions.'))
        if self.filtered(lambda row: row.state not in ('draft', 'rejected')):
            raise UserError(_('Accepted production evidence is immutable. Create a new acceptance gate for a later release.'))
        if 'company_id' in values:
            raise AccessError(_('Create a new production acceptance gate for another legal entity.'))
        return super().write(values)

    def _requirements_missing(self):
        self.ensure_one()
        checks = {
            _('customer isolation evidence'): self.tenant_isolation_verified,
            _('timed backup and restore evidence'): self.backup_restore_verified,
            _('SSO/MFA evidence'): self.sso_mfa_verified,
            _('independent security review'): self.security_review_verified,
            _('finance and project UAT reconciliation'): self.finance_uat_verified,
            _('country localization and production e-invoicing acceptance'): self.localization_verified,
            _('zero open critical/high defects'): self.zero_critical_high_defects,
            _('acceptance attachments'): bool(self.evidence_attachment_ids),
            _('measured RPO of 60 minutes or less'): 0 <= self.rpo_minutes <= 60,
            _('measured RTO of 480 minutes or less'): 0 <= self.rto_minutes <= 480,
        }
        return [label for label, complete in checks.items() if not complete]

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'production_readiness.' + row.state, row, {
                'country': row.country, 'customer_reference': row.customer_reference,
            })
        return result

    def action_mark_ready(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            if row.state not in ('draft', 'rejected'):
                raise UserError(_('Only a draft or rejected production gate can be prepared.'))
            missing = row._requirements_missing()
            if missing:
                raise UserError(_('Production acceptance is missing: %s') % ', '.join(missing))
            row._transition(state='ready')
        return True

    def action_accept(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            lock_records(row)
            missing = row._requirements_missing()
            if row.state != 'ready' or missing:
                raise UserError(_('Accept only evidence-complete production gates.'))
            row._transition(state='accepted', accepted_by=self.env.user.id, accepted_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            lock_records(row)
            if row.state != 'ready':
                raise UserError(_('Only evidence-complete production gates can be rejected.'))
            row._transition(state='rejected')
        return True
