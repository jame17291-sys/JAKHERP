from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .estimate import EDITORS, FINANCIAL, _has_role


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    jakh_source_system = fields.Char(string='Original CRM system', readonly=True, copy=False)
    jakh_source_id = fields.Char(string='Original CRM reference', readonly=True, copy=False, index=True)
    jakh_source_currency_id = fields.Many2one('res.currency', string='Original CRM currency', readonly=True, copy=False, groups=FINANCIAL)
    jakh_source_amount = fields.Monetary(string='Original CRM amount', currency_field='jakh_source_currency_id', readonly=True, copy=False, groups=FINANCIAL)
    jakh_source_payload = fields.Json(string='Original CRM fields', readonly=True, copy=False, groups=FINANCIAL)
    jakh_estimate_ids = fields.One2many('jakh.estimate', 'opportunity_id', string='Construction estimates', readonly=True)
    jakh_estimate_count = fields.Integer(compute='_compute_jakh_estimate_count')
    _unique_jakh_source = models.Constraint('UNIQUE(company_id, jakh_source_system, jakh_source_id)', 'An original CRM reference can be registered only once in the company.')

    @api.depends('jakh_estimate_ids')
    def _compute_jakh_estimate_count(self):
        for lead in self:
            lead.jakh_estimate_count = len(lead.jakh_estimate_ids)

    @api.constrains('jakh_source_amount', 'jakh_source_currency_id')
    def _check_source_currency(self):
        if any(lead.jakh_source_amount < 0 or lead.jakh_source_amount and not lead.jakh_source_currency_id for lead in self):
            raise ValidationError(_('An original CRM amount requires its original currency and cannot be negative.'))

    def write(self, values):
        source = {'jakh_source_system', 'jakh_source_id', 'jakh_source_currency_id', 'jakh_source_amount', 'jakh_source_payload'}
        if source.intersection(values):
            if not _has_role(self.env, EDITORS):
                raise AccessError(_('Only construction estimators/managers can register the original CRM source.'))
            if any(lead.jakh_source_id or lead.jakh_source_payload for lead in self):
                raise AccessError(_('Original CRM source data is retained unchanged.'))
        return super().write(values)

    def action_jakh_new_estimate(self):
        self.ensure_one()
        if not _has_role(self.env, EDITORS):
            raise AccessError(_('Only construction estimators/managers can prepare a tender estimate.'))
        if self.type != 'opportunity':
            raise UserError(_('Convert the lead to an opportunity before preparing an estimate.'))
        if not self.partner_id:
            raise UserError(_('Select a customer before preparing an estimate.'))
        company = self.company_id or self.env.company
        estimate = self.env['jakh.estimate'].create({'title': self.name, 'company_id': company.id, 'partner_id': self.partner_id.id, 'opportunity_id': self.id, 'currency_id': company.currency_id.id})
        # Native expected_revenue and original source currency/amount are never relabelled.
        return {'type': 'ir.actions.act_window', 'name': _('Construction estimate'), 'res_model': 'jakh.estimate', 'res_id': estimate.id, 'view_mode': 'form', 'target': 'current'}

    def action_jakh_estimates(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Construction estimates'), 'res_model': 'jakh.estimate', 'view_mode': 'list,form', 'domain': [('opportunity_id', '=', self.id)], 'context': {'default_opportunity_id': self.id, 'default_partner_id': self.partner_id.id, 'default_title': self.name}}
