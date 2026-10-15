from odoo import api, fields, models
from odoo.exceptions import AccessError


class Dashboard(models.AbstractModel):
    _name = 'jakh.dashboard'
    _description = 'Permission-aware JAKH Dashboard'

    @api.model
    def get_data(self):
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(self.env._('An internal account is required.'))
        today = fields.Date.to_string(fields.Date.context_today(self))
        overdue = [
            ('state', '=', 'posted'),
            ('payment_state', 'in', ['not_paid', 'partial']),
            ('invoice_date_due', '<', today),
        ]
        candidates = [
            (self.env._('Active projects'), 'project.project', [('active', '=', True)], 'jakh_core.action_projects'),
            (self.env._('Open opportunities'), 'crm.lead', [('type', '=', 'opportunity'), ('active', '=', True), ('probability', '<', 100)], 'jakh_core.action_crm'),
            (self.env._('Estimates awaiting approval'), 'jakh.estimate', [('state', '=', 'submitted')], 'jakh_estimate.action_estimate'),
            (self.env._('Requisitions awaiting approval'), 'jakh.purchase.request', [('state', '=', 'submitted')], 'jakh_operations.action_purchase_requests'),
            (self.env._('Certificates awaiting approval'), 'jakh.payment.certificate', [('state', '=', 'submitted')], 'jakh_operations.action_payment_certificates'),
            (self.env._('Priced variations awaiting approval'), 'jakh.project.variation', [('state', '=', 'priced')], 'jakh_operations.action_variations'),
            (self.env._('Budget revisions awaiting approval'), 'jakh.project.budget.revision', [('state', '=', 'submitted')], 'jakh_operations.action_budget_revisions'),
            (self.env._('Forecasts awaiting approval'), 'jakh.project.forecast', [('state', '=', 'submitted')], 'jakh_operations.action_forecasts'),
            (self.env._('Pending receipts'), 'stock.picking', [('picking_type_code', '=', 'incoming'), ('state', 'not in', ['draft', 'done', 'cancel'])], 'stock.action_picking_tree_incoming'),
            (self.env._('Overdue customer invoices'), 'account.move', overdue + [('move_type', '=', 'out_invoice')], 'account.action_move_out_invoice_type'),
            (self.env._('Overdue vendor bills'), 'account.move', overdue + [('move_type', '=', 'in_invoice')], 'account.action_move_in_invoice_type'),
            (self.env._('Open quality / HSE actions'), 'jakh.assurance.case', [('state', 'not in', ['closed', 'cancelled'])], 'jakh_assurance.action_cases'),
            (self.env._('Documents awaiting approval'), 'jakh.document.revision', [('state', '=', 'submitted')], 'jakh_assurance.action_revisions'),
        ]
        cards = []
        for label, model, domain, action in candidates:
            if model not in self.env:
                continue
            Model = self.env[model]
            if not Model.has_access('read'):
                continue
            if 'company_id' in Model._fields:
                domain = domain + [('company_id', 'in', self.env.companies.ids)]
            target = self.env.ref(action, raise_if_not_found=False)
            if not target:
                continue
            # Native /web/action/load also reads action metadata with sudo.
            # Staff need no administration rights to use an allowed action.
            # Never elevate the business model, count, domain or record access.
            action_groups = target.sudo().group_ids
            if action_groups and not (action_groups & self.env.user.all_group_ids):
                continue
            cards.append({'label': label, 'count': Model.search_count(domain), 'action': action, 'domain': domain})
        portfolio = []
        if ('jakh.construction.control' in self.env and any(self.env.user.has_group('jakh_core.' + role)
                for role in ('group_manager', 'group_project_manager', 'group_finance'))):
            portfolio = self.env['jakh.construction.control'].get_portfolio()
        return {
            'companies': self.env.companies.mapped('name'),
            'generated_at': fields.Datetime.to_string(fields.Datetime.now()),
            'cards': cards,
            'portfolio': portfolio,
            'notice': self.env._('Record counts for your selected companies and access rights. Overdue invoices and bills include only posted, unpaid or partially paid documents past their due date; payments in progress are excluded. Pending receipts exclude drafts and completed or cancelled transfers. No currency amounts are combined.'),
        }
