"""Post-award BOQ changes on the same contract, without rewriting its baseline.

Approved revisions publish existing owner-variation and budget-revision facts.
Unperformed omissions reduce measurement scope, not a second invoice deduction.
Already-certified work instead uses the existing financial adjustment workflow.
"""
import json
import math
from collections import defaultdict
from decimal import Decimal

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .project_control import ProjectVariation as OriginalVariation
from .procurement import FINANCIAL, creation_parents, form_action, lock_editable, lock_records, require_role


def _finite(values, names):
    try:
        return all(math.isfinite(float(values[name] or 0)) for name in names if name in values)
    except (ValueError, TypeError, OverflowError):
        return False


class ContractBaselineCost(models.Model):
    _inherit = 'jakh.project.contract.line'

    original_direct_unit_cost = fields.Float(digits=(16, 6), groups=FINANCIAL, string='Award direct unit cost')
    original_cost_total = fields.Monetary(groups=FINANCIAL, string='Award direct cost')
    original_budget_total = fields.Monetary(groups=FINANCIAL, string='Award cost budget',
        help='Frozen direct cost plus allocated indirect cost; not the selling price.')
    source_line_uuid = fields.Char(copy=False, index=True, string='BOQ source identity')
    _source_identity = models.Constraint('UNIQUE(contract_id,source_line_uuid)', 'Preserve one original BOQ source identity per contract.')

    @api.model_create_multi
    def create(self, values_list):
        if any(not _finite(values, ('original_direct_unit_cost', 'original_cost_total', 'original_budget_total')) for values in values_list):
            raise ValidationError(_('Award costs must be finite, non-negative amounts.'))
        return super().create(values_list)

    def write(self, values):
        if not _finite(values, ('original_direct_unit_cost', 'original_cost_total', 'original_budget_total')):
            raise ValidationError(_('Award costs must be finite, non-negative amounts.'))
        return super().write(values)

    @api.constrains('original_direct_unit_cost', 'original_cost_total', 'original_budget_total')
    def _check_baseline_cost(self):
        for line in self:
            amounts = [line.original_direct_unit_cost, line.original_cost_total, line.original_budget_total]
            if any(not math.isfinite(value) or value < 0 for value in amounts):
                raise ValidationError(_('Award costs must be finite, non-negative amounts.'))

    def _jakh_measurement_limit(self, as_of=None):
        self.ensure_one()
        domain = [('contract_line_id', '=', self.id), ('boq_scope_omission', '=', True),
                  ('state', 'in', ['approved', 'certified'])]
        if as_of:
            domain.append(('date', '<=', as_of))
        omitted = sum(self.env['jakh.project.variation'].search(domain).mapped('quantity'))
        return max(0, self.quantity - omitted)


class ContractCurrentBOQ(models.Model):
    _inherit = 'jakh.project.contract'

    boq_revision_ids = fields.One2many('jakh.contract.boq.revision', 'contract_id', string='Approved BOQ revisions')
    boq_owner_variation_ids = fields.One2many('jakh.project.variation', 'contract_id', groups=FINANCIAL, string='Owner variation facts')
    original_contract_value = fields.Monetary(compute='_compute_boq_values', groups=FINANCIAL, string='Original contract value')
    current_contract_value = fields.Monetary(compute='_compute_boq_values', groups=FINANCIAL, string='Revised contract value')

    @api.depends('line_ids.quantity', 'line_ids.unit_price', 'boq_owner_variation_ids.state', 'boq_owner_variation_ids.amount')
    def _compute_boq_values(self):
        for contract in self:
            original = sum(line.quantity * line.unit_price for line in contract.line_ids)
            changes = self.env['jakh.project.variation'].search([
                ('contract_id', '=', contract.id), ('variation_type', '=', 'owner'),
                ('state', 'in', ['approved', 'certified']),
            ])
            contract.original_contract_value = original
            contract.current_contract_value = original + sum(changes.mapped('amount'))

    def action_new_boq_revision(self):
        self.ensure_one()
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if self.state != 'active':
            raise UserError(_('Create post-award BOQ revisions against an active contract.'))
        return {'type': 'ir.actions.act_window', 'name': _('BOQ revision'),
                'res_model': 'jakh.contract.boq.revision', 'view_mode': 'form',
                'context': {'default_contract_id': self.id}}

    def action_current_boq(self):
        self.ensure_one()
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        report = self.env['jakh.contract.boq.report'].create({'contract_id': self.id})
        return report.action_generate()


class OwnerVariationBOQScope(models.Model):
    _inherit = 'jakh.project.variation'

    boq_scope_omission = fields.Boolean(readonly=True, copy=False,
        help='Approved unperformed scope reduction; excluded from negative certificate deductions.')
    boq_revision_line_id = fields.Many2one('jakh.contract.boq.revision.line', readonly=True, copy=False, check_company=True)
    boq_item_code = fields.Char(readonly=True, copy=False)
    _server_fields = OriginalVariation._server_fields | {'boq_scope_omission', 'boq_revision_line_id', 'boq_item_code'}

    def _jakh_attach_boq_revision(self, line):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one(); line.ensure_one(); self.check_access('write')
        if self.state != 'draft' or self.contract_id != line.revision_id.contract_id:
            raise UserError(_('Attach BOQ revision provenance only to its matching draft owner variation.'))
        return super(OriginalVariation, self).write({
            'boq_revision_line_id': line.id, 'boq_item_code': line.code,
            'boq_scope_omission': line.effect == 'omission',
        })


class ContractBOQRevision(models.Model):
    _name = 'jakh.contract.boq.revision'
    _description = 'Controlled post-award BOQ revision'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'approved_number desc, id desc'

    name = fields.Char(required=True, default=lambda self: self.env._('New BOQ revision'), tracking=True)
    contract_id = fields.Many2one('jakh.project.contract', required=True, check_company=True, ondelete='restrict')
    project_id = fields.Many2one(related='contract_id.project_id', store=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    reason = fields.Text(required=True, tracking=True)
    line_ids = fields.One2many('jakh.contract.boq.revision.line', 'revision_id')
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('rejected', 'Rejected')],
                            readonly=True, required=True, default='draft', tracking=True)
    basis_revision = fields.Integer(readonly=True, copy=False)
    approved_number = fields.Integer(readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    snapshot_json = fields.Text(readonly=True, copy=False, groups=FINANCIAL)
    budget_revision_ids = fields.Many2many('jakh.project.budget.revision', 'jakh_boq_budget_revision_rel',
        'boq_revision_id', 'budget_revision_id', readonly=True, copy=False, check_company=True)
    _server_fields = {'state', 'basis_revision', 'approved_number', 'approved_by', 'approved_at',
                      'snapshot_json', 'budget_revision_ids'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if any(set(values) & self._server_fields for values in values_list):
            raise AccessError(_('BOQ revision workflow evidence is assigned by server actions.'))
        records = super().create(values_list)
        for record in records:
            record.project_id._jakh_assert_open()
            if record.contract_id.state != 'active':
                raise ValidationError(_('A post-award BOQ revision requires an active contract.'))
        return records

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if set(values) & (self._server_fields | {'contract_id'}):
            raise AccessError(_('Use the controlled BOQ revision actions; the contract cannot be changed.'))
        lock_editable(self, ('draft',), _('Submitted BOQ revision evidence is immutable.'))
        self.project_id._jakh_assert_open()
        return super().write(values)

    def unlink(self):
        lock_editable(self, ('draft',), _('Only draft BOQ revisions can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write')
        before = {'state': self.state}
        result = super().write(values)
        self.env['jakh.audit.event']._append(self.company_id, 'boq_revision.' + self.state, self,
            {'before': before, 'after': {'state': self.state, 'approved_number': self.approved_number}})
        return result

    def action_submit(self):
        for revision in self:
            lock_records(revision); revision.contract_id._locked()
            revision.project_id._jakh_assert_open()
            if revision.state != 'draft' or not revision.line_ids or not (revision.reason or '').strip():
                raise UserError(_('Submit a draft BOQ revision with changed items and a documented reason.'))
            revision.line_ids._check_scope()
            revision._transition(state='submitted', basis_revision=revision.contract_id.revision)
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for revision in self:
            lock_records(revision); lock_records(revision.contract_id, touch=True)
            revision.project_id._jakh_assert_open()
            if revision.state == 'approved':
                continue
            if revision.state != 'submitted' or revision.contract_id.state != 'active':
                raise UserError(_('Approve a submitted BOQ revision against an active contract.'))
            if revision.contract_id.revision != revision.basis_revision:
                raise UserError(_('The contract changed after submission. Reject this revision and submit a reviewed replacement.'))
            latest_certificate = self.env['jakh.payment.certificate'].search([
                ('contract_id', '=', revision.contract_id.id), ('state', 'in', ['approved', 'invoiced']),
            ], order='approval_sequence desc', limit=1)
            latest_revision = self.search([('contract_id', '=', revision.contract_id.id), ('state', '=', 'approved')],
                                          order='approved_number desc', limit=1)
            if (revision.date > fields.Date.context_today(revision)
                    or latest_certificate and revision.date < latest_certificate.date
                    or latest_revision and revision.date < latest_revision.date):
                raise UserError(_('A BOQ revision cannot precede accepted commercial evidence or take effect in the future.'))
            revision.line_ids._check_scope()
            # Serialize each WBS budget and create one controlled budget amendment
            # per affected WBS, not independent amounts that overwrite each other.
            deltas = defaultdict(Decimal)
            omissions = defaultdict(float)
            prior_measurements = {line.contract_line_id.id: line.cumulative_quantity for line in latest_certificate.line_ids}
            for line in revision.line_ids:
                deltas[line.wbs_id.id] += Decimal(str(line.budget_delta))
                if line.effect == 'omission':
                    omissions[line.contract_line_id.id] += line.quantity
            for item_id, omitted in omissions.items():
                item = self.env['jakh.project.contract.line'].browse(item_id)
                if omitted > item._jakh_measurement_limit() - prior_measurements.get(item_id, 0) + 1e-6:
                    raise UserError(_('An omission cannot remove already certified work or exceed the remaining original scope.'))
            budget_revisions = self.env['jakh.project.budget.revision']
            for wbs_id, delta in sorted(deltas.items()):
                if revision.currency_id.is_zero(float(delta)):
                    continue
                wbs = self.env['jakh.wbs'].browse(wbs_id)
                lock_records(wbs, budget=True)
                budget = self.env['jakh.project.budget.revision'].create({
                    'name': revision.name, 'wbs_id': wbs.id, 'date': revision.date,
                    'proposed_budget': float(Entry._effective_budget(wbs, revision.date) + delta),
                    'reason': revision.reason,
                })
                budget.action_submit(); budget.action_approve()
                budget_revisions |= budget
            for line in revision.line_ids:
                variation = self.env['jakh.project.variation'].create({
                    'name': '%s — %s' % (revision.name, line.code), 'variation_type': 'owner',
                    'effect': line.effect, 'project_id': revision.project_id.id, 'wbs_id': line.wbs_id.id,
                    'contract_id': revision.contract_id.id, 'contract_line_id': line.contract_line_id.id,
                    'partner_id': revision.contract_id.partner_id.id, 'date': revision.date,
                    'description': '%s\n%s' % (line.name, revision.reason), 'quantity': line.quantity,
                    'uom_id': line.uom_id.id, 'unit_price': line.unit_price,
                    'location_id': line.location_id.id, 'discipline_id': line.discipline_id.id,
                })
                variation._jakh_attach_boq_revision(line)
                variation.action_submit(); variation.action_price(); variation.action_approve()
                super(ContractBOQRevisionLine, line).write({'variation_id': variation.id})
            snapshot = {
                'contract_id': revision.contract_id.id,
                'original_contract_value': revision.contract_id.original_contract_value,
                'reason': revision.reason,
                'lines': [{'code': line.code, 'effect': line.effect, 'contract_line_id': line.contract_line_id.id,
                    'quantity': line.quantity, 'unit_price': line.unit_price, 'direct_unit_cost': line.direct_unit_cost,
                    'budget_delta': line.budget_delta, 'wbs_id': line.wbs_id.id, 'location_id': line.location_id.id,
                    'discipline_id': line.discipline_id.id, 'variation_id': line.variation_id.id}
                    for line in revision.line_ids],
            }
            revision.contract_id._transition(revision=revision.contract_id.revision + 1)
            revision._transition(state='approved', approved_number=latest_revision.approved_number + 1,
                approved_by=self.env.uid, approved_at=fields.Datetime.now(), snapshot_json=json.dumps(snapshot, sort_keys=True),
                budget_revision_ids=[(6, 0, budget_revisions.ids)])
        return True

    def action_reject(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for revision in self:
            lock_records(revision)
            if revision.state != 'submitted':
                raise UserError(_('Only submitted BOQ revisions can be rejected.'))
            revision._transition(state='rejected')
        return True


class ContractBOQRevisionLine(models.Model):
    _name = 'jakh.contract.boq.revision.line'
    _description = 'Controlled BOQ revision item'
    _check_company_auto = True

    revision_id = fields.Many2one('jakh.contract.boq.revision', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='revision_id.company_id', store=True)
    project_id = fields.Many2one(related='revision_id.project_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, string='Original BOQ item')
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    effect = fields.Selection([('addition', 'Addition'), ('omission', 'Unperformed scope omission')], default='addition', required=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    location_id = fields.Many2one('jakh.project.location', check_company=True)
    discipline_id = fields.Many2one('jakh.discipline', check_company=True)
    uom_id = fields.Many2one('uom.uom', required=True)
    quantity = fields.Float(required=True, digits=(16, 6), default=1)
    unit_price = fields.Float(required=True, digits=(16, 6), groups=FINANCIAL)
    direct_unit_cost = fields.Float(required=True, digits=(16, 6), groups=FINANCIAL)
    budget_delta = fields.Monetary(compute='_compute_delta', groups=FINANCIAL)
    variation_id = fields.Many2one('jakh.project.variation', readonly=True, copy=False, check_company=True)

    @api.depends('quantity', 'direct_unit_cost', 'effect')
    def _compute_delta(self):
        for line in self:
            line.budget_delta = line.quantity * line.direct_unit_cost * (-1 if line.effect == 'omission' else 1)

    @api.onchange('contract_line_id')
    def _onchange_contract_line(self):
        if self.contract_line_id:
            item = self.contract_line_id
            self.code, self.name, self.wbs_id, self.uom_id = item.code, item.name, item.wbs_id, item.uom_id
            self.unit_price, self.direct_unit_cost = item.unit_price, item.original_direct_unit_cost
            self.location_id, self.discipline_id = item.location_id, item.discipline_id

    @api.model_create_multi
    def create(self, values_list):
        if any('variation_id' in values or not _finite(values, ('quantity', 'unit_price', 'direct_unit_cost')) for values in values_list):
            raise AccessError(_('BOQ changes require finite amounts and workflow-controlled source links.'))
        parents = creation_parents(self, values_list, 'revision_id', 'jakh.contract.boq.revision')
        lock_editable(parents, ('draft',), _('Only draft BOQ revision items can be edited.'))
        records = super().create(values_list)
        records._check_scope()
        return records

    def write(self, values):
        if set(values) & {'variation_id', 'revision_id'}:
            raise AccessError(_('Revision items cannot move or assign their generated commercial documents.'))
        if not _finite(values, ('quantity', 'unit_price', 'direct_unit_cost')):
            raise ValidationError(_('BOQ quantity and pricing must be finite.'))
        lock_editable(self.revision_id, ('draft',), _('Only draft BOQ revision items can be edited.'))
        return super().write(values)

    def unlink(self):
        lock_editable(self.revision_id, ('draft',), _('Only draft BOQ revision items can be deleted.'))
        return super().unlink()

    @api.constrains('contract_line_id', 'wbs_id', 'uom_id', 'quantity', 'unit_price', 'direct_unit_cost',
                    'location_id', 'discipline_id', 'effect')
    def _check_scope(self):
        for line in self:
            if (line.quantity <= 0 or line.unit_price < 0 or line.direct_unit_cost < 0
                    or not all(math.isfinite(value) for value in (line.quantity, line.unit_price, line.direct_unit_cost))):
                raise ValidationError(_('BOQ changes require a positive finite quantity and non-negative finite prices.'))
            if line.wbs_id.project_id != line.project_id or line.location_id and line.location_id.project_id != line.project_id:
                raise ValidationError(_('The revised BOQ WBS and location must belong to the contract project.'))
            if line.discipline_id and line.discipline_id.company_id != line.company_id:
                raise ValidationError(_('The discipline must belong to the contract legal entity.'))
            item = line.contract_line_id
            if item and (item.contract_id != line.revision_id.contract_id or item.wbs_id != line.wbs_id or item.uom_id != line.uom_id):
                raise ValidationError(_('Original BOQ item, WBS and unit must match this contract revision.'))
            if line.effect == 'omission' and not item:
                raise ValidationError(_('An unperformed scope omission must identify its original BOQ item.'))
            if item and abs(line.unit_price - item.unit_price) > 1e-6:
                raise ValidationError(_('Preserve the original item rate. Repriced additional scope needs a separate new BOQ item.'))
            if not item:
                duplicate = line.revision_id.line_ids.filtered(lambda row: row != line and not row.contract_line_id and row.code == line.code)
                baseline = line.revision_id.contract_id.line_ids.filtered(lambda row: row.code == line.code)
                prior = self.env['jakh.project.variation'].search_count([
                    ('contract_id', '=', line.revision_id.contract_id.id), ('boq_item_code', '=', line.code),
                    ('contract_line_id', '=', False), ('state', 'in', ['approved', 'certified']),
                ])
                if duplicate or baseline or prior:
                    raise ValidationError(_('A new BOQ item needs a unique code within the contract.'))


class ContractBOQReport(models.TransientModel):
    _name = 'jakh.contract.boq.report'
    _description = 'Consolidated current contract BOQ'
    _check_company_auto = True

    contract_id = fields.Many2one('jakh.project.contract', required=True, check_company=True)
    project_id = fields.Many2one(related='contract_id.project_id')
    company_id = fields.Many2one(related='contract_id.company_id')
    currency_id = fields.Many2one(related='company_id.currency_id')
    original_value = fields.Monetary(readonly=True)
    revised_value = fields.Monetary(readonly=True)
    generated_at = fields.Datetime(readonly=True)
    line_ids = fields.One2many('jakh.contract.boq.report.line', 'report_id', readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if any(set(values) & {'original_value', 'revised_value', 'generated_at', 'line_ids'} for values in values_list):
            raise AccessError(_('Generate current BOQ evidence from the approved contract sources.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'original_value', 'revised_value', 'generated_at', 'line_ids'}:
            raise AccessError(_('Generated BOQ evidence cannot be edited.'))
        if 'contract_id' in values:
            self.line_ids._clear_report()
            result = super().write(values)
            super().write({'original_value': 0, 'revised_value': 0, 'generated_at': False})
            return result
        return super().write(values)

    def action_generate(self):
        self.ensure_one(); self.check_access('write')
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select the contract legal entity before generating its BOQ.'))
        variations = self.env['jakh.project.variation'].search([
            ('contract_id', '=', self.contract_id.id), ('variation_type', '=', 'owner'), ('state', 'in', ['approved', 'certified']),
        ])
        values = []
        folded = self.env['jakh.project.variation']
        for item in self.contract_id.line_ids:
            additions = variations.filtered(lambda row: row.contract_line_id == item and row.effect == 'addition' and abs(row.unit_price - item.unit_price) < 1e-6)
            omitted = variations.filtered(lambda row: row.contract_line_id == item and row.boq_scope_omission)
            folded |= additions | omitted
            quantity = item.quantity + sum(additions.mapped('quantity')) - sum(omitted.mapped('quantity'))
            values.append({'report_id': self.id, 'contract_line_id': item.id, 'code': item.code, 'name': item.name,
                'wbs_id': item.wbs_id.id, 'location_id': item.location_id.id, 'discipline_id': item.discipline_id.id,
                'uom_id': item.uom_id.id, 'original_quantity': item.quantity, 'current_quantity': quantity,
                'unit_price': item.unit_price, 'selling_value': quantity * item.unit_price})
        for variation in variations - folded:
            factor = -1 if variation.effect == 'omission' else 1
            values.append({'report_id': self.id, 'variation_id': variation.id,
                'code': variation.boq_item_code or 'VAR-%s' % variation.id, 'name': variation.name,
                'wbs_id': variation.wbs_id.id, 'location_id': variation.location_id.id, 'discipline_id': variation.discipline_id.id,
                'uom_id': variation.uom_id.id, 'original_quantity': 0, 'current_quantity': factor * variation.quantity,
                'unit_price': variation.unit_price, 'selling_value': variation.amount})
        self.line_ids._clear_report()
        self.env['jakh.contract.boq.report.line']._create_report(values)
        super().write({'original_value': self.contract_id.original_contract_value,
                       'revised_value': sum(row['selling_value'] for row in values), 'generated_at': fields.Datetime.now()})
        return form_action(self)


class ContractBOQReportLine(models.TransientModel):
    _name = 'jakh.contract.boq.report.line'
    _description = 'Current BOQ approved-scope row'
    _check_company_auto = True

    report_id = fields.Many2one('jakh.contract.boq.report', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='report_id.company_id')
    currency_id = fields.Many2one(related='company_id.currency_id')
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True)
    variation_id = fields.Many2one('jakh.project.variation', check_company=True)
    code = fields.Char()
    name = fields.Char()
    wbs_id = fields.Many2one('jakh.wbs', check_company=True)
    location_id = fields.Many2one('jakh.project.location', check_company=True)
    discipline_id = fields.Many2one('jakh.discipline', check_company=True)
    uom_id = fields.Many2one('uom.uom')
    original_quantity = fields.Float(digits=(16, 6))
    current_quantity = fields.Float(digits=(16, 6))
    unit_price = fields.Float(digits=(16, 6), groups=FINANCIAL)
    selling_value = fields.Monetary(groups=FINANCIAL)

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Current BOQ rows are generated from approved sources only.'))

    def _create_report(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().create(values_list)

    def write(self, values):
        raise AccessError(_('Generated BOQ rows are immutable.'))

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Regenerate the current BOQ rather than deleting its rows.'))

    def _clear_report(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        return super().unlink()
