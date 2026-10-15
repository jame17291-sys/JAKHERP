import json
from decimal import Decimal
from uuid import uuid4

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from ..services.costing import calculate_estimate, compare_versions, json_value, _decimal, MAX_AMOUNT, MAX_QUANTITY

FINANCIAL = 'jakh_core.group_financial'
EDITORS = ('jakh_core.group_manager', 'jakh_core.group_estimator', 'jakh_core.group_platform_owner')
MANAGERS = ('jakh_core.group_manager', 'jakh_core.group_platform_owner')
INPUT_DEPENDS = (
    'unit_count', 'header_markup_percent', 'indirect_amount', 'overhead_percent', 'profit_percent', 'currency_id',
    'line_ids.code', 'line_ids.name', 'line_ids.quantity', 'line_ids.unit_cost',
    'line_ids.item_markup_percent', 'line_ids.uom_id', 'line_ids.secondary_uom_id',
    'line_ids.conversion_factor', 'line_ids.product_id', 'line_ids.cost_code_id',
    'line_ids.parent_uuid', 'line_ids.is_group', 'line_ids.allocation_percent',
    'line_ids.resource_ids.name', 'line_ids.resource_ids.resource_type',
    'line_ids.resource_ids.quantity', 'line_ids.resource_ids.unit_cost',
    'building_ids.number', 'building_ids.name', 'building_ids.override_ids.line_id',
    'building_ids.override_ids.quantity',
)


def _has_role(env, groups):
    return any(env.user.has_group(group) for group in groups)


def _window(model, record_id, title):
    return {'type': 'ir.actions.act_window', 'name': title, 'res_model': model, 'res_id': record_id, 'view_mode': 'form', 'target': 'current'}


def _validate_numbers(env, values, limits):
    # Check before native Float/Monetary precision converts malformed values.
    for name, maximum in limits.items():
        if name in values:
            try:
                _decimal(values[name], name, maximum, translate=env._)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc


class JakhEstimate(models.Model):
    _name = 'jakh.estimate'
    _description = 'Construction BOQ Estimate'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.audit.mixin']
    _order = 'create_date desc, id desc'
    _check_company_auto = True
    _jakh_audit_fields = ('title', 'unit_count', 'header_markup_percent', 'indirect_amount', 'overhead_percent', 'profit_percent')

    name = fields.Char(required=True, default=lambda self: self.env._('New'), copy=False, readonly=True, index=True)
    title = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id, groups=FINANCIAL)
    opportunity_id = fields.Many2one('crm.lead', check_company=True, tracking=True)
    partner_id = fields.Many2one('res.partner', string='Customer', check_company=True, required=True, tracking=True)
    estimator_id = fields.Many2one('res.users', default=lambda self: self.env.user, required=True)
    project_manager_id = fields.Many2one('res.users', domain=[('share', '=', False)])
    state = fields.Selection([('draft', 'Draft'), ('review', 'In review'), ('approved', 'Approved'), ('handed_over', 'Handed over'), ('cancel', 'Cancelled')], default='draft', required=True, readonly=True, copy=False, tracking=True)
    unit_count = fields.Integer(string='Repeated buildings', default=1, required=True, tracking=True)
    header_markup_percent = fields.Float(string='Header markup (%)', default=0, digits=(16, 6), groups=FINANCIAL, tracking=True)
    indirect_amount = fields.Monetary(string='Indirect cost pool', default=0, groups=FINANCIAL, tracking=True)
    overhead_percent = fields.Float(string='Overhead (%)', default=0, digits=(16, 6), groups=FINANCIAL, tracking=True)
    profit_percent = fields.Float(string='Profit (%)', default=0, digits=(16, 6), groups=FINANCIAL, tracking=True)
    pricing_basis = fields.Char(compute='_compute_pricing_basis', groups=FINANCIAL)
    line_ids = fields.One2many('jakh.estimate.line', 'estimate_id', string='BOQ lines', copy=True)
    building_ids = fields.One2many('jakh.estimate.building', 'estimate_id', string='Building overrides', copy=True)
    version_ids = fields.One2many('jakh.estimate.version', 'estimate_id', readonly=True, copy=False)
    version_count = fields.Integer(compute='_compute_version_count')
    version_label = fields.Char(string='Next study version label')
    approved_version_id = fields.Many2one('jakh.estimate.version', readonly=True, copy=False, ondelete='restrict', check_company=True)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    sale_order_id = fields.Many2one('sale.order', readonly=True, copy=False, check_company=True, ondelete='restrict')
    project_id = fields.Many2one('project.project', readonly=True, copy=False, check_company=True, ondelete='restrict')
    origin_estimate_id = fields.Many2one('jakh.estimate', readonly=True, copy=False, ondelete='restrict', check_company=True)
    cost_amount = fields.Monetary(string='Direct cost', compute='_compute_totals', store=True, compute_sudo=True, groups=FINANCIAL)
    sell_amount = fields.Monetary(string='Selling amount, excluding tax', compute='_compute_totals', store=True, compute_sudo=True, groups=FINANCIAL)
    margin_amount = fields.Monetary(string='Margin', compute='_compute_totals', store=True, compute_sudo=True, groups=FINANCIAL)
    budget_amount = fields.Monetary(string='Direct + allocated indirect cost', compute='_compute_totals', store=True, compute_sudo=True, groups=FINANCIAL)
    unallocated_indirect_amount = fields.Monetary(string='Unallocated indirect cost', compute='_compute_totals', store=True, compute_sudo=True, groups=FINANCIAL)
    note = fields.Text()

    _name_company_unique = models.Constraint('UNIQUE(company_id, name)', 'The estimate reference must be unique within the company.')
    _positive_units = models.Constraint('CHECK(unit_count >= 1 AND unit_count <= 100000)', 'Repeated buildings must be between 1 and 100,000.')
    _valid_markup = models.Constraint('CHECK(header_markup_percent >= 0 AND header_markup_percent <= 1000)', 'Header markup must be between zero and 1,000 percent.')

    def _check_editor(self):
        if not _has_role(self.env, EDITORS):
            raise AccessError(_('Only an estimator or construction manager can change an estimate.'))

    @api.depends_context('lang')
    def _compute_pricing_basis(self):
        for record in self:
            record.pricing_basis = self.env._('Direct + proportional indirect; overhead then profit then legacy item/header markup')

    def _check_manager(self):
        if not _has_role(self.env, MANAGERS):
            raise AccessError(_('Only a construction manager can approve or hand over an estimate.'))

    def _lock(self):
        existing = self.filtered(lambda record: isinstance(record.id, int))
        if existing:
            existing.check_access('write')
            existing.flush_recordset(['state'])
            self.env.cr.execute(SQL('SELECT id FROM %s WHERE id IN %s ORDER BY id FOR UPDATE', SQL.identifier(self._table), tuple(existing.ids)))
            existing.invalidate_recordset(['state', 'sale_order_id', 'project_id', 'approved_version_id', 'version_ids'])

    def _ensure_mutable(self):
        self._check_editor()
        self._lock()
        if any(record.state != 'draft' for record in self):
            raise UserError(_('This study is locked. Return a review to draft, or create a revision of an approved estimate.'))

    def _payload(self, strict=True):
        self.ensure_one()
        lines = []
        for line in self.line_ids.sorted('sequence'):
            if not strict and (not line.code or not line.name or not line.uom_id):
                continue
            resources = [{'type': resource.resource_type, 'description': resource.name, 'quantity': resource.quantity, 'unit_cost': resource.unit_cost} for resource in line.resource_ids if strict or resource.name]
            item = {'id': line.line_uuid, 'code': line.code, 'description': line.name, 'cost_code': line.cost_code_id.code or '', 'product_id': line.product_id.id or False, 'uom': line.uom_id.name, 'quantity': line.quantity, 'unit_cost': line.unit_cost, 'item_markup_percent': line.item_markup_percent, 'resources': resources,
                    'parent_uuid': line.parent_uuid or '', 'is_group': line.is_group, 'allocation_percent': line.allocation_percent}
            source = line.import_row_id
            origin = self.origin_estimate_id
            while not source and origin:
                original = origin.line_ids.filtered(lambda row: row.line_uuid == line.line_uuid)[:1]
                source = original.import_row_id
                origin = origin.origin_estimate_id
            if source:
                item['source_provenance'] = {'batch_id': source.batch_id.id, 'file_id': source.file_id.id,
                    'filename': source.file_id.filename, 'sheet': source.source_sheet, 'row': source.source_row,
                    'reviewed_by': source.batch_id.reviewed_by.id, 'reviewed_at': str(source.batch_id.reviewed_at)}
            if line.secondary_uom_id or line.conversion_factor:
                item.update(secondary_uom=line.secondary_uom_id.name or '', conversion_factor=line.conversion_factor)
            lines.append(item)
        line_ids = {item['id'] for item in lines}
        buildings = []
        for building in self.building_ids.sorted('number'):
            if not strict and not building.name:
                continue
            overrides = {}
            for override in building.override_ids:
                if override.line_id.line_uuid in line_ids:
                    if override.line_id.line_uuid in overrides:
                        raise ValidationError(_('A building can override each BOQ line only once.'))
                    overrides[override.line_id.line_uuid] = override.quantity
                elif strict:
                    raise ValidationError(_('A building override must refer to a line in this estimate.'))
            buildings.append({'number': building.number, 'name': building.name, 'overrides': overrides})
        return {'unit_count': self.unit_count, 'header_markup_percent': self.header_markup_percent, 'indirect_amount': self.indirect_amount,
                'overhead_percent': self.overhead_percent, 'profit_percent': self.profit_percent,
                'allow_unallocated_indirect': not strict,
                'currency_decimals': self.currency_id.decimal_places, 'currency_code': self.currency_id.name, 'lines': lines, 'buildings': buildings}

    def _calculate(self, strict=True):
        try:
            return calculate_estimate(self._payload(strict), translate=self.env._)
        except ValueError as error:
            raise ValidationError(str(error)) from error

    @api.depends(*INPUT_DEPENDS)
    def _compute_totals(self):
        for estimate in self:
            calculation = estimate._calculate(strict=False)
            estimate.cost_amount = float(calculation['cost_total'])
            estimate.sell_amount = float(calculation['sell_total'])
            estimate.margin_amount = float(calculation['margin'])
            estimate.budget_amount = float(calculation['budget_total'])
            estimate.unallocated_indirect_amount = float(calculation['unallocated_indirect'])

    @api.depends('version_ids')
    def _compute_version_count(self):
        for estimate in self:
            estimate.version_count = len(estimate.version_ids)

    @api.model_create_multi
    def create(self, values_list):
        self._check_editor()
        protected = {'approved_version_id', 'approved_by_id', 'approved_at', 'sale_order_id', 'project_id', 'origin_estimate_id', 'cost_amount', 'sell_amount', 'margin_amount', 'budget_amount', 'unallocated_indirect_amount', 'pricing_basis', 'version_ids'}
        for values in values_list:
            _validate_numbers(self.env, values, {'indirect_amount': MAX_AMOUNT, 'overhead_percent': 1000, 'profit_percent': 1000, 'header_markup_percent': 1000})
            if values.get('state', 'draft') != 'draft' or any(values.get(key) for key in protected):
                raise AccessError(_('Create a draft estimate; controlled references are assigned by workflow actions.'))
            if not values.get('name') or values['name'] in ('New', self.env._('New')):
                values['name'] = self.env['ir.sequence'].next_by_code('jakh.estimate') or f'EST-{uuid4().hex[:12]}'
        return super().create(values_list)

    def write(self, values):
        _validate_numbers(self.env, values, {'indirect_amount': MAX_AMOUNT, 'overhead_percent': 1000, 'profit_percent': 1000, 'header_markup_percent': 1000})
        protected = {'state', 'name', 'approved_version_id', 'approved_by_id', 'approved_at', 'sale_order_id', 'project_id', 'origin_estimate_id', 'cost_amount', 'sell_amount', 'margin_amount', 'budget_amount', 'unallocated_indirect_amount', 'pricing_basis', 'version_ids'}
        if protected.intersection(values):
            raise AccessError(_('Controlled estimate fields can only be changed by workflow actions.'))
        business = {'title', 'active', 'company_id', 'currency_id', 'opportunity_id', 'partner_id', 'estimator_id', 'project_manager_id', 'unit_count', 'header_markup_percent', 'indirect_amount', 'overhead_percent', 'profit_percent', 'line_ids', 'building_ids', 'version_label', 'note'}
        if business.intersection(values):
            self._ensure_mutable()
            if 'company_id' in values and any(record.line_ids or record.version_ids for record in self):
                raise UserError(_('Create a new company estimate rather than moving a study with lines or history.'))
        return super().write(values)

    def unlink(self):
        self._ensure_mutable()
        if any(record.version_ids for record in self):
            raise UserError(_('An estimate with saved versions must be retained; archive a draft instead.'))
        return super().unlink()

    def _create_snapshot(self, label):
        self.ensure_one()
        self._check_editor()
        self._lock()
        calculation = self._calculate()
        snapshot = json_value(calculation)
        snapshot.update(currency_id=self.currency_id.id, currency_code=self.currency_id.name, company_id=self.company_id.id)
        previous = self.version_ids.sorted('number')[-1:]
        comparison = json_value(compare_versions(previous.snapshot_json, snapshot, translate=self.env._)) if previous else {'added': snapshot['lines'], 'removed': [], 'modified': [], 'headers': {}}
        if previous and previous.currency_id != self.currency_id:
            comparison['sell_delta'] = comparison['cost_delta'] = None
            comparison['headers']['currency'] = {'before': previous.currency_id.name, 'after': self.currency_id.name}
        version = self.env['jakh.estimate.version']._create_snapshot(self, label, snapshot, comparison)
        self.env['jakh.audit.event']._append(self.company_id, 'estimate.version_saved', version, {'estimate_id': self.id, 'number': version.number})
        return version

    def action_save_version(self):
        self.ensure_one()
        self._ensure_mutable()
        label = (self.version_label or self.env._('Study %02d', len(self.version_ids) + 1)).strip()
        if not label or len(label) > 100:
            raise ValidationError(_('Use a version label of up to 100 characters.'))
        version = self._create_snapshot(label)
        self.message_post(body=_('Study version saved: %s', label))
        return _window('jakh.estimate.version', version.id, _('Study version'))

    def action_submit(self):
        self.ensure_one()
        self._ensure_mutable()
        if not self.line_ids:
            raise UserError(_('Add BOQ lines before submitting the estimate.'))
        calculation = self._calculate()
        for group in (line for line in calculation['lines'] if line.get('is_group')):
            children = [line for line in calculation['lines'] if line.get('parent_uuid') == group['id']]
            if not children or sum((line['cost_total'] for line in children), Decimal(0)) != group['cost_total'] or (
                all(line['allocation_percent'] for line in children) and sum((line['allocation_percent'] for line in children), Decimal(0)) != Decimal(100)
            ):
                raise UserError(_('Fully allocate every parent BOQ value before submitting the estimate.'))
        super(JakhEstimate, self).write({'state': 'review'})
        self.env['jakh.audit.event']._append(self.company_id, 'estimate.submitted', self)
        self.message_post(body=_('Estimate submitted for construction manager review.'))
        return True

    def action_return_to_draft(self):
        self._check_manager()
        self._lock()
        if any(record.state != 'review' for record in self):
            raise UserError(_('Only an estimate in review can be returned to draft.'))
        result = super().write({'state': 'draft'})
        for estimate in self:
            self.env['jakh.audit.event']._append(estimate.company_id, 'estimate.returned_to_draft', estimate)
        return result

    def action_approve(self):
        self.ensure_one()
        self._check_manager()
        self._lock()
        if self.state != 'review':
            raise UserError(_('Submit the estimate for review before approval.'))
        leaves = self.line_ids.filtered(lambda line: not line.is_group)
        if not leaves or any(not line.cost_code_id or not line.product_id for line in leaves):
            raise UserError(_('Map every BOQ line to a product and company cost code before approval.'))
        self._calculate()
        version = self._create_snapshot(self.env._('Approved %(name)s · %(number)02d', name=self.name, number=len(self.version_ids) + 1))
        super(JakhEstimate, self).write({'state': 'approved', 'approved_version_id': version.id, 'approved_by_id': self.env.uid, 'approved_at': fields.Datetime.now()})
        self.env['jakh.audit.event']._append(self.company_id, 'estimate.approved', self, {'version_id': version.id})
        self.message_post(body=_('Approved study %s is locked. Further changes require a new revision.', version.name))
        return True

    def action_new_revision(self):
        self.ensure_one()
        self._check_editor()
        self._lock()
        if self.state not in ('approved', 'handed_over', 'cancel'):
            raise UserError(_('Create a revision from an approved, handed-over or cancelled estimate.'))
        revision = self.copy({'title': f'{self.title} · revision', 'state': 'draft', 'active': True, 'building_ids': []})
        # Copy building overrides only after their stable BOQ IDs are remapped.
        old_to_new = {line.line_uuid: line for line in revision.line_ids}
        for building in self.building_ids:
            self.env['jakh.estimate.building'].create({'estimate_id': revision.id, 'number': building.number, 'name': building.name, 'override_ids': [Command.create({'line_id': old_to_new[override.line_id.line_uuid].id, 'quantity': override.quantity}) for override in building.override_ids]})
        super(JakhEstimate, revision).write({'origin_estimate_id': self.id})
        self.env['jakh.audit.event']._append(revision.company_id, 'estimate.revision_created', revision, {'origin_estimate_id': self.id})
        revision.message_post(body=_('New draft revision of %s. The original remains locked.', self.name))
        return _window('jakh.estimate', revision.id, _('Estimate revision'))

    def action_handover(self):
        self.ensure_one()
        self._check_manager()
        self._lock()
        if self.state == 'handed_over' and self.sale_order_id and self.project_id:
            return _window('sale.order', self.sale_order_id.id, _('Existing quotation'))
        if self.state != 'approved' or not self.approved_version_id:
            raise UserError(_('Approve and lock the estimate before handover.'))
        origin = self.origin_estimate_id
        while origin:
            if origin.project_id:
                raise UserError(_('Use a controlled BOQ revision on the existing contract after award; do not hand over a second project.'))
            origin = origin.origin_estimate_id
        if self.currency_id != self.company_id.currency_id:
            raise UserError(_('Handover requires the company currency. Preserve the source currency and have Finance prepare an explicitly converted company-currency study.'))
        if any(line.uom_id != line.product_id.uom_id for line in self.line_ids if not line.is_group):
            raise UserError(_('For this handover, map each primary BOQ unit to its product unit. Dual-unit conversions remain recorded in the approved study.'))
        snapshot = self.approved_version_id.snapshot_json
        sale_order = self.env['sale.order']._create_jakh_from_estimate(self, snapshot)
        project = self.env['project.project']._create_jakh_from_estimate(self)
        analytic = project._jakh_prepare_cost_centre()
        if 'analytic_distribution' in sale_order.order_line._fields:
            sale_order.order_line.write({'analytic_distribution': {str(analytic.id): 100}})
        line_map = {line.line_uuid: line for line in self.line_ids}
        sale_lines = {line.jakh_estimate_line_id.id: line for line in sale_order.order_line if line.jakh_estimate_line_id}
        wbs_values = []
        for item in snapshot['lines']:
            if item.get('is_group'):
                continue
            line = line_map[item['id']]
            wbs_values.append({'name': item['description'], 'code': item['code'], 'project_id': project.id, 'cost_code_id': line.cost_code_id.id, 'budget_amount': float(item.get('budget_total', item['cost_total'])), 'state': 'draft', 'jakh_estimate_line_id': line.id, 'jakh_sale_line_id': sale_lines[line.id].id})
        self.env['jakh.wbs'].create(wbs_values)
        super(JakhEstimate, self).write({'state': 'handed_over', 'sale_order_id': sale_order.id, 'project_id': project.id})
        self.env['jakh.audit.event']._append(self.company_id, 'estimate.handed_over', self, {'version_id': self.approved_version_id.id, 'sale_order_id': sale_order.id, 'project_id': project.id})
        self.message_post(body=_('Approved study handed over to draft quotation %s and project %s. WBS budgets remain draft.', sale_order.name, project.name))
        return _window('sale.order', sale_order.id, _('Draft quotation'))

    def action_open_versions(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Study versions'), 'res_model': 'jakh.estimate.version', 'view_mode': 'list,form', 'domain': [('estimate_id', '=', self.id)], 'context': {'create': False}}

    def action_boq_intake(self):
        self.ensure_one(); self._ensure_mutable()
        return {'type': 'ir.actions.act_window', 'name': _('Reviewed BOQ intake'), 'res_model': 'jakh.boq.import.batch', 'view_mode': 'form', 'target': 'current', 'context': {'default_estimate_id': self.id}}

    def action_open_quotation(self):
        self.ensure_one()
        if not self.sale_order_id:
            raise UserError(_('No handover quotation exists yet.'))
        return _window('sale.order', self.sale_order_id.id, _('Quotation'))

    def action_open_project(self):
        self.ensure_one()
        if not self.project_id:
            raise UserError(_('No handover project exists yet.'))
        return _window('project.project', self.project_id.id, _('Project'))

    @api.model
    def action_analysis(self):
        if not self.env.user.has_group(FINANCIAL):
            raise AccessError(_('Estimate amounts are restricted to financial roles.'))
        action = self.env.ref('jakh_estimate.action_estimate_analysis').read()[0]
        action['name'] = _('Estimate analysis · %s', self.env.company.currency_id.name)
        action['domain'] = [('company_id', '=', self.env.company.id), ('currency_id', '=', self.env.company.currency_id.id)]
        return action


class JakhEstimateLine(models.Model):
    _name = 'jakh.estimate.line'
    _description = 'BOQ Estimate Line'
    _inherit = ['jakh.audit.mixin']
    _jakh_audit_fields = ('code', 'name', 'quantity', 'unit_cost', 'item_markup_percent', 'parent_uuid', 'is_group', 'allocation_percent')
    _order = 'sequence, id'
    _check_company_auto = True

    estimate_id = fields.Many2one('jakh.estimate', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='estimate_id.company_id', store=True)
    currency_id = fields.Many2one(related='estimate_id.currency_id', groups=FINANCIAL)
    line_uuid = fields.Char(default=lambda self: str(uuid4()), required=True, readonly=True, copy=True)
    parent_uuid = fields.Char(string='Parent BOQ identity', copy=True)
    parent_line_id = fields.Many2one('jakh.estimate.line', string='Parent BOQ group', compute='_compute_parent_line', inverse='_inverse_parent_line', check_company=True)
    is_group = fields.Boolean(string='Parent value cap / summary', default=False)
    allocation_percent = fields.Float(string='Share of parent (%)', default=0, digits=(16, 6), groups=FINANCIAL)
    import_row_id = fields.Many2one('jakh.boq.import.row', readonly=True, copy=False, ondelete='restrict', groups=FINANCIAL)
    sequence = fields.Integer(default=10)
    code = fields.Char(required=True)
    name = fields.Char(string='Description', required=True)
    cost_code_id = fields.Many2one('jakh.cost.code', check_company=True)
    product_id = fields.Many2one('product.product', check_company=True)
    uom_id = fields.Many2one('uom.uom', string='Primary unit', required=True, default=lambda self: self.env.ref('uom.product_uom_unit'))
    quantity = fields.Float(string='Quantity per building', default=0, digits=(16, 6), required=True)
    unit_cost = fields.Float(string='Direct unit cost', default=0, digits=(16, 6), groups=FINANCIAL)
    item_markup_percent = fields.Float(string='Item markup (%)', default=0, digits=(16, 6), groups=FINANCIAL)
    resource_ids = fields.One2many('jakh.estimate.resource', 'line_id', string='Resources per primary unit', copy=True)
    secondary_uom_id = fields.Many2one('uom.uom', string='Secondary unit')
    conversion_factor = fields.Float(string='Secondary units per primary unit', default=0, digits=(16, 6))
    total_quantity = fields.Float(compute='_compute_amounts', store=True, compute_sudo=True, digits=(16, 6))
    secondary_quantity = fields.Float(compute='_compute_amounts', store=True, compute_sudo=True, digits=(16, 6))
    effective_unit_cost = fields.Float(compute='_compute_amounts', store=True, compute_sudo=True, digits=(16, 6), groups=FINANCIAL)
    cost_amount = fields.Monetary(compute='_compute_amounts', store=True, compute_sudo=True, groups=FINANCIAL)
    sell_amount = fields.Monetary(compute='_compute_amounts', store=True, compute_sudo=True, groups=FINANCIAL)
    selling_unit_rate = fields.Float(compute='_compute_amounts', store=True, compute_sudo=True, digits=(16, 6), groups=FINANCIAL)
    indirect_allocated = fields.Monetary(compute='_compute_amounts', store=True, compute_sudo=True, groups=FINANCIAL)
    budget_total = fields.Monetary(compute='_compute_amounts', store=True, compute_sudo=True, groups=FINANCIAL)

    _unique_code = models.Constraint('UNIQUE(estimate_id, code)', 'BOQ codes must be unique within the study.')
    _unique_uuid = models.Constraint('UNIQUE(estimate_id, line_uuid)', 'Stable line IDs must be unique within the study.')
    _nonnegative_quantity = models.Constraint('CHECK(quantity >= 0 AND unit_cost >= 0 AND item_markup_percent >= 0 AND item_markup_percent <= 1000)', 'Quantities and costs must be non-negative; markup cannot exceed 1,000 percent.')

    @api.depends('quantity', 'unit_cost', 'item_markup_percent', 'resource_ids.quantity', 'resource_ids.unit_cost', 'resource_ids.name', 'uom_id', 'secondary_uom_id', 'conversion_factor', 'estimate_id.unit_count', 'estimate_id.header_markup_percent', 'estimate_id.indirect_amount', 'estimate_id.overhead_percent', 'estimate_id.profit_percent', 'estimate_id.line_ids.parent_uuid', 'estimate_id.line_ids.is_group', 'estimate_id.line_ids.allocation_percent', 'estimate_id.line_ids.quantity', 'estimate_id.line_ids.unit_cost', 'estimate_id.line_ids.item_markup_percent', 'estimate_id.line_ids.resource_ids.quantity', 'estimate_id.line_ids.resource_ids.unit_cost', 'estimate_id.building_ids.number', 'estimate_id.building_ids.override_ids.line_id', 'estimate_id.building_ids.override_ids.quantity', 'name', 'code')
    def _compute_amounts(self):
        for estimate in self.mapped('estimate_id'):
            calculations = {line['id']: line for line in estimate._calculate(strict=False)['lines']}
            for line in self.filtered(lambda record: record.estimate_id == estimate):
                value = calculations.get(line.line_uuid, {})
                line.total_quantity = float(value.get('total_quantity', 0))
                line.secondary_quantity = float(value.get('secondary_quantity', 0))
                line.effective_unit_cost = float(value.get('unit_cost', 0))
                line.selling_unit_rate = float(value.get('selling_unit_rate', 0))
                line.cost_amount = float(value.get('cost_total', 0))
                line.sell_amount = float(value.get('sell_total', 0))
                line.indirect_allocated = float(value.get('indirect_allocated', 0))
                line.budget_total = float(value.get('budget_total', 0))

    @api.model_create_multi
    def create(self, values_list):
        protected = {'total_quantity', 'secondary_quantity', 'effective_unit_cost', 'cost_amount', 'sell_amount', 'selling_unit_rate', 'indirect_allocated', 'budget_total'}
        for values in values_list:
            _validate_numbers(self.env, values, {'quantity': MAX_QUANTITY, 'unit_cost': MAX_QUANTITY, 'allocation_percent': 100, 'item_markup_percent': 1000})
        if any(protected.intersection(values) for values in values_list):
            raise AccessError(_('Calculated line amounts are assigned by the costing service.'))
        if any(values.get('import_row_id') for values in values_list):
            raise AccessError(_('Imported source references are assigned by reviewed intake.'))
        self.env['jakh.estimate'].browse([values['estimate_id'] for values in values_list])._ensure_mutable()
        return super().create(values_list)

    @api.model
    def _create_from_intake(self, values_list, batch):
        batch._locked()
        if batch.state != 'reviewed' or batch.error_count:
            raise AccessError(_('Only a clean reviewed intake can create source-linked BOQ lines.'))
        for values in values_list:
            source = self.env['jakh.boq.import.row'].browse(values['import_row_id'])
            if source.batch_id != batch or values['estimate_id'] != batch.estimate_id.id or values['line_uuid'] != source.line_uuid:
                raise ValidationError(_('BOQ source identity must match its reviewed workbook batch.'))
        return super().create(values_list)

    @api.depends('parent_uuid', 'estimate_id.line_ids.line_uuid')
    def _compute_parent_line(self):
        for line in self:
            line.parent_line_id = line.estimate_id.line_ids.filtered(lambda row: row.line_uuid == line.parent_uuid)[:1]

    def _inverse_parent_line(self):
        for line in self:
            parent = line.parent_line_id
            if parent and (parent.estimate_id != line.estimate_id or not parent.is_group):
                raise ValidationError(_('Select a parent BOQ group in the same estimate.'))
            line.write({'parent_uuid': parent.line_uuid if parent else False})

    def write(self, values):
        self.mapped('estimate_id')._ensure_mutable()
        _validate_numbers(self.env, values, {'quantity': MAX_QUANTITY, 'unit_cost': MAX_QUANTITY, 'allocation_percent': 100, 'item_markup_percent': 1000})
        if {'estimate_id', 'line_uuid', 'import_row_id', 'total_quantity', 'secondary_quantity', 'effective_unit_cost', 'cost_amount', 'sell_amount', 'selling_unit_rate', 'indirect_allocated', 'budget_total'}.intersection(values):
            raise AccessError(_('The line identity, parent and calculated amounts are controlled.'))
        return super().write(values)

    @api.constrains('parent_uuid', 'is_group', 'allocation_percent', 'quantity', 'unit_cost', 'item_markup_percent')
    def _check_allocation(self):
        for estimate in self.mapped('estimate_id'):
            estimate._calculate()

    def unlink(self):
        self.mapped('estimate_id')._ensure_mutable()
        if any(row.estimate_id.line_ids.filtered(lambda child: child.parent_uuid == row.line_uuid and child not in self) for row in self):
            raise UserError(_('Remove or reassign child BOQ allocations before deleting a parent group.'))
        return super().unlink()


class JakhEstimateResource(models.Model):
    _name = 'jakh.estimate.resource'
    _inherit = ['jakh.audit.mixin']
    _jakh_audit_fields = ('name', 'resource_type', 'uom_id', 'quantity', 'unit_cost')
    _description = 'BOQ Resource Assembly'
    _check_company_auto = True

    line_id = fields.Many2one('jakh.estimate.line', required=True, ondelete='cascade', index=True)
    estimate_id = fields.Many2one(related='line_id.estimate_id', store=True)
    company_id = fields.Many2one(related='line_id.company_id', store=True)
    currency_id = fields.Many2one(related='line_id.currency_id', groups=FINANCIAL)
    resource_type = fields.Selection([(key, label) for key, label in [('material', 'Material'), ('labour', 'Labour'), ('equipment', 'Equipment'), ('subcontract', 'Subcontract'), ('other', 'Other')]], required=True, default='material')
    name = fields.Char(string='Description', required=True)
    quantity = fields.Float(default=0, digits=(16, 6), required=True)
    unit_cost = fields.Float(default=0, digits=(16, 6), groups=FINANCIAL, required=True)
    uom_id = fields.Many2one('uom.uom', string='Resource unit')

    _positive_inputs = models.Constraint('CHECK(quantity >= 0 AND unit_cost >= 0)', 'Resource quantities and costs must be non-negative.')

    @api.model_create_multi
    def create(self, values_list):
        self.env['jakh.estimate.line'].browse([values['line_id'] for values in values_list]).mapped('estimate_id')._ensure_mutable()
        for values in values_list:
            _validate_numbers(self.env, values, {'quantity': MAX_QUANTITY, 'unit_cost': MAX_QUANTITY})
        return super().create(values_list)

    def write(self, values):
        self.mapped('estimate_id')._ensure_mutable()
        _validate_numbers(self.env, values, {'quantity': MAX_QUANTITY, 'unit_cost': MAX_QUANTITY})
        if 'line_id' in values:
            raise AccessError(_('A resource cannot be moved to another BOQ line.'))
        return super().write(values)

    def unlink(self):
        self.mapped('estimate_id')._ensure_mutable()
        return super().unlink()


class JakhEstimateBuilding(models.Model):
    _name = 'jakh.estimate.building'
    _description = 'Repeated Building Quantity Basis'
    _order = 'number, id'
    _check_company_auto = True

    estimate_id = fields.Many2one('jakh.estimate', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='estimate_id.company_id', store=True)
    number = fields.Integer(string='Building number', required=True, default=1)
    name = fields.Char(required=True)
    override_ids = fields.One2many('jakh.estimate.quantity.override', 'building_id', copy=True)

    _unique_number = models.Constraint('UNIQUE(estimate_id, number)', 'Each building number must be unique within the estimate.')

    @api.constrains('number', 'estimate_id')
    def _check_number(self):
        if any(not 1 <= building.number <= building.estimate_id.unit_count for building in self):
            raise ValidationError(_('Building numbers must be within the repeated-building count.'))

    @api.model_create_multi
    def create(self, values_list):
        self.env['jakh.estimate'].browse([values['estimate_id'] for values in values_list])._ensure_mutable()
        return super().create(values_list)

    def write(self, values):
        self.mapped('estimate_id')._ensure_mutable()
        if 'estimate_id' in values:
            raise AccessError(_('A building quantity basis cannot move between estimates.'))
        return super().write(values)

    def unlink(self):
        self.mapped('estimate_id')._ensure_mutable()
        return super().unlink()


class JakhEstimateQuantityOverride(models.Model):
    _name = 'jakh.estimate.quantity.override'
    _description = 'Per-Building BOQ Quantity Override'
    _check_company_auto = True

    building_id = fields.Many2one('jakh.estimate.building', required=True, ondelete='cascade', index=True)
    estimate_id = fields.Many2one(related='building_id.estimate_id', store=True)
    company_id = fields.Many2one(related='building_id.company_id', store=True)
    line_id = fields.Many2one('jakh.estimate.line', required=True, ondelete='cascade', check_company=True, domain="[('estimate_id', '=', estimate_id)]")
    quantity = fields.Float(string='Quantity for this building', default=0, digits=(16, 6), required=True)

    _unique_line = models.Constraint('UNIQUE(building_id, line_id)', 'A building can override a BOQ line only once.')
    _nonnegative_quantity = models.Constraint('CHECK(quantity >= 0)', 'Override quantities cannot be negative.')

    @api.constrains('line_id', 'building_id')
    def _check_same_estimate(self):
        if any(override.line_id.estimate_id != override.estimate_id for override in self):
            raise ValidationError(_('Override quantities must use a BOQ line from the same estimate.'))

    @api.model_create_multi
    def create(self, values_list):
        self.env['jakh.estimate.building'].browse([values['building_id'] for values in values_list]).mapped('estimate_id')._ensure_mutable()
        return super().create(values_list)

    def write(self, values):
        self.mapped('estimate_id')._ensure_mutable()
        if 'building_id' in values:
            raise AccessError(_('An override cannot move between buildings.'))
        return super().write(values)

    def unlink(self):
        self.mapped('estimate_id')._ensure_mutable()
        return super().unlink()


class JakhEstimateVersion(models.Model):
    _name = 'jakh.estimate.version'
    _description = 'Immutable BOQ Study Version'
    _order = 'estimate_id, number desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True)
    number = fields.Integer(required=True, readonly=True)
    estimate_id = fields.Many2one('jakh.estimate', required=True, readonly=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='estimate_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True, groups=FINANCIAL)
    snapshot_json = fields.Json(required=True, readonly=True, groups=FINANCIAL)
    comparison_json = fields.Json(readonly=True, groups=FINANCIAL)
    snapshot_text = fields.Text(compute='_compute_display', groups=FINANCIAL)
    comparison_text = fields.Text(compute='_compute_display', groups=FINANCIAL)
    added_count = fields.Integer(readonly=True)
    removed_count = fields.Integer(readonly=True)
    modified_count = fields.Integer(readonly=True)
    cost_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    sell_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    created_by_id = fields.Many2one('res.users', required=True, readonly=True)

    _unique_number = models.Constraint('UNIQUE(estimate_id, number)', 'Version numbers must be unique within the estimate.')

    @api.depends('snapshot_json', 'comparison_json')
    def _compute_display(self):
        for version in self:
            version.snapshot_text = json.dumps(version.snapshot_json, ensure_ascii=False, indent=2)
            comparison = version.comparison_json or {}
            summary = []
            for kind in ('added', 'removed'):
                for line in comparison.get(kind, []):
                    summary.append(f"{kind.title()}: {line['code']} · {line['description']}")
            for change in comparison.get('modified', []):
                summary.append(f"Modified: {change['after']['code']} · {', '.join(change['changed_fields'])}")
                for key in change['changed_fields']:
                    summary.append(f"  {key}: {change['before'].get(key)} → {change['after'].get(key)}")
            for key, change in comparison.get('headers', {}).items():
                summary.append(f"{key}: {change['before']} → {change['after']}")
            if comparison.get('sell_delta') is not None:
                summary.append(f"Selling amount change: {version.currency_id.name} {comparison['sell_delta']}")
            version.comparison_text = '\n'.join(summary) or 'No differences from the preceding saved study.'

    @api.model
    def _create_snapshot(self, estimate, label, snapshot, comparison):
        estimate._check_editor()
        return super(JakhEstimateVersion, self).create({'name': label, 'number': len(estimate.version_ids) + 1, 'estimate_id': estimate.id, 'currency_id': estimate.currency_id.id, 'snapshot_json': snapshot, 'comparison_json': comparison, 'added_count': len(comparison.get('added', [])), 'removed_count': len(comparison.get('removed', [])), 'modified_count': len(comparison.get('modified', [])), 'cost_amount': float(snapshot['cost_total']), 'sell_amount': float(snapshot['sell_total']), 'created_by_id': self.env.uid})

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Use the estimate action to create a calculated version.'))

    def write(self, values):
        raise AccessError(_('Saved estimate versions are immutable.'))

    def unlink(self):
        raise AccessError(_('Saved estimate versions are retained as audit evidence.'))


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    jakh_estimate_id = fields.Many2one('jakh.estimate', readonly=True, copy=False, check_company=True, ondelete='restrict', index=True)
    _unique_jakh_estimate = models.Constraint('UNIQUE(jakh_estimate_id)', 'An approved estimate can create only one handover quotation.')

    @api.model
    def _create_jakh_from_estimate(self, estimate, snapshot):
        estimate._check_manager()
        pricelist = self.env['product.pricelist'].search([('currency_id', '=', estimate.currency_id.id), ('company_id', 'in', [False, estimate.company_id.id])], order='company_id desc, id', limit=1)
        if not pricelist:
            raise UserError(_('Configure a Sales pricelist in the estimate currency before handover.'))
        originals = {line.line_uuid: line for line in estimate.line_ids}
        lines = []
        for item in snapshot['lines']:
            if item.get('is_group'):
                continue
            original = originals[item['id']]
            lines.append(Command.create({'product_id': original.product_id.id, 'name': f"{item['code']} · {item['description']}", 'product_uom_id': original.uom_id.id, 'product_uom_qty': float(item['total_quantity']), 'price_unit': float(item['selling_unit_rate']), 'jakh_estimate_line_id': original.id}))
        quotation = super(SaleOrder, self).create({'partner_id': estimate.partner_id.id, 'company_id': estimate.company_id.id, 'pricelist_id': pricelist.id, 'opportunity_id': estimate.opportunity_id.id or False, 'origin': estimate.name, 'jakh_estimate_id': estimate.id, 'order_line': lines})
        approved_amounts = {originals[item['id']].id: float(item['sell_total']) for item in snapshot['lines'] if not item.get('is_group')}
        if any(estimate.currency_id.compare_amounts(line.price_subtotal, approved_amounts[line.jakh_estimate_line_id.id]) for line in quotation.order_line if line.jakh_estimate_line_id):
            raise UserError(_('Native Sales rounding or price-included taxes differ from the approved study. Review unit/price precision and tax settings before handover.'))
        return quotation

    @api.model_create_multi
    def create(self, values_list):
        if any(values.get('jakh_estimate_id') for values in values_list):
            raise AccessError(_('Create an estimate-linked quotation through the approved estimate handover.'))
        return super().create(values_list)

    def write(self, values):
        if 'jakh_estimate_id' in values:
            raise AccessError(_('The approved estimate reference cannot be reassigned.'))
        return super().write(values)

    def unlink(self):
        if any(order.jakh_estimate_id for order in self):
            raise UserError(_('Retain estimate-linked quotations; use the native cancellation workflow.'))
        return super().unlink()


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    jakh_estimate_line_id = fields.Many2one('jakh.estimate.line', readonly=True, copy=False, ondelete='restrict', check_company=True)
    _unique_jakh_source = models.Constraint('UNIQUE(order_id, jakh_estimate_line_id)', 'A handover quotation can reference each source BOQ line only once.')

    @api.constrains('jakh_estimate_line_id', 'order_id')
    def _check_jakh_source(self):
        if any(line.jakh_estimate_line_id and line.jakh_estimate_line_id.estimate_id != line.order_id.jakh_estimate_id for line in self):
            raise ValidationError(_('The source BOQ line must belong to the quotation’s approved estimate.'))

    def write(self, values):
        if 'jakh_estimate_line_id' in values:
            raise AccessError(_('The source BOQ line reference is immutable.'))
        return super().write(values)


class ProjectProject(models.Model):
    _inherit = 'project.project'

    jakh_estimate_id = fields.Many2one('jakh.estimate', readonly=True, copy=False, check_company=True, ondelete='restrict', index=True)
    _unique_jakh_estimate = models.Constraint('UNIQUE(jakh_estimate_id)', 'An approved estimate can create only one handover project.')

    @api.model
    def _create_jakh_from_estimate(self, estimate):
        estimate._check_manager()
        return super(ProjectProject, self).create({'name': estimate.title, 'partner_id': estimate.partner_id.id, 'company_id': estimate.company_id.id, 'user_id': estimate.project_manager_id.id or self.env.uid, 'jakh_estimate_id': estimate.id})

    @api.model_create_multi
    def create(self, values_list):
        if any(values.get('jakh_estimate_id') for values in values_list):
            raise AccessError(_('Create an estimate-linked project through the approved estimate handover.'))
        return super().create(values_list)

    def write(self, values):
        if 'jakh_estimate_id' in values:
            raise AccessError(_('The approved estimate reference cannot be reassigned.'))
        return super().write(values)

    def unlink(self):
        if any(project.jakh_estimate_id for project in self):
            raise UserError(_('Retain estimate-linked projects; archive them when the project is closed.'))
        return super().unlink()


class JakhWbs(models.Model):
    _inherit = 'jakh.wbs'

    jakh_estimate_line_id = fields.Many2one('jakh.estimate.line', readonly=True, copy=False, check_company=True, ondelete='restrict')
    jakh_sale_line_id = fields.Many2one('sale.order.line', readonly=True, copy=False, check_company=True, ondelete='restrict')

    @api.constrains('project_id', 'jakh_estimate_line_id', 'jakh_sale_line_id')
    def _check_jakh_source(self):
        for wbs in self:
            if wbs.jakh_estimate_line_id and wbs.jakh_estimate_line_id.estimate_id != wbs.project_id.jakh_estimate_id:
                raise ValidationError(_('The source BOQ line must belong to this project’s approved estimate.'))
            if wbs.jakh_sale_line_id and wbs.jakh_sale_line_id.jakh_estimate_line_id != wbs.jakh_estimate_line_id:
                raise ValidationError(_('The WBS quotation line and source BOQ line must match.'))

    def write(self, values):
        if {'jakh_estimate_line_id', 'jakh_sale_line_id'}.intersection(values):
            raise AccessError(_('The WBS source handover references cannot be reassigned.'))
        return super().write(values)
