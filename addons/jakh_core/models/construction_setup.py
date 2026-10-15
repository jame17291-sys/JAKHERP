"""Shared execution locations and native accounting cost-centre setup."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


class ProjectLocation(models.Model):
    _name = 'jakh.project.location'
    _description = 'Project building / floor / zone'
    _inherit = ['mail.thread']
    _check_company_auto = True
    _order = 'project_id, code, id'

    name = fields.Char(required=True, translate=True, tracking=True)
    code = fields.Char(required=True, index=True, tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='project_id.company_id', store=True, readonly=True)
    parent_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    kind = fields.Selection([('building', 'Building'), ('floor', 'Floor'), ('zone', 'Zone')], required=True, default='building')
    active = fields.Boolean(default=True)
    _code_unique = models.Constraint('UNIQUE(project_id, code)', 'Location code must be unique within its project.')

    @api.constrains('project_id', 'parent_id', 'kind')
    def _check_hierarchy(self):
        for row in self:
            if not row.company_id:
                raise ValidationError(_('An execution location requires a project legal entity.'))
            if row.parent_id and row.parent_id.project_id != row.project_id:
                raise ValidationError(_('A location parent must belong to the same project.'))
            if row.kind == 'floor' and (not row.parent_id or row.parent_id.kind != 'building'):
                raise ValidationError(_('A floor must belong to a building.'))
            if row.kind == 'building' and row.parent_id:
                raise ValidationError(_('A building is a top-level project location.'))
            seen = {row.id}
            parent = row.parent_id
            while parent:
                if parent.id in seen:
                    raise ValidationError(_('Execution locations cannot contain a cycle.'))
                seen.add(parent.id)
                parent = parent.parent_id

    def write(self, values):
        if set(values) & {'code', 'project_id', 'parent_id', 'kind'}:
            raise ValidationError(_('Location identity is permanent. Create a new location rather than moving historical work.'))
        return super().write(values)


class Discipline(models.Model):
    _name = 'jakh.discipline'
    _description = 'Construction discipline'
    _check_company_auto = True
    _order = 'code, id'
    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, ondelete='restrict')
    active = fields.Boolean(default=True)
    _code_unique = models.Constraint('UNIQUE(company_id,code)', 'Discipline code must be unique within its legal entity.')

    def write(self, values):
        if set(values) & {'code', 'company_id'}:
            raise ValidationError(_('Discipline identity is permanent. Archive and create a new discipline instead.'))
        return super().write(values)


class Project(models.Model):
    _inherit = 'project.project'
    jakh_location_ids = fields.One2many('jakh.project.location', 'project_id', string='Buildings / Floors / Zones')

    def _jakh_prepare_cost_centre(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('jakh_core.group_manager'):
            raise AccessError(_('A construction director must prepare project accounting cost centres.'))
        if not self.company_id or self.company_id not in self.env.companies:
            raise AccessError(_('Select the project legal entity before preparing its cost centre.'))
        if hasattr(self, '_jakh_assert_open'):
            self._jakh_assert_open()
        self.env.cr.execute('SELECT id FROM project_project WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset(['account_id'])
        if not self.account_id:
            self._create_analytic_account()
            self.env['jakh.audit.event']._append(self.company_id, 'project.cost_centre_created', self,
                {'account_id': self.account_id.id})
        if self.account_id.company_id != self.company_id:
            raise ValidationError(_('The accounting cost centre must belong to the project legal entity.'))
        return self.account_id

    def write(self, values):
        protected = self.filtered(lambda project: project.jakh_wbs_ids or project.jakh_location_ids)
        if 'company_id' in values and protected.filtered(lambda project: project.company_id.id != values['company_id']):
            raise ValidationError(_('A project legal entity cannot change after its construction hierarchy is established.'))
        if 'account_id' in values and protected.filtered(lambda project: project.account_id and project.account_id.id != values['account_id']):
            raise ValidationError(_('A construction project accounting cost centre cannot be replaced after its hierarchy is established.'))
        return super().write(values)

    @api.constrains('account_id', 'company_id')
    def _check_jakh_accounting_scope(self):
        for project in self:
            if project.account_id and project.company_id and project.account_id.company_id != project.company_id:
                raise ValidationError(_('The accounting cost centre must belong to the project legal entity.'))

    def action_jakh_prepare_cost_centre(self):
        account = self._jakh_prepare_cost_centre()
        return {'type': 'ir.actions.act_window', 'res_model': account._name,
            'res_id': account.id, 'view_mode': 'form', 'views': [(False, 'form')], 'target': 'current'}


class Wbs(models.Model):
    _inherit = 'jakh.wbs'
    analytic_account_id = fields.Many2one('account.analytic.account', related='project_id.account_id', readonly=True,
        string='Project accounting cost centre')

    @api.model_create_multi
    def create(self, values_list):
        names = {'analytic_account_id', 'company_id', 'currency_id'}
        if any(set(values) & names for values in values_list) or any(self.env.context.get('default_' + name) for name in names):
            raise AccessError(_('WBS accounting and legal-entity links are derived from the project.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'analytic_account_id', 'company_id', 'currency_id'}:
            raise AccessError(_('WBS accounting and legal-entity links are derived from the project.'))
        return super().write(values)

    def action_release(self):
        self.check_access('write')
        if not self.env.user.has_group('jakh_core.group_manager'):
            raise AccessError(self.env._('A construction manager must release the WBS.'))
        if self:
            # Keep the same WBS -> company order used by procurement and cost
            # posting. Company.write never acquires a WBS/history lock.
            self.env.cr.execute('SELECT id FROM jakh_wbs WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            self.company_id._jakh_lock_construction_currency()
        return super().action_release()


class Company(models.Model):
    _inherit = 'res.company'

    def _jakh_lock_construction_currency(self):
        self.check_access('read')
        if self:
            self.flush_recordset(['currency_id'])
            self.env.cr.execute('SELECT id FROM res_company WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            # Native transactions use REPEATABLE READ: waiting alone could keep
            # an old no-history snapshot. Change only the row version so a
            # concurrent currency/release/first-fact request retries cleanly.
            self.env.cr.execute('UPDATE res_company SET id=id WHERE id IN %s', [tuple(self.ids)])
            self.invalidate_recordset(['currency_id'])
        return True

    def write(self, values):
        if 'currency_id' in values:
            # Check the real administrator and selected legal entities before
            # the only narrowly elevated operation: company-scoped existence
            # counts. Hidden private-project history must not be missed, but no
            # business records, amounts or source payloads are exposed.
            self.check_access('write')
            if any(company not in self.env.companies for company in self):
                raise AccessError(_('Select each legal entity before changing its currency.'))
            self._jakh_lock_construction_currency()
            changed = self.filtered(lambda company: company.currency_id.id != values['currency_id'])
            for company in changed:
                released = self.env['jakh.wbs'].sudo().search_count([
                    ('company_id', '=', company.id), ('state', '=', 'released')], limit=1)
                facts = ('jakh.project.control.entry' in self.env and self.env['jakh.project.control.entry'].sudo().search_count([
                    ('company_id', '=', company.id)], limit=1))
                if released or facts:
                    raise ValidationError(_('The legal entity currency cannot change after a construction budget is released or project-control facts exist.'))
        return super().write(values)


class AuditMixin(models.AbstractModel):
    _name = 'jakh.audit.mixin'
    _description = 'Permission-safe before / after business-document evidence'
    _jakh_audit_fields = ()

    def _jakh_audit_snapshot(self, names):
        accessible = self.fields_get(list(names))
        result = {}
        for name in names:
            if name not in accessible:
                continue
            field = self._fields[name]
            value = self[name]
            if field.type == 'many2one':
                value = value.id or False
            elif field.type in ('one2many', 'many2many'):
                value = value.ids
            elif field.type in ('date', 'datetime'):
                value = str(value) if value else False
            result[name] = value
        return result

    def _jakh_audit_changes(self, values, before):
        for row in self:
            after = row._jakh_audit_snapshot(before.get(row.id, {}).keys())
            changes = {name: {'before': old, 'after': after.get(name)}
                for name, old in before.get(row.id, {}).items() if old != after.get(name)}
            if changes and row.company_id:
                self.env['jakh.audit.event']._append(row.company_id, 'document.edited', row,
                    {'changes': changes})

    def write(self, values):
        names = set(values) & set(self._jakh_audit_fields)
        self.check_access('write')
        before = {row.id: row._jakh_audit_snapshot(names) for row in self} if names else {}
        result = super().write(values)
        if before:
            self._jakh_audit_changes(values, before)
        return result
