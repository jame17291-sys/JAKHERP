from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Document(models.Model):
    _name = 'jakh.document'
    _inherit = ['jakh.document', 'jakh.audit.mixin']
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')
    _jakh_audit_fields = ('name', 'code', 'kind', 'wbs_id', 'location_id', 'discipline_id', 'authority', 'standard_edition', 'effective_date')

    @api.constrains('location_id', 'discipline_id', 'project_id')
    def _check_context(self):
        for row in self:
            if row.location_id and row.location_id.project_id != row.project_id:
                raise ValidationError(_('The drawing location must belong to its project.'))
            if row.discipline_id and row.discipline_id.company_id != row.company_id:
                raise ValidationError(_('The drawing discipline must belong to its legal entity.'))

    def write(self, values):
        if set(values) & {'location_id', 'discipline_id', 'wbs_id'} and self.revision_ids.filtered(lambda revision: revision.state == 'approved'):
            raise ValidationError(_('Approved drawing context is immutable. Create a new controlled document instead.'))
        return super().write(values)


class Revision(models.Model):
    _name = 'jakh.document.revision'
    _inherit = ['jakh.document.revision', 'jakh.audit.mixin']
    location_id = fields.Many2one(related='document_id.location_id', store=True, readonly=True)
    discipline_id = fields.Many2one(related='document_id.discipline_id', store=True, readonly=True)
    _jakh_audit_fields = ('summary', 'reviewer_id', 'due_date', 'attachment_ids')
