"""Reviewed allocation of an original BOQ/WBS baseline to real buildings.

This is an immutable allocation snapshot, not a revised contract or another
cost ledger. Historical plans remain available to dated reporting.
"""
import math
from decimal import Decimal

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, lock_records, require_role
from .construction_dimensions import check_dimensions


_ALLOCATION_ROLES = ['group_manager', 'group_project_manager', 'group_finance']


class BoqLocationBudget(models.Model):
    _name = 'jakh.boq.location.budget'
    _description = 'Reviewed BOQ building budget allocation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc,id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New building allocation'), tracking=True)
    contract_line_id = fields.Many2one('jakh.project.contract.line', required=True, check_company=True, ondelete='restrict', string='BOQ item')
    contract_id = fields.Many2one(related='contract_line_id.contract_id', store=True)
    project_id = fields.Many2one(related='contract_id.project_id', store=True)
    wbs_id = fields.Many2one(related='contract_line_id.wbs_id', store=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    uom_id = fields.Many2one(related='contract_line_id.uom_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    reason = fields.Text(required=True)
    allocation_mode = fields.Selection([('complete', 'Complete quantity allocation'), ('partial', 'Partial; remainder unallocated')], required=True, default='complete')
    line_ids = fields.One2many('jakh.boq.location.budget.line', 'allocation_id', string='Building allocations')
    replaces_id = fields.Many2one('jakh.boq.location.budget', check_company=True, ondelete='restrict', copy=False, string='Replaces allocation')
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='draft', readonly=True, required=True, tracking=True)
    original_quantity = fields.Float(readonly=True, digits=(16, 6))
    original_wbs_budget = fields.Monetary(readonly=True, groups=FINANCIAL)
    allocated_quantity = fields.Float(compute='_compute_totals', digits=(16, 6))
    allocated_budget = fields.Monetary(compute='_compute_totals', groups=FINANCIAL)
    unallocated_quantity = fields.Float(readonly=True, digits=(16, 6))
    unallocated_wbs_budget = fields.Monetary(readonly=True, groups=FINANCIAL, string='Unallocated WBS budget at approval')
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    approved_revision = fields.Integer(readonly=True, copy=False, string='Approval revision')
    _server_fields = {'state', 'original_quantity', 'original_wbs_budget', 'unallocated_quantity', 'unallocated_wbs_budget', 'approved_by', 'approved_at', 'approved_revision'}
    _derived_fields = {'contract_id', 'project_id', 'wbs_id', 'company_id', 'currency_id', 'uom_id', 'allocated_quantity', 'allocated_budget'}

    @api.depends('line_ids.quantity', 'line_ids.budget_amount')
    def _compute_totals(self):
        for allocation in self:
            allocation.allocated_quantity = float(sum((Decimal(str(line.quantity)) for line in allocation.line_ids), Decimal(0)))
            allocation.allocated_budget = float(sum((Decimal(str(line.budget_amount)) for line in allocation.line_ids), Decimal(0)))

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, _ALLOCATION_ROLES)
        if any(set(values) & (self._server_fields | self._derived_fields) for values in values_list) or any(
                self.env.context.get('default_' + name) not in (None, False) for name in self._server_fields | self._derived_fields):
            raise AccessError(_('Create draft allocation plans; approval and scope snapshots are server assigned.'))
        allocations = super().create(values_list)
        allocations.project_id._jakh_assert_open()
        return allocations

    def write(self, values):
        require_role(self.env, _ALLOCATION_ROLES)
        lock_records(self, touch=True)
        if set(values) & (self._server_fields | self._derived_fields | {'contract_line_id'}):
            raise AccessError(_('Use allocation workflow actions; its source BOQ and approval evidence cannot be reassigned.'))
        if self.filtered(lambda plan: plan.state != 'draft'):
            raise UserError(_('Submitted building allocation plans are immutable. Create a reviewed replacement.'))
        self.project_id._jakh_assert_open()
        return super().write(values)

    def unlink(self):
        require_role(self.env, _ALLOCATION_ROLES)
        if self.filtered(lambda plan: plan.state != 'draft'):
            raise UserError(_('Retain submitted building allocation plans and their historical evidence.'))
        return super().unlink()

    @api.constrains('contract_line_id', 'date', 'replaces_id')
    def _check_scope(self):
        for plan in self:
            check_dimensions(plan, plan.project_id, plan.wbs_id, plan.contract_line_id)
            if plan.date and plan.date > fields.Date.context_today(plan):
                raise ValidationError(_('A building allocation date cannot be in the future.'))
            if plan.replaces_id and (plan.replaces_id == plan or plan.replaces_id.contract_line_id != plan.contract_line_id
                                     or plan.replaces_id.state != 'approved' or plan.date < plan.replaces_id.date):
                raise ValidationError(_('An allocation replacement must retain its BOQ and follow the approved source date.'))

    @api.model
    def _latest(self, project, as_of):
        """One accepted plan per BOQ, under the caller's current access rights."""
        project.check_access('read')
        plans = self.search([('project_id', '=', project.id), ('state', '=', 'approved'), ('date', '<=', as_of)], order='date desc,approved_revision desc,id desc')
        latest = {}
        for plan in plans:
            latest.setdefault(plan.contract_line_id.id, plan)
        return latest

    def _validate_dated_budget(self):
        """Conserve the original WBS baseline at every effective-date boundary.

        A backdated plan must not consume capacity that only became available
        after another BOQ's later replacement. The caller holds the shared WBS
        row-version lock so concurrent approvals serialize/retry. Decimal sums
        come from immutable lines, not a rounded display aggregate.
        """
        self.ensure_one()
        budget = Decimal(str(self.wbs_id.budget_amount))
        candidate = sum((Decimal(str(line.budget_amount)) for line in self.line_ids), Decimal(0))
        plans = self.search([('wbs_id', '=', self.wbs_id.id), ('state', '=', 'approved'),
                             ('date', '<=', fields.Date.context_today(self))],
                            order='date,approved_revision,id')
        breakpoints = sorted({self.date, *(plan.date for plan in plans if plan.date >= self.date)})
        latest_amounts = {}
        source_index = 0
        others_total = Decimal(0)
        for as_of in breakpoints:
            while source_index < len(plans) and plans[source_index].date <= as_of:
                source = plans[source_index]
                identity = source.contract_line_id.id
                if identity != self.contract_line_id.id:
                    amount = sum((Decimal(str(line.budget_amount)) for line in source.line_ids), Decimal(0))
                    others_total += amount - latest_amounts.get(identity, Decimal(0))
                    latest_amounts[identity] = amount
                source_index += 1
            total = candidate + others_total
            if not budget.is_finite() or total > budget:
                raise UserError(_('A dated building allocation cannot exceed the original WBS budget at any reporting date.'))
        return budget, candidate + others_total

    def _validate_quantities(self):
        self.ensure_one()
        if not self.line_ids or not (self.reason or '').strip():
            raise UserError(_('Add building quantities, budget amounts and a documented allocation basis.'))
        original = Decimal(str(self.contract_line_id.quantity))
        allocated = sum((Decimal(str(line.quantity)) for line in self.line_ids), Decimal(0))
        if not original.is_finite() or original <= 0:
            raise UserError(_('Allocate a finite positive original BOQ quantity.'))
        if allocated > original:
            raise UserError(_('Building allocation quantities cannot exceed the original BOQ quantity.'))
        if self.allocation_mode == 'complete' and allocated != original:
            raise UserError(_('A complete allocation must match the original BOQ quantity; choose partial mode to keep a remainder unallocated.'))
        return float(max(Decimal(0), original - allocated))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for plan in self:
            self.env['jakh.audit.event']._append(plan.company_id, 'boq_location_budget.' + plan.state, plan, {
                'boq': plan.contract_line_id.id, 'quantity': plan.allocated_quantity,
                'budget': plan.allocated_budget, 'replaces': plan.replaces_id.id,
                'approval_revision': plan.approved_revision,
            })
        return result

    def action_submit(self):
        require_role(self.env, _ALLOCATION_ROLES)
        for plan in self:
            lock_records(plan, touch=True); plan.project_id._jakh_assert_open()
            if plan.state != 'draft' or plan.contract_id.state != 'active' or plan.wbs_id.state != 'released':
                raise UserError(_('Submit a draft allocation against an active contract and released WBS.'))
            plan._check_scope(); plan._validate_quantities()
            plan._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for plan in self:
            lock_records(plan, touch=True)
            if plan.state == 'approved':
                continue
            plan.project_id._jakh_assert_open()
            if plan.state != 'submitted' or plan.contract_id.state != 'active' or plan.wbs_id.state != 'released':
                raise UserError(_('Approve a submitted allocation against an active contract and released WBS.'))
            lock_records(plan.wbs_id, budget=True, touch=True)
            plan._check_scope(); remainder = plan._validate_quantities()
            latest = self._latest(plan.project_id, fields.Date.context_today(plan))
            old = latest.get(plan.contract_line_id.id)
            if old and plan.replaces_id != old:
                raise UserError(_('Replace the latest approved BOQ allocation explicitly; stale or overlapping plans cannot be approved.'))
            if old and plan.date < old.date:
                raise UserError(_('A replacement allocation cannot precede its latest approved plan.'))
            if not old and plan.replaces_id:
                raise UserError(_('The allocation replacement source is no longer the approved baseline.'))
            budget, total = plan._validate_dated_budget()
            plan._transition(state='approved', original_quantity=plan.contract_line_id.quantity,
                             original_wbs_budget=plan.wbs_id.budget_amount, unallocated_quantity=remainder,
                             unallocated_wbs_budget=float(max(Decimal(0), budget - total)),
                             approved_by=self.env.uid, approved_at=fields.Datetime.now(),
                             approved_revision=(old.approved_revision if old else 0) + 1)
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for plan in self:
            lock_records(plan)
            if plan.state != 'submitted':
                raise UserError(_('Only submitted building allocations can be rejected.'))
            plan._transition(state='rejected')
        return True


class BoqLocationBudgetLine(models.Model):
    _name = 'jakh.boq.location.budget.line'
    _description = 'Reviewed BOQ building quantity and budget'
    _check_company_auto = True

    allocation_id = fields.Many2one('jakh.boq.location.budget', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='allocation_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    location_id = fields.Many2one('jakh.project.location', required=True, check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')
    quantity = fields.Float(required=True, digits=(16, 6))
    budget_amount = fields.Monetary(required=True, groups=FINANCIAL)
    _location_once = models.Constraint('UNIQUE(allocation_id,location_id)', 'Allocate each location once within a BOQ plan.')

    def _editable(self):
        require_role(self.env, _ALLOCATION_ROLES)
        lock_records(self.allocation_id, touch=True)
        if self.allocation_id.filtered(lambda plan: plan.state != 'draft'):
            raise UserError(_('Submitted allocation quantities and budgets are immutable.'))
        self.allocation_id.project_id._jakh_assert_open()

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, _ALLOCATION_ROLES)
        if any(set(values) & {'company_id', 'currency_id'} for values in values_list) or any(
                self.env.context.get('default_' + name) not in (None, False) for name in ('company_id', 'currency_id')):
            raise AccessError(_('Allocation row company and currency are derived from the approved source scope.'))
        plans = self.env['jakh.boq.location.budget'].browse([
            values.get('allocation_id', self.env.context.get('default_allocation_id')) for values in values_list
            if values.get('allocation_id', self.env.context.get('default_allocation_id'))])
        lock_records(plans, touch=True)
        if plans.filtered(lambda plan: plan.state != 'draft'):
            raise UserError(_('Submitted allocation quantities and budgets are immutable.'))
        for values in values_list:
            if any(name in values and not math.isfinite(float(values[name] or 0)) for name in ('quantity', 'budget_amount')):
                raise ValidationError(_('Allocation quantities and budgets must be finite and non-negative.'))
        records = super().create(values_list)
        records._editable()
        return records

    def write(self, values):
        if set(values) & {'allocation_id', 'company_id', 'currency_id'}:
            raise AccessError(_('Allocation rows cannot move between plans.'))
        self._editable()
        if any(name in values and not math.isfinite(float(values[name] or 0)) for name in ('quantity', 'budget_amount')):
            raise ValidationError(_('Allocation quantities and budgets must be finite and non-negative.'))
        return super().write(values)

    def unlink(self):
        self._editable()
        return super().unlink()

    @api.constrains('allocation_id', 'location_id', 'discipline_id', 'quantity', 'budget_amount')
    def _check_row(self):
        for row in self:
            check_dimensions(row, row.allocation_id.project_id, row.allocation_id.wbs_id,
                             row.allocation_id.contract_line_id, row.location_id, row.discipline_id)
            if (not all(math.isfinite(value) for value in (row.quantity, row.budget_amount))
                    or row.quantity <= 0 or row.budget_amount < 0):
                raise ValidationError(_('Allocation quantities must be positive and budgets non-negative finite amounts.'))


class ContractLineAllocationNavigation(models.Model):
    _inherit = 'jakh.project.contract.line'
    location_budget_ids = fields.One2many('jakh.boq.location.budget', 'contract_line_id', string='Reviewed building allocations')

    def action_open_location_budgets(self):
        self.ensure_one(); self.check_access('read')
        require_role(self.env, _ALLOCATION_ROLES)
        return {'type': 'ir.actions.act_window', 'name': self.env._('BOQ building allocations'),
                'res_model': 'jakh.boq.location.budget', 'view_mode': 'list,form',
                'domain': [('contract_line_id', '=', self.id)], 'context': {'default_contract_line_id': self.id}}
