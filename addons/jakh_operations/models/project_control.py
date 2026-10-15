"""Controlled project-cost facts and commercial forecast workflows.

The construction ledger is deliberately append-only.  It is not an accounting
ledger and never posts journals; it provides the project-control view that
links approved commitments, native posted actuals, certificates and approved
commercial decisions back to a project WBS.
"""
from collections import defaultdict
import math
from decimal import Decimal

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role


CONTROL_TYPES = [
    ('budget_revision', 'Approved budget revision'),
    ('commitment', 'Approved commitment'),
    ('receipt', 'Received cost exposure'),
    ('actual_cost', 'Posted actual cost'),
    ('forecast_final_cost', 'Forecast final cost'),
    ('variation_revenue', 'Approved owner variation'),
    ('variation_cost', 'Approved subcontract variation'),
    ('certified_revenue', 'Certified revenue'),
    ('billed_revenue', 'Posted billing'),
    ('planned_value', 'Accepted planned value'),
    ('earned_value', 'Accepted earned value'),
    ('cash_in_forecast', 'Approved cash-in forecast'),
    ('cash_out_forecast', 'Approved cash-out forecast'),
]


class ProjectControlEntry(models.Model):
    _name = 'jakh.project.control.entry'
    _description = 'Immutable project-control ledger entry'
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    project_id = fields.Many2one('project.project', required=True, readonly=True, check_company=True, index=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, readonly=True, check_company=True, index=True)
    cost_code_id = fields.Many2one(related='wbs_id.cost_code_id', readonly=True, store=True)
    partner_id = fields.Many2one('res.partner', readonly=True, check_company=True)
    contract_id = fields.Many2one('jakh.project.contract', readonly=True, check_company=True)
    contract_line_id = fields.Many2one('jakh.project.contract.line', readonly=True, check_company=True)
    entry_type = fields.Selection(CONTROL_TYPES, required=True, readonly=True, index=True)
    amount = fields.Monetary(required=True, readonly=True, groups='jakh_core.group_financial', help='Signed company-currency amount. Reversals are separate entries.')
    quantity = fields.Float(readonly=True, digits=(16, 6))
    date = fields.Date(required=True, readonly=True, index=True)
    source_model = fields.Char(required=True, readonly=True, index=True)
    source_id = fields.Integer(required=True, readonly=True, index=True)
    source_key = fields.Char(required=True, readonly=True, index=True)
    provenance = fields.Selection([('native', 'Native workflow'), ('integration', 'Validated integration')], required=True, readonly=True, default='native')
    notes = fields.Char(readonly=True)

    _source_once = models.Constraint('UNIQUE(company_id,source_key)', 'A project-control source can be recorded only once.')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Project-control entries are created by approved server workflows only.'))

    def write(self, values):
        raise AccessError(_('Project-control entries are immutable. Record a controlled reversal instead.'))

    def unlink(self):
        raise AccessError(_('Project-control entries cannot be deleted.'))

    @api.constrains('project_id', 'wbs_id', 'company_id', 'contract_id', 'contract_line_id')
    def _check_scope(self):
        for entry in self:
            if entry.wbs_id.project_id != entry.project_id or entry.wbs_id.company_id != entry.company_id:
                raise ValidationError(_('A project-control entry must use a WBS from its project and legal entity.'))
            if entry.contract_id and entry.contract_id.project_id != entry.project_id:
                raise ValidationError(_('The commercial contract must belong to the project-control project.'))
            if entry.contract_line_id and (
                entry.contract_line_id.contract_id != entry.contract_id
                or entry.contract_line_id.wbs_id != entry.wbs_id
            ):
                raise ValidationError(_('The commercial BOQ line must match the project-control contract and WBS.'))

    @api.model
    def _record(self, *, name, company, project, wbs, entry_type, amount, source_model,
                source_id, source_key, entry_date=None, quantity=0, partner=False,
                contract=False, contract_line=False, provenance='native', notes=False,
                location=None, discipline=None):
        """Create an idempotent fact from an authorized server action only."""
        if not all(math.isfinite(float(value)) for value in (amount, quantity)):
            raise ValidationError(self.env._('%(label)s must be a finite decimal.', label=self.env._('Signed amount')))
        if entry_type not in dict(CONTROL_TYPES):
            raise ValidationError(self.env._('Use a recognized project-control entry type.'))
        if not source_model or not source_id or not source_key:
            raise ValidationError(self.env._('Project-control entries require a source identity and idempotency key.'))
        if company not in self.env.companies or project.company_id != company or wbs.project_id != project:
            raise AccessError(self.env._('Select the matching company, project and WBS before recording project control.'))
        if provenance not in ('native', 'integration'):
            raise ValidationError(self.env._('Use native workflow or validated integration provenance.'))
        if contract and contract.project_id != project:
            raise ValidationError(self.env._('The commercial contract must belong to the project-control project.'))
        if contract_line and (contract_line.contract_id != contract or contract_line.wbs_id != wbs):
            raise ValidationError(self.env._('The commercial BOQ line must match the project-control contract and WBS.'))
        # Infer only explicit, permission-readable source links. Never allocate
        # old WBS-level totals to an arbitrary BOQ item or building.
        source = self.env[source_model].browse(source_id).exists() if source_model in self.env else False
        if source:
            source.check_access('read')
            if 'company_id' in source._fields and source.company_id and source.company_id != company:
                raise ValidationError(self.env._('A project-control source must belong to the same legal entity as its fact.'))
            context = source
            if source_model == 'jakh.purchase.request.cost':
                context = source.line_id
            elif source_model == 'jakh.subcontract.service.reversal':
                context = source.service_entry_id
            elif source_model == 'jakh.subcontract.amendment':
                context = source.purchase_line_id.jakh_request_line_id
            elif source_model in ('stock.move', 'account.move.line') and source.purchase_line_id.jakh_request_line_id:
                context = source.purchase_line_id.jakh_request_line_id
            if 'contract_line_id' in context._fields:
                contract_line = contract_line or context.contract_line_id
                contract = contract or contract_line.contract_id
            if 'location_id' in context._fields and context._fields['location_id'].comodel_name == 'jakh.project.location':
                if location is None:
                    location = context.location_id
            if 'discipline_id' in context._fields:
                if discipline is None:
                    discipline = context.discipline_id
        if contract_line:
            if location is None:
                location = contract_line.location_id
            if discipline is None:
                discipline = contract_line.discipline_id
        from .construction_dimensions import check_dimensions
        check_dimensions(self, project, wbs, contract_line, location, discipline)
        # Existing workflow actions acquire their WBS/source lock first. This
        # company lock then freezes the currency basis against first-fact and
        # currency-change races without locking unrelated project history.
        company._jakh_lock_construction_currency()
        from odoo.tools.float_utils import float_round
        normalized_amount = company.currency_id.round(float(amount))
        normalized_quantity = float_round(float(quantity), precision_digits=6)
        effective_date = fields.Date.to_date(entry_date) if entry_date else fields.Date.context_today(self)
        # Operational warehouse staff can trigger trusted receipt facts without
        # acquiring read access to financial ledger data. Elevate only this
        # append-only store, after source/company/project checks above; uid and
        # audit identity remain the actual operator.
        self.check_access('create')
        store = self if self.has_access('read') else self.sudo()
        existing = store.search([('company_id', '=', company.id), ('source_key', '=', source_key)], limit=1)
        if existing:
            same = (
                existing.company_id == company and existing.entry_type == entry_type
                and existing.project_id == project and existing.wbs_id == wbs
                and existing.source_model == source_model and existing.source_id == source_id
            )
            if not same:
                raise ValidationError(self.env._('The idempotency key is already used by a different project-control fact.'))
            if ((existing.contract_line_id.id or False) != (contract_line.id if contract_line else False)
                    or (existing.location_id.id or False) != (location.id if location else False)
                    or (existing.discipline_id.id or False) != (discipline.id if discipline else False)):
                raise ValidationError(self.env._('A recorded cost fact cannot be reassigned to a different BOQ item or location.'))
            if (company.currency_id.round(existing.amount) != normalized_amount
                    or float_round(existing.quantity, precision_digits=6) != normalized_quantity):
                raise ValidationError(self.env._('A replay cannot change the recorded amount or quantity.'))
            if existing.date != effective_date:
                raise ValidationError(self.env._('A replay cannot change the recorded effective date.'))
            if ((existing.partner_id.id or False) != (partner.id if partner else False)
                    or (existing.contract_id.id or False) != (contract.id if contract else False)
                    or existing.provenance != provenance):
                raise ValidationError(self.env._('A replay cannot change the recorded counterparty, contract or provenance.'))
            return existing
        values = {
            'name': name,
            'company_id': company.id,
            'project_id': project.id,
            'wbs_id': wbs.id,
            'partner_id': partner.id if partner else False,
            'contract_id': contract.id if contract else False,
            'contract_line_id': contract_line.id if contract_line else False,
            'location_id': location.id if location else False,
            'discipline_id': discipline.id if discipline else False,
            'entry_type': entry_type,
            'amount': normalized_amount,
            'quantity': normalized_quantity,
            'date': effective_date,
            'source_model': source_model,
            'source_id': source_id,
            'source_key': source_key,
            'provenance': provenance,
            'notes': notes or False,
        }
        # Bypass the public create guard but retain the caller's ACL and audit actor.
        self.check_access('create')
        entry = super(ProjectControlEntry, store).create(values)
        self.env['jakh.audit.event']._append(company, 'project_control.recorded', entry, {
            'type': entry_type, 'source': source_key, 'amount': normalized_amount,
        })
        return entry

    @api.model
    def _sum(self, wbs, entry_types, date_to=False):
        domain = [('wbs_id', '=', wbs.id), ('entry_type', 'in', entry_types)]
        if date_to:
            domain.append(('date', '<=', date_to))
        return sum(Decimal(str(value)) for value in self.search(domain).mapped('amount'))

    @api.model
    def _effective_budget(self, wbs, date_to=False):
        return Decimal(str(wbs.budget_amount)) + self._sum(wbs, ['budget_revision'], date_to)

    @api.model
    def _latest_forecast(self, wbs, date_to=False):
        domain = [('wbs_id', '=', wbs.id), ('entry_type', '=', 'forecast_final_cost')]
        if date_to:
            domain.append(('date', '<=', date_to))
        return self.search(domain, order='date desc, id desc', limit=1)


class ProjectBudgetRevision(models.Model):
    _name = 'jakh.project.budget.revision'
    _description = 'Controlled project budget revision'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New budget revision'), tracking=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    project_id = fields.Many2one(related='wbs_id.project_id', store=True)
    company_id = fields.Many2one(related='wbs_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    proposed_budget = fields.Monetary(required=True, groups=FINANCIAL)
    reason = fields.Text(required=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    approved_delta = fields.Monetary(readonly=True, groups=FINANCIAL)

    _server_fields = {'state', 'approved_by', 'approved_at', 'approved_delta'}
    _nonnegative = models.Constraint('CHECK(proposed_budget >= 0)', 'Proposed budget cannot be negative.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list):
            raise AccessError(_('Budget-revision workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled budget-revision actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted budget revisions are immutable.'))
        if 'wbs_id' in values:
            raise AccessError(_('Create a new budget revision instead of moving it to another WBS.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft budget revisions can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'budget_revision.' + row.state, row, {'amount': row.proposed_budget})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row)
            if row.state != 'draft' or not row.reason.strip():
                raise UserError(_('Submit a draft budget revision with a documented reason.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); lock_records(row.wbs_id, budget=True)
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.wbs_id.state != 'released':
                raise UserError(_('Approve a submitted revision against a released WBS.'))
            current = Entry._effective_budget(row.wbs_id, row.date)
            commitments = Entry._sum(row.wbs_id, ['commitment'], row.date)
            actuals = Entry._sum(row.wbs_id, ['actual_cost'], row.date)
            proposed = Decimal(str(row.proposed_budget))
            if proposed < max(Decimal(0), commitments, actuals):
                raise UserError(_('The revised budget cannot fall below approved commitments or posted actual costs.'))
            delta = proposed - current
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='budget_revision', amount=delta, source_model=row._name, source_id=row.id,
                source_key='%s:%s:approved_budget' % (row._name, row.id), entry_date=row.date,
                notes=row.reason,
            )
            row._transition(state='approved', approved_by=self.env.user.id,
                            approved_at=fields.Datetime.now(), approved_delta=float(delta))
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted budget revisions can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectForecast(models.Model):
    _name = 'jakh.project.forecast'
    _description = 'Approved project forecast final cost'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'as_of_date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New cost forecast'), tracking=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    project_id = fields.Many2one(related='wbs_id.project_id', store=True)
    company_id = fields.Many2one(related='wbs_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    as_of_date = fields.Date(required=True, default=fields.Date.context_today)
    forecast_final_cost = fields.Monetary(required=True, groups=FINANCIAL)
    basis = fields.Text(required=True, help='State the committed, actual, progress and risk basis used for this forecast.')
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'approved_by', 'approved_at'}
    _nonnegative = models.Constraint('CHECK(forecast_final_cost >= 0)', 'Forecast final cost cannot be negative.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list):
            raise AccessError(_('Forecast workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled forecast actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted forecasts are immutable. Create a new forecast revision.'))
        if 'wbs_id' in values:
            raise AccessError(_('Create a new forecast instead of moving it to another WBS.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft forecasts can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'forecast.' + row.state, row, {'forecast': row.forecast_final_cost})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row)
            if row.state != 'draft' or not row.basis.strip():
                raise UserError(_('Submit a draft forecast with its calculation basis.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); lock_records(row.wbs_id, budget=True)
            if row.state == 'approved':
                continue
            if row.state != 'submitted' or row.wbs_id.state != 'released':
                raise UserError(_('Approve a submitted forecast against a released WBS.'))
            actuals = Entry._sum(row.wbs_id, ['actual_cost'], row.as_of_date)
            if Decimal(str(row.forecast_final_cost)) < actuals:
                raise UserError(_('Forecast final cost cannot be below posted actual costs.'))
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='forecast_final_cost', amount=row.forecast_final_cost,
                source_model=row._name, source_id=row.id,
                source_key='%s:%s:forecast_final_cost' % (row._name, row.id),
                entry_date=row.as_of_date, notes=row.basis,
            )
            row._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Only submitted forecasts can be rejected.'))
            row._transition(state='rejected')
        return True


class ProjectVariation(models.Model):
    _name = 'jakh.project.variation'
    _description = 'Controlled construction variation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New variation'), tracking=True)
    variation_type = fields.Selection([('owner', 'Owner variation'), ('subcontract', 'Subcontract variation')], required=True, default='owner')
    effect = fields.Selection([('addition', 'Addition'), ('omission', 'Omission / credit')], required=True, default='addition')
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    contract_id = fields.Many2one('jakh.project.contract', check_company=True)
    partner_id = fields.Many2one('res.partner', check_company=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    description = fields.Text(required=True)
    quantity = fields.Float(required=True, default=1, digits=(16, 6))
    uom_id = fields.Many2one('uom.uom', required=True)
    unit_price = fields.Float(required=True, digits=(16, 6), groups=FINANCIAL)
    amount = fields.Monetary(compute='_compute_amount', store=True, readonly=True, groups=FINANCIAL)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('priced', 'Priced'), ('approved', 'Approved'), ('certified', 'Fully certified'), ('rejected', 'Rejected')], default='draft', required=True, readonly=True, tracking=True)
    priced_by = fields.Many2one('res.users', readonly=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)

    _server_fields = {'state', 'priced_by', 'approved_by', 'approved_at', 'amount'}
    _positive = models.Constraint('CHECK(quantity > 0 AND unit_price >= 0)', 'Variation quantity must be positive and the unit price cannot be negative.')

    @api.depends('quantity', 'unit_price', 'effect')
    def _compute_amount(self):
        for row in self:
            amount = Decimal(str(row.quantity)) * Decimal(str(row.unit_price))
            row.amount = float(-amount if row.effect == 'omission' else amount)

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if any(set(values) & self._server_fields or values.get('state', 'draft') != 'draft' for values in values_list):
            raise AccessError(_('Variation workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled variation actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted variations are immutable. Create a new variation revision.'))
        if set(values) & {'project_id', 'wbs_id', 'contract_id'}:
            raise AccessError(_('Create a new variation instead of moving its commercial scope.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft variations can be deleted.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'contract_id', 'variation_type', 'partner_id')
    def _check_scope(self):
        for row in self:
            if row.wbs_id.project_id != row.project_id:
                raise ValidationError(_('The variation WBS must belong to its project.'))
            if row.contract_id and row.contract_id.project_id != row.project_id:
                raise ValidationError(_('The variation contract must belong to its project.'))
            if row.variation_type == 'owner' and row.contract_id and row.partner_id and row.partner_id != row.contract_id.partner_id:
                raise ValidationError(_('An owner variation customer must match the project contract.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'variation.' + row.state, row, {'amount': row.amount, 'type': row.variation_type})
        return result

    def action_submit(self):
        for row in self:
            lock_records(row)
            if row.state != 'draft' or not row.description.strip() or row.wbs_id.state != 'released':
                raise UserError(_('Submit a documented variation against a released WBS.'))
            if row.variation_type == 'owner' and (not row.contract_id or row.contract_id.state != 'active'):
                raise UserError(_('An owner variation requires an active measured contract.'))
            if row.variation_type == 'subcontract' and not row.partner_id:
                raise UserError(_('A subcontract variation requires the affected subcontractor.'))
            row._transition(state='submitted')
        return True

    def action_price(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state != 'submitted':
                raise UserError(_('Price a submitted variation before approval.'))
            row._transition(state='priced', priced_by=self.env.user.id)
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for row in self:
            lock_records(row); lock_records(row.wbs_id, budget=True)
            if row.state == 'approved':
                continue
            if row.state != 'priced':
                raise UserError(_('Finance must price the submitted variation before approval.'))
            if row.variation_type == 'owner':
                if not row.contract_id or row.contract_id.state != 'active':
                    raise UserError(_('An owner variation requires an active measured contract.'))
                if not row.partner_id:
                    super(ProjectVariation, row).write({'partner_id': row.contract_id.partner_id.id})
                entry_type = 'variation_revenue'
            else:
                entry_type = 'variation_cost'
            Entry._record(
                name=row.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type=entry_type, amount=row.amount, source_model=row._name, source_id=row.id,
                source_key='%s:%s:approved' % (row._name, row.id), entry_date=row.date,
                partner=row.partner_id, contract=row.contract_id, notes=row.description,
            )
            row._transition(state='approved', approved_by=self.env.user.id, approved_at=fields.Datetime.now())
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state not in ('submitted', 'priced'):
                raise UserError(_('Only submitted or priced variations can be rejected.'))
            row._transition(state='rejected')
        return True

    def _mark_certified(self):
        for row in self.filtered(lambda variation: variation.state == 'approved'):
            super(ProjectVariation, row).write({'state': 'certified'})
            self.env['jakh.audit.event']._append(row.company_id, 'variation.certified', row, {'amount': row.amount})


class CertificateVariationLine(models.Model):
    _name = 'jakh.payment.certificate.variation.line'
    _description = 'Cumulative variation measurement'
    _check_company_auto = True

    certificate_id = fields.Many2one('jakh.payment.certificate', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='certificate_id.company_id', store=True)
    variation_id = fields.Many2one('jakh.project.variation', required=True, check_company=True)
    cumulative_quantity = fields.Float(required=True, digits=(16, 6))
    _one_variation = models.Constraint('UNIQUE(certificate_id,variation_id)', 'Measure each variation once per certificate.')
    _nonnegative = models.Constraint('CHECK(cumulative_quantity >= 0)', 'Variation measurements cannot be negative.')

    def _editable(self):
        self.certificate_id._ensure_draft_measurement()

    @api.model_create_multi
    def create(self, values_list):
        parents = self.env['jakh.payment.certificate'].browse([value.get('certificate_id') for value in values_list if value.get('certificate_id')])
        parents._ensure_draft_measurement()
        records = super().create(values_list)
        records._editable()
        return records

    def write(self, values):
        self._editable()
        if 'certificate_id' in values or 'variation_id' in values:
            raise AccessError(_('Variation measurements cannot move between certificates or variations.'))
        return super().write(values)

    def unlink(self):
        self._editable()
        return super().unlink()

    @api.constrains('certificate_id', 'variation_id')
    def _check_variation(self):
        for line in self:
            variation = line.variation_id
            if (
                variation.variation_type != 'owner'
                or variation.contract_id != line.certificate_id.contract_id
                or variation.project_id != line.certificate_id.contract_id.project_id
                or variation.state not in ('approved', 'certified')
            ):
                raise ValidationError(_('Certificate variation measurements require an approved owner variation on the same contract.'))


class ProjectControlReport(models.TransientModel):
    _name = 'jakh.project.control.report'
    _description = 'Project cost and forecast control report'

    name = fields.Char(compute='_compute_name', string='Report')
    project_id = fields.Many2one('project.project', required=True, string='Project')
    company_id = fields.Many2one(related='project_id.company_id', string='Company')
    currency_id = fields.Many2one(related='company_id.currency_id')
    as_of_date = fields.Date(required=True, default=fields.Date.context_today, string='As of date')
    line_ids = fields.One2many('jakh.project.control.report.line', 'wizard_id', readonly=True, string='Cost control lines')
    scope_note = fields.Text(readonly=True, string='Calculation basis')

    @api.depends('project_id')
    def _compute_name(self):
        for row in self:
            row.name = self.env._('Project cost & forecast · %s') % (row.project_id.name or '')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if any(set(values) & {'line_ids', 'scope_note'} for values in values_list):
            raise AccessError(_('Generate project-control output from approved and posted sources.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'line_ids', 'scope_note'}:
            raise AccessError(_('Project-control output is generated from approved and posted sources.'))
        if set(values) & {'project_id', 'as_of_date'}:
            self.line_ids._clear_report()
            values = {**values, 'scope_note': _('Regenerate this report after changing its scope.')}
        return super().write(values)

    def action_generate(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        self.ensure_one(); self.check_access('write')
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select this project company in the company switcher.'))
        Entry = self.env['jakh.project.control.entry']
        entries = Entry.search([('project_id', '=', self.project_id.id), ('date', '<=', self.as_of_date)])
        grouped = defaultdict(lambda: defaultdict(Decimal))
        latest_forecasts = {}
        for entry in entries:
            grouped[entry.wbs_id.id][entry.entry_type] += Decimal(str(entry.amount))
            if entry.entry_type == 'forecast_final_cost':
                current = latest_forecasts.get(entry.wbs_id.id)
                if not current or (entry.date, entry.id) > (current.date, current.id):
                    latest_forecasts[entry.wbs_id.id] = entry
        revenue = defaultdict(Decimal)
        contracts = self.env['jakh.project.contract'].search([
            ('project_id', '=', self.project_id.id), ('state', 'in', ['active', 'closed'])
        ])
        for line in contracts.line_ids:
            revenue[line.wbs_id.id] += Decimal(str(line.quantity)) * Decimal(str(line.unit_price))
        values = []
        for wbs in self.env['jakh.wbs'].search([('project_id', '=', self.project_id.id)], order='sequence, code, id'):
            facts = grouped[wbs.id]
            original_budget = Decimal(str(wbs.budget_amount))
            revised_budget = original_budget + facts['budget_revision']
            actual = facts['actual_cost']
            forecast = Decimal(str(latest_forecasts[wbs.id].amount)) if wbs.id in latest_forecasts else revised_budget
            revised_revenue = revenue[wbs.id] + facts['variation_revenue']
            values.append({
                'wizard_id': self.id, 'wbs_id': wbs.id,
                'original_budget': float(original_budget), 'revised_budget': float(revised_budget),
                'commitments': float(facts['commitment']), 'received_cost': float(facts['receipt']),
                'actual_cost': float(actual), 'forecast_final_cost': float(forecast),
                'forecast_to_complete': float(max(Decimal(0), forecast - actual)),
                'original_revenue': float(revenue[wbs.id]), 'variation_revenue': float(facts['variation_revenue']),
                'variation_cost': float(facts['variation_cost']), 'revised_revenue': float(revised_revenue),
                'certified_revenue': float(facts['certified_revenue']), 'billed_revenue': float(facts['billed_revenue']),
                'forecast_margin': float(revised_revenue - forecast),
                'planned_value': float(facts['planned_value']), 'earned_value': float(facts['earned_value']),
                'cost_performance_index': float(facts['earned_value'] / actual) if actual > 0 else 0,
                'schedule_performance_index': float(facts['earned_value'] / facts['planned_value']) if facts['planned_value'] > 0 else 0,
                'cash_in_forecast': float(facts['cash_in_forecast']), 'cash_out_forecast': float(facts['cash_out_forecast']),
                'forecast_cash_net': float(facts['cash_in_forecast'] - facts['cash_out_forecast']),
            })
        self.line_ids._clear_report()
        self.env['jakh.project.control.report.line']._create_report(values)
        note = self.env._('Company-currency project control as of the selected date. Budget revisions, commitments, approved variations and forecasts are controlled operational facts; actual costs and billing are created only from posted native accounting documents or accepted verified labour/plant feeds. Received cost exposure, including approved subcontract service entries, is shown separately and is not added to actual cost. CPI equals earned value divided by actual cost; SPI equals earned value divided by planned value. Forecast final cost is the latest approved forecast, or the revised budget where no forecast is approved.')
        super().write({'scope_note': note})
        self.env['jakh.audit.event']._append(self.company_id, 'project_control.report_generated', self, {'project': self.project_id.id, 'rows': len(values), 'as_of': str(self.as_of_date)})
        return form_action(self)

    def action_pivot(self):
        self.ensure_one(); self.check_access('read')
        return {
            'type': 'ir.actions.act_window', 'name': _('Project cost & forecast analysis'),
            'res_model': 'jakh.project.control.report.line', 'view_mode': 'pivot,graph,list',
            'domain': [('wizard_id', '=', self.id)], 'context': {'create': False},
        }


class ProjectControlReportLine(models.TransientModel):
    _name = 'jakh.project.control.report.line'
    _description = 'Project cost and forecast detail'

    wizard_id = fields.Many2one('jakh.project.control.report', required=True, ondelete='cascade', string='Report')
    company_id = fields.Many2one(related='wizard_id.company_id', string='Company')
    currency_id = fields.Many2one(related='company_id.currency_id')
    wbs_id = fields.Many2one('jakh.wbs', required=True, string='WBS')
    original_budget = fields.Monetary(string='Original budget')
    revised_budget = fields.Monetary(string='Revised budget')
    commitments = fields.Monetary(string='Commitments')
    received_cost = fields.Monetary(string='Received cost exposure')
    actual_cost = fields.Monetary(string='Actual cost')
    forecast_final_cost = fields.Monetary(string='Forecast final cost')
    forecast_to_complete = fields.Monetary(string='Forecast to complete')
    original_revenue = fields.Monetary(string='Original revenue')
    variation_revenue = fields.Monetary(string='Variation revenue')
    variation_cost = fields.Monetary(string='Variation cost')
    revised_revenue = fields.Monetary(string='Revised revenue')
    certified_revenue = fields.Monetary(string='Certified revenue')
    billed_revenue = fields.Monetary(string='Billed revenue')
    forecast_margin = fields.Monetary(string='Forecast margin')
    planned_value = fields.Monetary(string='Planned value')
    earned_value = fields.Monetary(string='Earned value')
    cost_performance_index = fields.Float(string='CPI', digits=(16, 4))
    schedule_performance_index = fields.Float(string='SPI', digits=(16, 4))
    cash_in_forecast = fields.Monetary(string='Cash-in forecast')
    cash_out_forecast = fields.Monetary(string='Cash-out forecast')
    forecast_cash_net = fields.Monetary(string='Forecast cash net')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Generate project-control report rows through the report wizard.'))

    def _create_report(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().create(values_list) if values_list else self.browse()

    def write(self, values):
        raise AccessError(_('Generated project-control rows are immutable.'))

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Clear project-control output by regenerating its report.'))

    def _clear_report(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        result = super()._post(soft=soft)
        Entry = self.env['jakh.project.control.entry']
        for move in self.filtered(lambda row: row.state == 'posted'):
            if move.jakh_certificate_id:
                certificate = move.jakh_certificate_id
                previous = self.env['jakh.payment.certificate'].search([
                    ('contract_id', '=', certificate.contract_id.id),
                    ('state', 'in', ['approved', 'invoiced']),
                    ('approval_sequence', '<', certificate.approval_sequence),
                ], order='approval_sequence desc', limit=1)
                prior_base = {line.contract_line_id.id: line.cumulative_quantity for line in previous.line_ids}
                prior_variation = {line.variation_id.id: line.cumulative_quantity for line in previous.variation_line_ids}
                for line in certificate.line_ids:
                    amount = (line.cumulative_quantity - prior_base.get(line.contract_line_id.id, 0)) * line.contract_line_id.unit_price
                    Entry._record(
                        name=move.name, company=move.company_id, project=certificate.contract_id.project_id,
                        wbs=line.contract_line_id.wbs_id, entry_type='billed_revenue', amount=amount,
                        source_model=move._name, source_id=move.id,
                        source_key='%s:%s:billed_boq:%s' % (move._name, move.id, line.contract_line_id.id),
                        entry_date=move.date, partner=move.partner_id, contract=certificate.contract_id,
                        contract_line=line.contract_line_id,
                    )
                for line in certificate.variation_line_ids:
                    variation = line.variation_id
                    factor = -1 if variation.effect == 'omission' else 1
                    amount = factor * (line.cumulative_quantity - prior_variation.get(variation.id, 0)) * variation.unit_price
                    Entry._record(
                        name=move.name, company=move.company_id, project=certificate.contract_id.project_id,
                        wbs=variation.wbs_id, entry_type='billed_revenue', amount=amount,
                        source_model=move._name, source_id=move.id,
                        source_key='%s:%s:billed_variation:%s' % (move._name, move.id, variation.id),
                        entry_date=move.date, partner=move.partner_id, contract=certificate.contract_id,
                        contract_line=variation.contract_line_id,
                        location=variation.location_id, discipline=variation.discipline_id,
                    )
            if move.move_type in ('in_invoice', 'in_refund'):
                for line in move.invoice_line_ids.filtered(lambda item: item.purchase_line_id.jakh_request_line_id):
                    request_line = line.purchase_line_id.jakh_request_line_id
                    request = request_line.request_id
                    if move.currency_id != move.company_id.currency_id:
                        raise UserError(_('Linked project supplier invoices require company currency until an approved FX workflow is enabled.'))
                    ordered_product = line.purchase_line_id.product_id
                    if ordered_product.is_storable and ordered_product.valuation == 'real_time':
                        if line.account_id != ordered_product.with_company(move.company_id)._get_product_accounts().get('stock_valuation'):
                            raise UserError(_('Stocked project materials must be billed to inventory assets; job cost is posted at site consumption.'))
                        continue
                    if line.account_id.account_type not in ('expense', 'expense_direct_cost', 'expense_depreciation'):
                        continue
                    entry = Entry._record(
                        name=move.name, company=move.company_id, project=request.project_id, wbs=request.wbs_id,
                        entry_type='actual_cost', amount=line.balance, source_model=line._name, source_id=line.id,
                        source_key='%s:%s:actual_cost' % (line._name, line.id), entry_date=move.date,
                        quantity=line.quantity, partner=move.partner_id, notes=line.name,
                    )
                    entry._attach_posting(line)
        return result


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        result = super()._action_done(cancel_backorder=cancel_backorder)
        Entry = self.env['jakh.project.control.entry']
        for move in result.filtered(lambda row: row.purchase_line_id.jakh_request_line_id
                                    and (row._should_count_for_quantity_received() or row._is_purchase_return())):
            request_line = move.purchase_line_id.jakh_request_line_id
            request = request_line.request_id
            sign = -1 if move.location_dest_id.usage == 'supplier' else 1
            Entry._record(
                name=move.reference or move.picking_id.name or move.display_name,
                company=move.company_id, project=request.project_id, wbs=request.wbs_id,
                entry_type='receipt', amount=sign * move.quantity * move.purchase_line_id.price_unit,
                quantity=sign * move.quantity, source_model=move._name, source_id=move.id,
                source_key='%s:%s:receipt' % (move._name, move.id),
                entry_date=fields.Date.to_date(move.date), partner=request.supplier_id,
                notes=move.product_id.display_name,
            )
        return result
