import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class CostCode(models.Model):
    _name = 'jakh.cost.code'
    _description = 'Construction Cost Code'
    _inherit = ['mail.thread']
    _check_company_auto = True
    _order = 'code, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, index=True)
    parent_id = fields.Many2one('jakh.cost.code', check_company=True, ondelete='restrict')
    active = fields.Boolean(default=True)
    resource_type = fields.Selection([
        ('material', 'Material'), ('labour', 'Labour'), ('equipment', 'Equipment'),
        ('subcontract', 'Subcontract'), ('other', 'Other')], required=True, default='material')

    _code_unique = models.Constraint('UNIQUE(company_id, code)', 'Cost code must be unique within its company.')

    @api.constrains('parent_id')
    def _check_parent(self):
        for row in self:
            seen = {row.id}
            parent = row.parent_id
            while parent:
                if parent.id in seen:
                    raise ValidationError(self.env._('Cost-code hierarchy cannot contain a cycle.'))
                seen.add(parent.id)
                parent = parent.parent_id


class Wbs(models.Model):
    _name = 'jakh.wbs'
    _description = 'Project Work Breakdown Structure'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'project_id, sequence, code, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    parent_id = fields.Many2one('jakh.wbs', check_company=True, ondelete='restrict')
    cost_code_id = fields.Many2one('jakh.cost.code', check_company=True, ondelete='restrict')
    budget_amount = fields.Monetary(default=0, groups='jakh_core.group_financial')
    forecast_amount = fields.Monetary(default=0, groups='jakh_core.group_financial')
    state = fields.Selection([('draft', 'Draft'), ('released', 'Released')], default='draft', required=True, readonly=True, copy=False)

    _code_unique = models.Constraint('UNIQUE(project_id, code)', 'WBS code must be unique within its project.')
    _budget_nonnegative = models.Constraint('CHECK(budget_amount >= 0 AND forecast_amount >= 0)', 'Budget and forecast cannot be negative.')

    @api.constrains('parent_id', 'project_id', 'company_id')
    def _check_structure(self):
        for row in self:
            if not row.company_id:
                raise ValidationError(self.env._('Construction projects must belong to a legal entity.'))
            if row.parent_id and row.parent_id.project_id != row.project_id:
                raise ValidationError(self.env._('Parent WBS must belong to the same project.'))
            seen = {row.id}
            parent = row.parent_id
            while parent:
                if parent.id in seen:
                    raise ValidationError(self.env._('WBS hierarchy cannot contain a cycle.'))
                seen.add(parent.id)
                parent = parent.parent_id

    @api.model_create_multi
    def create(self, vals_list):
        if (self.env.context.get('default_state', 'draft') != 'draft'
                or any(vals.get('state', 'draft') != 'draft' for vals in vals_list)):
            raise AccessError(self.env._('Create a draft WBS and release it through its action.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'state' in vals:
            raise AccessError(self.env._('Use the WBS release action to change its state.'))
        if any(row.state == 'released' for row in self) and set(vals) & {'code', 'project_id', 'parent_id', 'cost_code_id', 'budget_amount'}:
            raise ValidationError(self.env._('A released WBS is locked. Budget changes require a later controlled revision.'))
        return super().write(vals)

    def unlink(self):
        if any(row.state != 'draft' for row in self):
            raise ValidationError(self.env._('Released WBS records cannot be deleted.'))
        return super().unlink()

    def action_release(self):
        self.check_access('write')
        if not self.env.user.has_group('jakh_core.group_manager'):
            raise AccessError(self.env._('A construction manager must release the WBS.'))
        for row in self:
            self.env.cr.execute('SELECT id FROM jakh_wbs WHERE id = %s FOR UPDATE', [row.id])
            row.invalidate_recordset(['state'])
            if row.state == 'released':
                continue
            super(Wbs, row).write({'state': 'released'})
            self.env['jakh.audit.event']._append(row.company_id, 'wbs.released', row, {'code': row.code})
        return True


class Project(models.Model):
    _inherit = 'project.project'

    jakh_country = fields.Selection([('sa', 'Saudi Arabia'), ('ae', 'United Arab Emirates')], string='Operating jurisdiction')
    jakh_wbs_ids = fields.One2many('jakh.wbs', 'project_id', string='WBS / Cost Control')
    jakh_source_ref = fields.Char(copy=False, index=True)


class AuditEvent(models.Model):
    _name = 'jakh.audit.event'
    _description = 'Immutable JAKH Business Event'
    _order = 'create_date desc, id desc'

    company_id = fields.Many2one('res.company', required=True, ondelete='restrict', index=True)
    actor_id = fields.Many2one('res.users', required=True, ondelete='restrict')
    operation = fields.Char(required=True, index=True)
    resource_model = fields.Char(required=True)
    resource_id = fields.Integer(required=True)
    details = fields.Text(required=True)

    @api.model
    def _append(self, company, operation, resource, details=None):
        # Private server method, not RPC-callable. Only this event insertion is elevated;
        # business reads/writes remain under the original user's permissions.
        return super(AuditEvent, self.sudo()).create({
            'company_id': company.id, 'actor_id': self.env.uid,
            'operation': operation, 'resource_model': resource._name,
            'resource_id': resource.id, 'details': json.dumps(details or {}, sort_keys=True),
        })

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._('Business events are created by controlled server actions.'))

    def write(self, vals):
        raise AccessError(self.env._('Audit events are immutable.'))

    def unlink(self):
        raise AccessError(self.env._('Audit events cannot be deleted.'))


class ModuleMenu(models.Model):
    _inherit = 'ir.ui.menu'

    @api.model
    def _jakh_configure_module_menus(self):
        if not self.env.is_system():
            raise AccessError(self.env._('Module menu configuration requires system administration.'))
        construction = self.env.ref('jakh_core.menu_construction')
        administration = self.env.ref('jakh_core.menu_administration')
        reports = self.env.ref('jakh_core.menu_reports')
        # Configure upstream menus, never patch upstream code or weaken their groups.
        mapping = [
            ('crm.crm_menu_root', 'CRM', 10), ('sale.sale_menu_root', 'Sales & Quotations', 25),
            ('project.menu_main_pm', 'Projects', 30), ('purchase.menu_purchase_root', 'Purchasing', 50),
            ('stock.menu_stock_root', 'Stores & Receipts', 60), ('account.menu_finance', 'Finance', 65),
            ('contacts.menu_contacts', 'Contacts & Masters', 80), ('mail.menu_root_discuss', 'Team Discussions', 86),
            ('project_todo.menu_todo_todos', 'My Tasks', 87),
        ]
        for xmlid, name, sequence in mapping:
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            if menu:
                # Always update the source term, not the administrator's chosen
                # language. Native ar.po entries supply translated menu names.
                menu.with_context(lang='en_US').write({'parent_id': construction.id, 'name': name, 'sequence': sequence})
        for xmlid in ('jakh_core.menu_crm', 'jakh_core.menu_projects', 'jakh_core.menu_purchase', 'jakh_core.menu_stores'):
            self.env.ref(xmlid).write({'active': False})
        # Keep saved analytical dashboards as reports, not a competing home page.
        saved_reports = self.env.ref('spreadsheet_dashboard.spreadsheet_dashboard_menu_root', raise_if_not_found=False)
        if saved_reports:
            saved_reports.with_context(lang='en_US').write({'parent_id': reports.id, 'name': 'Saved Dashboards', 'sequence': 60})
        # Other native apps/settings stay available under Administration. No
        # module, native action or permission is removed by consolidation.
        for menu in self.search([('parent_id', '=', False), ('id', '!=', construction.id)]):
            menu.write({'parent_id': administration.id})
        return True
