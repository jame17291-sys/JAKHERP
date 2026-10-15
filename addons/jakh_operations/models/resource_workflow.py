"""Company-scoped labour/plant capacity through verified actual posting."""
import math
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role
from .execution_controls import _has_server_defaults


class SiteResource(models.Model):
    _name = 'jakh.site.resource'
    _description = 'Site labour or plant resource'
    _inherit = ['mail.thread']
    _check_company_auto = True

    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    kind = fields.Selection([('labour', 'Labour crew'), ('plant', 'Plant / equipment')], required=True, default='plant')
    source_reference = fields.Char(required=True)
    active = fields.Boolean(default=True)
    cost_journal_id = fields.Many2one('account.journal', check_company=True, groups=FINANCIAL, domain="[('type','=','general')]")
    expense_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)
    clearing_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)

    _source_once = models.Constraint('UNIQUE(company_id,source_reference)', 'A site resource reference must be unique within its legal entity.')

    def write(self, values):
        if set(values) & {'company_id', 'kind', 'source_reference'} and self.env['jakh.resource.assignment'].search_count([('resource_id', 'in', self.ids)]):
            raise UserError(_('A resource used by project allocations cannot change its legal entity, type or identity.'))
        return super().write(values)


class ResourceAssignment(models.Model):
    _name = 'jakh.resource.assignment'
    _description = 'Controlled site resource allocation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'start_at, id'

    name = fields.Char(required=True)
    resource_id = fields.Many2one('jakh.site.resource', required=True, check_company=True, ondelete='restrict')
    project_id = fields.Many2one('project.project', required=True, check_company=True, ondelete='restrict')
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True, ondelete='restrict')
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    start_at = fields.Datetime(required=True)
    end_at = fields.Datetime(required=True)
    capacity_percent = fields.Float(required=True, default=100)
    site_reference = fields.Char(required=True)
    actual_hours = fields.Float(default=0)
    actual_cost = fields.Monetary(default=0, groups=FINANCIAL)
    completion_reference = fields.Char()
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Allocated'), ('completed', 'Actuals prepared'), ('cancelled', 'Cancelled')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    actual_feed_id = fields.Many2one('jakh.project.actual.feed', readonly=True, copy=False, check_company=True, groups=FINANCIAL)
    _server_fields = {'state', 'approved_by', 'actual_feed_id'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields for values in values_list)):
            raise AccessError(_('Resource-allocation workflow values are assigned by server actions.'))
        records = super().create(values_list)
        records.project_id._jakh_assert_open()
        return records

    def write(self, values):
        self.project_id._jakh_assert_open()
        require_role(self.env, ['group_manager', 'group_project_manager'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled allocation actions.'))
        if self.filtered(lambda row: row.state in ('completed', 'cancelled')):
            raise UserError(_('Completed or cancelled allocations are immutable.'))
        if self.filtered(lambda row: row.state == 'approved') and set(values) - {'actual_hours', 'actual_cost', 'completion_reference'}:
            raise UserError(_('Cancel and replace an approved allocation before changing its resource, timing or scope.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft resource allocations can be deleted.'))
        return super().unlink()

    @api.constrains('resource_id', 'project_id', 'wbs_id', 'start_at', 'end_at', 'capacity_percent', 'actual_hours', 'actual_cost')
    def _check_assignment(self):
        for row in self:
            if row.wbs_id.project_id != row.project_id or row.resource_id.company_id != row.company_id:
                raise ValidationError(_('A site resource allocation must use the same legal entity, project and WBS.'))
            if (not all(math.isfinite(value) for value in (row.capacity_percent, row.actual_hours, row.actual_cost))
                    or row.start_at >= row.end_at or not 0 < row.capacity_percent <= 100 or row.actual_hours < 0 or row.actual_cost < 0):
                raise ValidationError(_('Use a valid allocation period, capacity between zero and 100, and non-negative actuals.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'resource_allocation.' + row.state, row, {'resource': row.resource_id.id})
        return result

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            row.project_id._jakh_assert_open(); lock_records(row); lock_records(row.resource_id, budget=True)
            if row.state == 'approved':
                continue
            if row.state != 'draft' or row.wbs_id.state != 'released' or not row.resource_id.active:
                raise UserError(_('Allocate a draft assignment against a released WBS and available resource.'))
            # Capacity is checked across every project in this permitted legal
            # entity. Only the availability result is disclosed; foreign
            # project details do not leave this private server check.
            others = self.sudo().search([('resource_id', '=', row.resource_id.id), ('company_id', '=', row.company_id.id),
                ('state', 'in', ['approved', 'completed']), ('id', '!=', row.id),
                ('start_at', '<', row.end_at), ('end_at', '>', row.start_at)])
            points = {row.start_at, row.end_at}
            for other in others:
                points.update((max(row.start_at, other.start_at), min(row.end_at, other.end_at)))
            for point in sorted(points)[:-1]:
                total = row.capacity_percent + sum(other.capacity_percent for other in others if other.start_at <= point < other.end_at)
                if total > 100 + 0.000001:
                    raise UserError(_('This resource is already allocated beyond its available capacity for the requested period.'))
            row._transition(state='approved', approved_by=self.env.user.id)
        return True

    def action_prepare_actual(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        self.ensure_one(); self.project_id._jakh_assert_open(); lock_records(self)
        if self.actual_feed_id:
            return form_action(self.actual_feed_id)
        if self.state != 'approved' or self.actual_hours <= 0 or not self.completion_reference:
            raise UserError(_('Complete an approved allocation with actual hours, cost and source evidence.'))
        resource = self.resource_id
        feed = self.env['jakh.project.actual.feed'].create({
            'name': self.name, 'source_system': 'plant' if resource.kind == 'plant' else 'verified',
            'actual_type': resource.kind, 'external_id': 'allocation:%s' % self.id,
            'source_reference': self.completion_reference, 'project_id': self.project_id.id, 'wbs_id': self.wbs_id.id,
            'contract_line_id': self.contract_line_id.id, 'location_id': self.location_id.id,
            'discipline_id': self.discipline_id.id,
            'work_date': fields.Date.to_date(self.end_at), 'resource_reference': resource.source_reference,
            'quantity': self.actual_hours, 'unit_label': 'hours', 'amount': self.actual_cost,
            'journal_id': resource.cost_journal_id.id, 'expense_account_id': resource.expense_account_id.id,
            'offset_account_id': resource.clearing_account_id.id,
        })
        feed.action_submit()
        self._transition(state='completed', actual_feed_id=feed.id)
        return form_action(feed)

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_project_manager'])
        for row in self:
            row.project_id._jakh_assert_open(); lock_records(row); lock_records(row.resource_id, budget=True)
            if row.state not in ('draft', 'approved'):
                raise UserError(_('Only uncompleted allocations can be cancelled.'))
            row._transition(state='cancelled')
        return True
