"""Retire paid lead generation without deleting CRM records or provenance.

Installed provider models remain for historical references and safe upgrades.
The product has no switch to reactivate these operations; both UI and RPC
entrypoints are disabled, including privileged calls and stale background jobs.
"""
from odoo import api, models
from odoo.exceptions import AccessError, UserError


def _disabled(recordset):
    raise UserError(recordset.env._('Lead generation and external lead enrichment are disabled in JAKH ERP.'))


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    @api.depends('email_from', 'probability', 'iap_enrich_done', 'reveal_id')
    def _compute_show_enrich_button(self):
        for lead in self:
            lead.show_enrich_button = False

    @api.model
    def action_generate_leads(self):
        _disabled(self)

    def iap_enrich(self, *, batch_size=50):
        _disabled(self)

    def _process_leads(self, send_notification=True):
        _disabled(self)

    @api.model
    def _iap_enrich_leads_cron(self, enrich_hours_delay=24, batch_size=50):
        # A previously queued job must not send data, consume credits or mutate
        # existing leads, even if an administrator reactivates the native cron.
        return None


class LeadMiningRequest(models.Model):
    _inherit = 'crm.iap.lead.mining.request'

    @api.model_create_multi
    def create(self, vals_list):
        _disabled(self)

    def action_submit(self):
        _disabled(self)

    def action_draft(self):
        _disabled(self)

    def action_buy_credits(self):
        _disabled(self)

    def _perform_request(self):
        _disabled(self)

    def _iap_contact_mining(self, params, timeout=300):
        _disabled(self)


class Settings(models.TransientModel):
    _inherit = 'res.config.settings'

    def set_values(self):
        # Validate before super() changes any other settings or schedules jobs.
        if any(settings.lead_enrich_auto == 'auto' or settings.module_website_crm_iap_reveal for settings in self):
            _disabled(self)
        return super().set_values()


class Users(models.Model):
    _inherit = 'res.users'

    @api.model
    def _jakh_disable_lead_generation(self):
        """Idempotent, audited deployment policy; retain leads and past requests."""
        if not self.env.is_system():
            raise AccessError(self.env._('External-service configuration requires system administration.'))
        parameters = self.env['ir.config_parameter'].sudo()
        key = 'jakh.crm.lead_generation.disabled'
        already_disabled = parameters.get_bool(key)
        action = self.env.ref('crm_iap_enrich.action_enrich_mail', raise_if_not_found=False)
        had_binding = bool(action and action.binding_model_id)
        self._jakh_disable_automatic_enrichment()
        if action and had_binding:
            action.sudo().binding_model_id = False
        parameters.set_bool(key, True)
        if not already_disabled or had_binding:
            resource = parameters.search([('key', '=', key)], limit=1)
            self.env['jakh.audit.event']._append(self.env.company, 'disable_lead_generation', resource, {
                'leadMiningDisabled': True,
                'leadEnrichmentDisabled': True,
                'automaticEnrichmentDisabled': True,
                'existingLeadsModified': False,
                'historicalRequestsDeleted': False,
            })
        return True
