from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class AssuranceCase(models.Model):
    _name = 'jakh.assurance.case'
    _description = 'Quality / HSE Action'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'due_date, id desc'

    name = fields.Char(required=True, tracking=True)
    kind = fields.Selection([
        ('inspection', 'Inspection'), ('ncr', 'Non-conformance'),
        ('permit', 'Permit to Work'), ('incident', 'Incident'),
        ('risk', 'Risk Assessment'), ('corrective', 'Corrective Action'),
    ], required=True, default='inspection', tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True, index=True)
    wbs_id = fields.Many2one('jakh.wbs', check_company=True, ondelete='restrict')
    responsible_id = fields.Many2one('res.users', required=True, default=lambda s: s.env.user, tracking=True)
    due_date = fields.Date(required=True, tracking=True)
    permit_start = fields.Datetime()
    permit_end = fields.Datetime()
    severity = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], required=True, default='medium', tracking=True)
    description = fields.Html(required=True, sanitize=True)
    resolution = fields.Html(sanitize=True)
    authority_reference = fields.Char(help='Project-approved authority/code reference; not a compliance certification.')
    attachment_ids = fields.One2many('ir.attachment', 'res_id', domain=[('res_model', '=', 'jakh.assurance.case')], string='Evidence')
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Awaiting Review'), ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'), ('closed', 'Verified / Closed'), ('cancelled', 'Cancelled'),
    ], required=True, default='draft', readonly=True, tracking=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    resolved_at = fields.Datetime(readonly=True, copy=False)
    closed_at = fields.Datetime(readonly=True, copy=False)
    verified_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    is_overdue = fields.Boolean(compute='_compute_overdue', search='_search_overdue')

    @api.depends('due_date', 'state')
    def _compute_overdue(self):
        today = fields.Date.context_today(self)
        for row in self:
            row.is_overdue = row.state not in ('closed', 'cancelled') and row.due_date < today

    @api.model
    def _search_overdue(self, operator, value):
        positive = (operator in ('=', 'in') and value in (True, [True])) or (operator in ('!=', 'not in') and value in (False, [False]))
        domain = [('state', 'not in', ['closed', 'cancelled']), ('due_date', '<', fields.Date.context_today(self))]
        return domain if positive else ['!', '&', *domain]

    @api.constrains('project_id', 'company_id', 'wbs_id', 'permit_start', 'permit_end', 'kind')
    def _check_links(self):
        for row in self:
            if not row.company_id:
                raise ValidationError(self.env._('The project must have a legal entity.'))
            if row.wbs_id and row.wbs_id.project_id != row.project_id:
                raise ValidationError(self.env._('WBS must belong to the selected project.'))
            if row.kind == 'permit' and row.permit_start and row.permit_end and row.permit_start >= row.permit_end:
                raise ValidationError(self.env._('Permit end must be later than its start.'))

    @api.model_create_multi
    def create(self, vals_list):
        protected = {'submitted_at', 'resolved_at', 'closed_at', 'verified_by_id'}
        if any(vals.get('state', 'draft') != 'draft' or set(vals) & protected for vals in vals_list):
            raise AccessError(self.env._('Create draft actions; workflow timestamps are server-controlled.'))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) & {'state', 'submitted_at', 'resolved_at', 'closed_at', 'verified_by_id'}:
            raise AccessError(self.env._('Use the Quality/HSE workflow actions.'))
        if any(row.state in ('closed', 'cancelled') for row in self):
            raise ValidationError(self.env._('Closed or cancelled actions are immutable. Create a linked follow-up instead.'))
        if set(vals) & {'project_id', 'wbs_id', 'kind', 'permit_start', 'permit_end'} and any(row.state != 'draft' for row in self):
            raise ValidationError(self.env._('Submitted action scope is locked; create a new action for a changed work scope.'))
        return super().write(vals)

    def unlink(self):
        if any(row.state != 'draft' for row in self):
            raise ValidationError(self.env._('Submitted actions cannot be deleted.'))
        return super().unlink()

    def _require_reviewer(self):
        if not (self.env.user.has_group('jakh_core.group_quality') or self.env.user.has_group('jakh_core.group_manager')):
            raise AccessError(self.env._('Quality/HSE reviewer access is required.'))

    def _transition(self, expected, state, values=None):
        self.ensure_one()
        self.check_access('write')
        # Serialize transitions so a second approval cannot silently overwrite the first.
        self.env.cr.execute('SELECT id FROM jakh_assurance_case WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset(['state'])
        if self.state not in expected:
            raise ValidationError(self.env._('This action is not valid in the current workflow state.'))
        super(AssuranceCase, self).write({'state': state, **(values or {})})
        self.env['jakh.audit.event']._append(self.company_id, 'assurance.' + state, self, {'kind': self.kind})
        return True

    def action_submit(self):
        self.ensure_one()
        if self.kind == 'permit' and (not self.permit_start or not self.permit_end):
            raise ValidationError(self.env._('A permit requires a start and end time.'))
        return self._transition(('draft',), 'submitted', {'submitted_at': fields.Datetime.now()})

    def action_start(self):
        self._require_reviewer()
        self.ensure_one()
        if self.kind == 'permit' and self.permit_end <= fields.Datetime.now():
            raise ValidationError(self.env._('An expired permit cannot be started.'))
        return self._transition(('submitted',), 'in_progress')

    def action_resolve(self):
        self.ensure_one()
        if not self.resolution:
            raise ValidationError(self.env._('Record the inspection result or corrective action before resolving.'))
        return self._transition(('in_progress',), 'resolved', {'resolved_at': fields.Datetime.now()})

    def action_close(self):
        self._require_reviewer()
        self.ensure_one()
        if self.kind in ('ncr', 'incident', 'corrective') and self.create_uid == self.env.user:
            raise ValidationError(self.env._('An independent reviewer must verify this action.'))
        if not self.env['ir.attachment'].search_count([('res_model', '=', self._name), ('res_id', '=', self.id)]):
            raise ValidationError(self.env._('Attach verification evidence before closing.'))
        return self._transition(('resolved',), 'closed', {'closed_at': fields.Datetime.now(), 'verified_by_id': self.env.uid})

    def action_cancel(self):
        self._require_reviewer()
        return self._transition(('draft', 'submitted'), 'cancelled')
