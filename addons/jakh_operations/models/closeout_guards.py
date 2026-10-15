"""Keep closed project evidence readable while guarding new operational work."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from .procurement import lock_records, require_role


class ProjectCloseGuard(models.Model):
    _inherit = 'project.project'

    jakh_workflow_closed = fields.Boolean(readonly=True, copy=False, string='Commercially closed')

    def _jakh_assert_open(self):
        self.check_access('read')
        lock_records(self, budget=True)
        if self.filtered('jakh_workflow_closed'):
            raise UserError(_('Reopen the approved final account before creating or changing project work.'))
        return True

    def _jakh_set_closed(self, closed):
        require_role(self.env, ['group_manager'])
        lock_records(self, touch=True)
        # Native project archiving propagates active to tasks. Complete that
        # propagation while operational writes are still permitted, then lock
        # the project. Reopening unlocks before restoring native task activity.
        if closed:
            super().write({'active': False})
            result = super().write({'jakh_workflow_closed': True})
        else:
            super().write({'jakh_workflow_closed': False})
            result = super().write({'active': True})
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'project.closed' if closed else 'project.reopened', row)
        return result

    def write(self, values):
        if 'jakh_workflow_closed' in values or (values.get('active') and self.filtered('jakh_workflow_closed')):
            raise AccessError(_('Project closure and reopening require the approved final-account workflow.'))
        return super().write(values)


def _guard(records):
    records.project_id._jakh_assert_open()


class RequestGuard(models.Model):
    _inherit = 'jakh.purchase.request'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        _guard(self)
        return super().write(values)


class ActualFeedGuard(models.Model):
    _inherit = 'jakh.project.actual.feed'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class ProgressGuard(models.Model):
    _inherit = 'jakh.project.schedule.progress'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class CashGuard(models.Model):
    _inherit = 'jakh.project.cash.forecast'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class ServiceGuard(models.Model):
    _inherit = 'jakh.subcontract.service.entry'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class VariationGuard(models.Model):
    _inherit = 'jakh.project.variation'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class ServiceReversalGuard(models.Model):
    _inherit = 'jakh.subcontract.service.reversal'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        _guard(self)
        return super().write(values)

    def action_approve(self):
        _guard(self)
        return super().action_approve()


class SubcontractAmendmentGuard(models.Model):
    _inherit = 'jakh.subcontract.amendment'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        _guard(self)
        return super().write(values)

    def action_approve(self):
        _guard(self)
        return super().action_approve()


class BudgetGuard(models.Model):
    _inherit = 'jakh.project.budget.revision'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class ForecastGuard(models.Model):
    _inherit = 'jakh.project.forecast'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class AssuranceGuard(models.Model):
    _inherit = 'jakh.assurance.case'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        _guard(self)
        return super().write(values)


class DocumentGuard(models.Model):
    _inherit = 'jakh.document'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        _guard(self)
        return super().write(values)


class RevisionGuard(models.Model):
    _inherit = 'jakh.document.revision'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records


class ProjectTaskGuard(models.Model):
    _inherit = 'project.task'

    @api.model_create_multi
    def create(self, values_list):
        records = super().create(values_list); _guard(records); return records

    def write(self, values):
        if set(values) - {'message_follower_ids', 'message_ids', 'activity_ids'}:
            _guard(self)
        return super().write(values)
