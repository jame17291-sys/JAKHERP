from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class Document(models.Model):
    _name = 'jakh.document'
    _description = 'Controlled Project Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'code, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True, index=True)
    wbs_id = fields.Many2one('jakh.wbs', check_company=True, ondelete='restrict')
    kind = fields.Selection([
        ('drawing', 'Drawing'), ('submittal', 'Submittal'), ('specification', 'Specification'),
        ('method', 'Method Statement'), ('itp', 'Inspection / Test Plan'), ('correspondence', 'Correspondence'),
    ], required=True, default='drawing')
    jurisdiction = fields.Selection([('sa', 'Saudi Arabia'), ('ae', 'United Arab Emirates')])
    authority = fields.Char()
    standard_edition = fields.Char()
    effective_date = fields.Date()
    revision_ids = fields.One2many('jakh.document.revision', 'document_id', string='Controlled Revisions')

    _code_unique = models.Constraint('UNIQUE(project_id, code)', 'Document code must be unique within its project.')

    @api.constrains('project_id', 'company_id', 'wbs_id')
    def _check_links(self):
        for row in self:
            if not row.company_id or (row.wbs_id and row.wbs_id.project_id != row.project_id):
                raise ValidationError(self.env._('Document, legal entity and WBS must belong to the same project.'))

    def write(self, vals):
        if set(vals) & {'code', 'project_id', 'kind', 'jurisdiction', 'authority', 'standard_edition'} and self.revision_ids.filtered(lambda r: r.state == 'approved'):
            raise ValidationError(self.env._('Approved document identity is locked; create a new document for a changed identity.'))
        return super().write(vals)

    def unlink(self):
        if self.revision_ids.filtered(lambda r: r.state != 'draft'):
            raise ValidationError(self.env._('Documents with submitted revisions cannot be deleted.'))
        return super().unlink()


class DocumentRevision(models.Model):
    _name = 'jakh.document.revision'
    _description = 'Controlled Document Revision'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'submitted_at desc, id desc'

    name = fields.Char(string='Revision', required=True)
    document_id = fields.Many2one('jakh.document', required=True, check_company=True, ondelete='restrict')
    project_id = fields.Many2one(related='document_id.project_id', store=True, readonly=True)
    company_id = fields.Many2one(related='document_id.company_id', store=True, readonly=True, index=True)
    summary = fields.Text(required=True)
    reviewer_id = fields.Many2one('res.users', required=True)
    due_date = fields.Date(required=True)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Awaiting Review'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='draft', required=True, readonly=True, tracking=True, copy=False)
    attachment_ids = fields.One2many('ir.attachment', 'res_id', domain=[('res_model', '=', 'jakh.document.revision')], string='Files')
    cycle_days = fields.Float(compute='_compute_cycle', store=True, aggregator='avg')

    _revision_unique = models.Constraint('UNIQUE(document_id, name)', 'Revision must be unique within a document.')

    @api.depends('submitted_at', 'completed_at')
    def _compute_cycle(self):
        for row in self:
            row.cycle_days = (row.completed_at - row.submitted_at).total_seconds() / 86400 if row.completed_at and row.submitted_at else 0

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('state', 'draft') != 'draft' or set(vals) & {'submitted_at', 'completed_at', 'approved_by_id'} for vals in vals_list):
            raise AccessError(self.env._('Create a draft revision; lifecycle values are server-controlled.'))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) & {'state', 'submitted_at', 'completed_at', 'approved_by_id'}:
            raise AccessError(self.env._('Use document revision actions.'))
        if any(row.state != 'draft' for row in self):
            raise ValidationError(self.env._('Submitted revisions are locked. Create the next revision instead.'))
        return super().write(vals)

    def unlink(self):
        if any(row.state != 'draft' for row in self):
            raise ValidationError(self.env._('Submitted revisions cannot be deleted.'))
        return super().unlink()

    def _transition(self, expected, state, values):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('SELECT id FROM jakh_document_revision WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset(['state'])
        if self.state not in expected:
            raise ValidationError(self.env._('Revision state changed. Refresh before continuing.'))
        super(DocumentRevision, self).write({'state': state, **values})
        self.env['jakh.audit.event']._append(self.company_id, 'document.' + state, self, {'revision': self.name})
        return True

    def action_submit(self):
        self.ensure_one()
        if not self.env['ir.attachment'].search_count([('res_model', '=', self._name), ('res_id', '=', self.id)]):
            raise ValidationError(self.env._('Attach the document before submitting this revision.'))
        if self.reviewer_id == self.env.user:
            raise ValidationError(self.env._('Assign an independent reviewer.'))
        return self._transition(('draft',), 'submitted', {'submitted_at': fields.Datetime.now()})

    def _require_reviewer(self):
        self.ensure_one()
        if self.env.user != self.reviewer_id and not self.env.user.has_group('jakh_core.group_manager'):
            raise AccessError(self.env._('Only the assigned reviewer or construction manager may review.'))
        if self.create_uid == self.env.user:
            raise ValidationError(self.env._('The revision author cannot approve their own submission.'))

    def action_approve(self):
        self._require_reviewer()
        return self._transition(('submitted',), 'approved', {'completed_at': fields.Datetime.now(), 'approved_by_id': self.env.uid})

    def action_reject(self):
        self._require_reviewer()
        return self._transition(('submitted',), 'rejected', {'completed_at': fields.Datetime.now()})


class Attachment(models.Model):
    _inherit = 'ir.attachment'

    def _jakh_lock_resource(self, resource):
        resource.check_access('write')
        resource.flush_recordset()
        # The table comes only from the two fixed controlled model types below.
        self.env.cr.execute('SELECT id FROM "%s" WHERE id = ANY(%%s) FOR UPDATE' % resource._table, [resource.ids])
        self.env.cr.execute('UPDATE "%s" SET id=id WHERE id = ANY(%%s)' % resource._table, [resource.ids])
        resource.invalidate_recordset(['state'])

    def _jakh_check_attachment_lock(self):
        for attachment in self:
            if attachment.res_model == 'jakh.document.revision' and attachment.res_id:
                revision = self.env['jakh.document.revision'].browse(attachment.res_id).exists()
                self._jakh_lock_resource(revision)
                if revision.state != 'draft':
                    raise ValidationError(self.env._('Files in submitted or approved revisions are immutable.'))
            if attachment.res_model == 'jakh.assurance.case' and attachment.res_id:
                case = self.env['jakh.assurance.case'].browse(attachment.res_id).exists()
                self._jakh_lock_resource(case)
                if case.state in ('closed', 'cancelled'):
                    raise ValidationError(self.env._('Closed verification evidence is immutable.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            model = vals.get('res_model')
            if model in ('jakh.document.revision', 'jakh.assurance.case') and vals.get('res_id'):
                resource = self.env[model].browse(vals['res_id']).exists()
                if not resource:
                    raise ValidationError(self.env._('The controlled attachment requires an existing record.'))
                self._jakh_lock_resource(resource)
                if (model == 'jakh.document.revision' and resource.state != 'draft') or (model == 'jakh.assurance.case' and resource.state in ('closed', 'cancelled')):
                    raise ValidationError(self.env._('Cannot add files to a locked record.'))
                vals['public'] = False
        return super().create(vals_list)

    def write(self, vals):
        self._jakh_check_attachment_lock()
        if vals.get('public') and any(row.res_model in ('jakh.document.revision', 'jakh.assurance.case') for row in self):
            raise AccessError(self.env._('Controlled files cannot be made public.'))
        if vals.get('res_model') in ('jakh.document.revision', 'jakh.assurance.case'):
            raise AccessError(self.env._('Attach new files from the controlled record instead of relinking files.'))
        return super().write(vals)

    def unlink(self):
        self._jakh_check_attachment_lock()
        return super().unlink()
