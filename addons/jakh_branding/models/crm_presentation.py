from markupsafe import Markup

from odoo import api, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    @api.model
    def get_empty_list_help(self, help_message):
        """Business guidance for empty results, including filtered views.

        Presentation only: team membership, inbound aliases, create rights and
        result domains remain managed by their existing business rules.
        """
        return Markup('<p class="o_view_nocontent_smiling_face">%s</p><p>%s</p>') % (
            self.env._('No leads or opportunities match this view'),
            self.env._('Create a lead or opportunity with New, import reviewed records, or adjust your filters.'),
        )
