"""Supported UI languages; locale metadata remains available to the framework."""
from odoo import api, fields, models
from odoo.exceptions import ValidationError


SUPPORTED_LANGUAGES = ('en_US', 'ar_001')


class Language(models.Model):
    _inherit = 'res.lang'

    @api.constrains('active', 'code')
    def _check_jakh_supported_language(self):
        if any(language.active and language.code not in SUPPORTED_LANGUAGES for language in self):
            raise ValidationError(self.env._('JAKH ERP supports English and Arabic only.'))


class LanguageInstall(models.TransientModel):
    _inherit = 'base.language.install'

    lang_ids = fields.Many2many(domain=[('code', 'in', SUPPORTED_LANGUAGES)])
