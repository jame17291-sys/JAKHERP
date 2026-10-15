"""Reviewed external identifiers; specialist masters retain scheduling/payroll."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError
from .construction_dimensions import check_dimensions
from .procurement import lock_records, require_role
from .execution_controls import _has_server_defaults
from .integration_workflow import _without_form_defaults

_MAPPING_REVISION = object()


class IntegrationBinding(models.Model):
    _name = 'jakh.integration.binding'
    _description = 'Approved P6 / Jisr / plant source mapping'
    _inherit = ['mail.thread']
    _check_company_auto = True
    name = fields.Char(required=True)
    kind = fields.Selection([('p6', 'P6'), ('jisr', 'Jisr'), ('plant', 'Plant')], required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    source_project_ref = fields.Char(required=True, index=True)
    source_wbs_ref = fields.Char(required=True, index=True)
    revision = fields.Integer(required=True, default=1, readonly=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict')
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, ondelete='restrict')
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('retired', 'Retired')], default='draft', readonly=True, required=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    _unique_source = models.Constraint('UNIQUE(company_id,kind,source_project_ref,source_wbs_ref,revision)',
        'A source mapping revision must be unique.')
    _one_approved = models.UniqueIndex("(company_id,kind,source_project_ref,source_wbs_ref) WHERE state = 'approved'",
        'Only one source mapping revision can be approved at a time.')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if (_has_server_defaults(self.env, {'state', 'approved_by'})
                or any(set(values) & {'state', 'approved_by'} for values in values_list)):
            raise AccessError(_('Create draft source mappings and approve them explicitly.'))
        if self.env.context.get('_jakh_source_revision') is not _MAPPING_REVISION and (
                any('revision' in values for values in values_list) or self.env.context.get('default_revision') not in (None, 1)):
            raise AccessError(_('Mapping revision numbers are assigned by the controlled revision action.'))
        return super().create(values_list)

    def write(self, values):
        lock_records(self, touch=True)
        if set(values) & {'state', 'approved_by', 'revision'} or self.filtered(lambda row: row.state != 'draft'):
            raise AccessError(_('Approved source mappings are immutable. Retire and create a new reviewed reference.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Retain approved source mappings for integration provenance.'))
        return super().unlink()

    @api.constrains('project_id', 'wbs_id', 'company_id', 'contract_line_id', 'location_id', 'discipline_id')
    def _check_scope(self):
        for row in self:
            if row.company_id != row.project_id.company_id:
                raise ValidationError(_('An integration mapping must use its project legal entity.'))
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for row in self:
            lock_records(row); row._check_scope(); row.project_id._jakh_assert_open()
            if row.state == 'approved':
                continue
            if row.state != 'draft' or row.wbs_id.state != 'released':
                raise UserError(_('Approve a draft source mapping against a released WBS.'))
            super(IntegrationBinding, row).write({'state': 'approved', 'approved_by': self.env.uid})
            self.env['jakh.audit.event']._append(row.company_id, 'integration.mapping_approved', row,
                {'source_project': row.source_project_ref, 'source_wbs': row.source_wbs_ref,
                 'project_id': row.project_id.id, 'wbs_id': row.wbs_id.id, 'location_id': row.location_id.id})
        return True

    def action_retire(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            super(IntegrationBinding, row).write({'state': 'retired'})
            self.env['jakh.audit.event']._append(row.company_id, 'integration.mapping_retired', row)
        return True

    def action_new_revision(self):
        self.ensure_one()
        require_role(self.env, ['group_manager', 'group_finance'])
        lock_records(self, touch=True)
        if self.state != 'retired':
            raise UserError(_('Retire the old mapping before preparing a revised execution scope.'))
        revisions = self.search([('company_id', '=', self.company_id.id), ('kind', '=', self.kind),
            ('source_project_ref', '=', self.source_project_ref), ('source_wbs_ref', '=', self.source_wbs_ref)])
        record = self.with_context(_jakh_source_revision=_MAPPING_REVISION).create({name: self[name].id if self._fields[name].type == 'many2one' else self[name]
            for name in ('name', 'kind', 'company_id', 'source_project_ref', 'source_wbs_ref', 'project_id',
                         'wbs_id', 'contract_line_id', 'location_id', 'discipline_id') } | {'revision': max(revisions.mapped('revision')) + 1})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': record.id,
            'view_mode': 'form', 'views': [(False, 'form')], 'target': 'current'}


class IntegrationEvent(models.Model):
    _inherit = 'jakh.integration.event'

    @api.model
    def ingest_source(self, payload):
        self = _without_form_defaults(self)
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        required = {'version', 'kind', 'external_id', 'company_id', 'source_project_ref', 'source_wbs_ref', 'data'}
        if not isinstance(payload, dict) or set(payload) != required or type(payload['version']) is not int or payload['version'] != 1:
            raise ValidationError(_('Use the version 1 source envelope with reviewed external project and WBS references.'))
        if type(payload['company_id']) is not int or payload['company_id'] not in self.env.companies.ids:
            raise AccessError(_('Source integration must use a selected legal entity.'))
        if payload['kind'] not in ('p6', 'jisr', 'plant') or not isinstance(payload['data'], dict):
            raise ValidationError(_('Use a supported specialist source and an approved data object.'))
        for name in ('source_project_ref', 'source_wbs_ref'):
            if not isinstance(payload[name], str) or not payload[name].strip() or len(payload[name]) > 160:
                raise ValidationError(_('External project and WBS references must be non-empty and at most 160 characters.'))
        if set(payload['data']) & {'project_id', 'company_id', 'wbs_id', 'contract_line_id', 'location_id', 'discipline_id'}:
            raise ValidationError(_('Source messages cannot override their approved execution mapping.'))
        binding = self.env['jakh.integration.binding'].search([
            ('company_id', '=', payload['company_id']), ('kind', '=', payload['kind']),
            ('source_project_ref', '=', payload['source_project_ref']), ('source_wbs_ref', '=', payload['source_wbs_ref']),
            ('state', '=', 'approved')], limit=1)
        if not binding:
            raise ValidationError(_('Approve the source project/WBS mapping before accepting specialist data.'))
        lock_records(binding, budget=True); binding._check_scope()
        data = dict(payload['data'])
        for name in ('contract_line_id', 'location_id', 'discipline_id'):
            if binding[name]:
                data[name] = binding[name].id
        # Missing mapping dimensions remain absent in the strict versioned
        # envelope. Intake seals those absences in the target and strips all
        # caller form defaults before any business/audit record is created.
        return self.ingest({'version': 1, 'kind': payload['kind'], 'external_id': payload['external_id'],
            'company_id': binding.company_id.id, 'project_id': binding.project_id.id, 'wbs_id': binding.wbs_id.id, 'data': data})
