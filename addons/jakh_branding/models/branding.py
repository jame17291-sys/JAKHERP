from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import BinaryBytes, file_open

from ..brand_assets import company_logo_png, icon_png


class Company(models.Model):
    _inherit = 'res.company'

    def _get_logo(self):
        return BinaryBytes(company_logo_png(), filename='jakh-logo.png')

    # The base field default holds a function reference; explicitly dispatch our
    # override for future companies without changing related-field behaviour.
    logo = fields.Binary(default=lambda self: self._get_logo())
    email_secondary_color = fields.Char(default='#146E65')


class Http(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _get_translation_frontend_modules_name(cls):
        return [*super()._get_translation_frontend_modules_name(), 'jakh_core', 'jakh_branding']

    def session_info(self):
        result = super().session_info()
        result['support_url'] = '/jakh/help'
        return result

    def webclient_rendering_context(self):
        result = super().webclient_rendering_context()
        result['title'] = 'JAKH ERP'
        return result


class Menu(models.Model):
    _inherit = 'ir.ui.menu'

    def _load_menus_blacklist(self):
        """Hide removed product features without deleting their historical data."""
        hidden = set(super()._load_menus_blacklist())
        for xmlid in ('base.menu_management', 'crm_iap_mine.crm_menu_lead_generation'):
            root = self.env.ref(xmlid, raise_if_not_found=False)
            if root:
                # Include all descendants, including debug-only and future entries.
                # Read-only sudo resolves IDs; it grants no menu/action/model access.
                hidden.update(self.sudo().with_context(active_test=False).search([
                    ('id', 'child_of', root.id),
                ]).ids)
        return sorted(hidden)


class Users(models.Model):
    _inherit = 'res.users'

    notification_type = fields.Selection(
        selection_add=[('inbox', 'In JAKH ERP')],
        help='Choose email delivery or notifications in your JAKH ERP inbox.')

    @api.model
    def _jakh_disable_automatic_enrichment(self, requested_by=None):
        """Explicit administrator action; do not silently reset later choices."""
        if not self.env.is_system():
            raise AccessError(self.env._('External-service configuration requires system administration.'))
        parameters = self.env['ir.config_parameter'].sudo()
        previous = parameters.get_str('crm.iap.lead.enrich.setting') or 'auto'
        parameters.set_str('crm.iap.lead.enrich.setting', 'manual')
        cron = self.env.ref('crm_iap_enrich.ir_cron_lead_enrichment', raise_if_not_found=False)
        was_active = bool(cron and cron.active)
        if cron:
            cron.sudo().active = False
        resource = parameters.search([('key', '=', 'crm.iap.lead.enrich.setting')], limit=1)
        if previous != 'manual' or was_active:
            self.env['jakh.audit.event']._append(self.env.company, 'disable_automatic_external_enrichment', resource, {
                'previousMode': previous, 'newMode': 'manual', 'previousCronActive': was_active,
                'requestedBy': requested_by or self.env.user.login,
                'existingLeadsModified': False,
            })
        return True

    @api.model
    def _jakh_apply_brand_defaults(self):
        if not self.env.is_system():
            raise AccessError(self.env._('Brand configuration requires system administration.'))
        self.env['ir.config_parameter'].sudo().set_str('web.web_app_name', 'JAKH ERP')
        with file_open('base/static/img/res_company_logo.png', 'rb') as source:
            original_default = source.read()
        # Only replace the exact upstream default, never a customer's own logo.
        for company in self.env['res.company'].sudo().search([]):
            if not company.logo or company.logo.content in (original_default, company_logo_png('#705365', '#287e79')):
                company.logo = company._get_logo()
            if (company.email_secondary_color or '').lower() == '#875a7b':
                company.email_secondary_color = '#146E65'
        assistant = self.env.ref('base.partner_root', raise_if_not_found=False)
        if assistant and assistant.name in ('OdooBot', 'System'):
            assistant.write({'name': 'JAKH Assistant', 'image_1920': BinaryBytes(icon_png(), filename='jakh-assistant.png')})
        elif assistant and assistant.name == 'JAKH Assistant' and assistant.image_1920 and assistant.image_1920.content == icon_png(192, '#705365', '#64c6bb'):
            assistant.image_1920 = BinaryBytes(icon_png(), filename='jakh-assistant.png')
        digest = self.env.ref('digest.digest_digest_default', raise_if_not_found=False)
        if digest and digest.name == 'Your Odoo Periodic Digest':
            digest.name = 'Your JAKH ERP Periodic Digest'
        return True


class MailTemplate(models.Model):
    _inherit = 'mail.template'

    def send_mail_batch(self, *args, **kwargs):
        template = self.env.ref('auth_signup.set_password_email', raise_if_not_found=False)
        subtitles = list(self.env.context.get('email_notification_subtitles') or [])
        if template and self == template and subtitles:
            # The signup service supplies this first subtitle independently of
            # the email template. Do not rename business subjects or user names.
            subtitles[0] = self.env._('Welcome to JAKH ERP')
            return super(MailTemplate, self.with_context(email_notification_subtitles=subtitles)).send_mail_batch(*args, **kwargs)
        return super().send_mail_batch(*args, **kwargs)
