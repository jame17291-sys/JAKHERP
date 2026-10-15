"""Versioned, permission-scoped construction integration inbox.

Intake creates validated draft business records.  Approvals and posting remain
the same explicit business actions used by people in the application.
"""
import hashlib
import json
import math

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import form_action, lock_records, require_role


def _without_form_defaults(record):
    """Form/RPC defaults are not part of a versioned integration payload.

    Replace only the context's default_* input keys. Keep the actual identity,
    selected companies, language, timezone and native execution/access context;
    normal model defaults still initialize protected draft workflow fields.
    """
    context = {key: value for key, value in record.env.context.items()
               if not (isinstance(key, str) and key.startswith('default_'))}
    return record.with_context(context)


class IntegrationEvent(models.Model):
    _name = 'jakh.integration.event'
    _description = 'Construction integration inbox'
    _check_company_auto = True
    _order = 'create_date desc, id desc'

    name = fields.Char(required=True, readonly=True)
    version = fields.Integer(required=True, readonly=True, default=1)
    kind = fields.Selection([('p6', 'P6 progress'), ('jisr', 'Jisr labour'), ('plant', 'Plant costs')], required=True, readonly=True)
    external_id = fields.Char(required=True, readonly=True, index=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True)
    project_id = fields.Many2one('project.project', required=True, readonly=True, check_company=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, readonly=True, check_company=True)
    payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    state = fields.Selection([('received', 'Received'), ('prepared', 'Ready for business approval'), ('failed', 'Failed')], required=True, default='received', readonly=True)
    target_model = fields.Char(readonly=True)
    target_id = fields.Integer(readonly=True)
    error_message = fields.Text(readonly=True)
    attempt_count = fields.Integer(readonly=True, default=0)
    last_attempt_at = fields.Datetime(readonly=True)
    processed_by = fields.Many2one('res.users', readonly=True)
    business_state = fields.Char(compute='_compute_business_state', string='Business status')

    _source_once = models.Constraint('UNIQUE(company_id,kind,external_id)', 'The source message identity is already in the integration inbox.')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Create integration messages through the validated intake interface.'))

    def write(self, values):
        raise AccessError(_('Integration evidence is immutable. Use the retry action for failed messages.'))

    def unlink(self):
        raise AccessError(_('Integration evidence cannot be deleted.'))

    def _compute_business_state(self):
        for row in self:
            state = ''
            if row.target_model in self.env and row.target_id:
                target = self.env[row.target_model].browse(row.target_id).exists()
                if target and target.has_access('read'):
                    state = dict(target._fields['state']._description_selection(self.env)).get(target.state, target.state)
            row.business_state = state

    @api.model
    def ingest(self, payload):
        self = _without_form_defaults(self)
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        if not isinstance(payload, dict) or set(payload) != {'version', 'kind', 'external_id', 'company_id', 'project_id', 'wbs_id', 'data'}:
            raise ValidationError(_('Use the version 1 integration envelope with explicit company, project, WBS and source identity.'))
        if payload['version'] != 1 or payload['kind'] not in ('p6', 'jisr', 'plant'):
            raise ValidationError(_('Use a supported integration version and source system.'))
        source = payload['external_id']
        if not isinstance(source, str) or not source.strip() or len(source) > 160:
            raise ValidationError(_('An integration source ID must be a non-empty value of at most 160 characters.'))
        for key in ('company_id', 'project_id', 'wbs_id'):
            if type(payload[key]) is not int or payload[key] <= 0:
                raise ValidationError(_('Integration company, project and WBS references must be valid record IDs.'))
        company = self.env['res.company'].browse(payload['company_id']).exists()
        project = self.env['project.project'].browse(payload['project_id']).exists()
        wbs = self.env['jakh.wbs'].browse(payload['wbs_id']).exists()
        company.check_access('read'); project.check_access('read'); wbs.check_access('read')
        if not company or not project or not wbs or company not in self.env.companies or project.company_id != company or wbs.project_id != project:
            raise AccessError(_('Integration references must belong to the permitted company and project.'))
        self._validate_data(payload['kind'], payload['data'])
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ValidationError(_('A construction integration message must not exceed 32 KiB.'))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        # A row-version lock on the authorized WBS serializes intake against
        # competing retries; no source payload or business record is elevated.
        lock_records(wbs, budget=True)
        existing = self.search([('company_id', '=', company.id), ('kind', '=', payload['kind']), ('external_id', '=', source)], limit=1)
        if existing:
            if existing.payload_hash != digest:
                raise ValidationError(_('This source ID was already received with different contents. Send a new revision identity.'))
            return existing._result()
        self.check_access('create')
        event = super(IntegrationEvent, self).create({
            'name': '%s / %s' % (payload['kind'].upper(), source), 'version': 1, 'kind': payload['kind'],
            'external_id': source, 'company_id': company.id, 'project_id': project.id, 'wbs_id': wbs.id,
            'payload': encoded, 'payload_hash': digest,
            'state': 'received', 'target_model': False, 'target_id': 0,
            'error_message': False, 'attempt_count': 0, 'last_attempt_at': False, 'processed_by': False,
        })
        event._process()
        return event._result()

    @api.model
    def _validate_data(self, kind, data):
        if not isinstance(data, dict):
            raise ValidationError(_('Integration data must be an object containing approved source fields.'))
        required = {'source_reference'}
        if kind == 'p6':
            required |= {'as_of_date', 'planned_value', 'earned_value', 'progress_percent'}
            allowed = required | {'forecast_finish', 'name', 'contract_line_id', 'location_id', 'discipline_id'}
        else:
            required |= {'work_date', 'resource_reference', 'quantity', 'amount'}
            allowed = required | {'unit_label', 'name', 'journal_id', 'expense_account_id', 'offset_account_id', 'effect', 'correction_of_id', 'correction_reason', 'contract_line_id', 'location_id', 'discipline_id'}
        if not required.issubset(data) or set(data) - allowed:
            raise ValidationError(_('The integration payload has missing or unsupported fields. Do not include credentials or personal payroll information.'))
        numeric = ('planned_value', 'earned_value', 'progress_percent') if kind == 'p6' else ('quantity', 'amount')
        for key in numeric:
            value = data[key]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValidationError(_('Integration amounts, quantities and progress must be finite numbers.'))
        for key, value in data.items():
            if key in ('contract_line_id', 'location_id', 'discipline_id') and (type(value) is not int or value <= 0):
                raise ValidationError(_('Integration BOQ, location and discipline references must be valid record IDs.'))
            if isinstance(value, str) and len(value) > 2000:
                raise ValidationError(_('Integration source text must not exceed 2,000 characters per field.'))

    def _process(self):
        self = _without_form_defaults(self)
        self.ensure_one(); self.check_access('write')
        if self.state == 'prepared':
            return True
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        lock_records(self, touch=True)
        data = json.loads(self.payload)['data']
        update = {'attempt_count': self.attempt_count + 1, 'last_attempt_at': fields.Datetime.now(), 'processed_by': self.env.user.id}
        try:
            with self.env.cr.savepoint():
                if self.wbs_id.state != 'released':
                    raise UserError(_('Release the project WBS budget before preparing integration evidence.'))
                values = {'name': data.get('name') or self.name, 'source_system': self.kind,
                          'external_id': self.external_id, 'project_id': self.project_id.id,
                          'wbs_id': self.wbs_id.id, **{key: value for key, value in data.items() if key != 'name'}}
                # An absent approved dimension remains unallocated, even if a
                # user has a native form default. The external JSON/hash stays
                # strict: absent IDs are not rewritten into false-valued IDs.
                for field in ('contract_line_id', 'location_id', 'discipline_id'):
                    values.setdefault(field, False)
                if self.kind == 'p6':
                    values.setdefault('forecast_finish', False)
                    target = self.env['jakh.project.schedule.progress'].create(values)
                else:
                    for field in ('correction_of_id', 'correction_reason', 'journal_id', 'expense_account_id', 'offset_account_id'):
                        values.setdefault(field, False)
                    values.setdefault('effect', 'debit')
                    values.setdefault('unit_label', 'hours')
                    values.update(actual_type='labour' if self.kind == 'jisr' else 'plant', payload_digest=self.payload_hash)
                    target = self.env['jakh.project.actual.feed'].create(values)
                target.action_submit()
                update.update(state='prepared', target_model=target._name, target_id=target.id, error_message=False)
        except (AccessError, UserError, ValidationError, ValueError) as exc:
            update.update(state='failed', error_message=str(exc), target_model=False, target_id=0)
        super(IntegrationEvent, self).write(update)
        self.env['jakh.audit.event']._append(self.company_id, 'integration.' + self.state, self, {
            'kind': self.kind, 'source': self.external_id, 'payload_hash': self.payload_hash, 'attempt': self.attempt_count,
        })
        return self.state == 'prepared'

    def _result(self):
        self.ensure_one(); self.check_access('read')
        return {'id': self.id, 'version': 1, 'state': self.state, 'kind': self.kind,
                'external_id': self.external_id, 'business_model': self.target_model or None,
                'business_id': self.target_id or None, 'error': self.error_message or None}

    def action_retry(self):
        require_role(self.env, ['group_manager', 'group_project_manager', 'group_finance'])
        for row in self:
            if row.state == 'failed':
                row._process()
        return True

    def action_open_business_record(self):
        self.ensure_one(); self.check_access('read')
        if not self.target_model or not self.target_id:
            raise UserError(_('Prepare this integration message before opening its business record.'))
        return form_action(self.env[self.target_model].browse(self.target_id).exists())
