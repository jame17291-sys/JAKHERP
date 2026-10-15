"""Post verified construction resource costs to accounting exactly once."""
from decimal import Decimal

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role
from .execution_controls import _require_project_scope, _has_server_defaults
from .project_control import ProjectControlEntry

_ACTUAL_POSTING = object()


class ActualFeed(models.Model):
    _inherit = 'jakh.project.actual.feed'

    journal_id = fields.Many2one('account.journal', check_company=True, groups=FINANCIAL,
                                domain="[('type','=','general'),('company_id','=',company_id)]", string='Cost journal')
    expense_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL, string='Cost account')
    offset_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL, string='Accrual / clearing account')
    account_move_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, groups=FINANCIAL, string='Posted cost journal')
    effect = fields.Selection([('debit', 'Cost'), ('credit', 'Cost correction')], required=True, default='debit', tracking=True)
    correction_of_id = fields.Many2one('jakh.project.actual.feed', check_company=True, ondelete='restrict', tracking=True, string='Corrects accepted cost')
    correction_reason = fields.Text(tracking=True)

    _server_fields = {'state', 'accepted_by', 'accepted_at', 'account_move_id'}

    @api.constrains('correction_of_id', 'effect', 'project_id', 'wbs_id', 'amount')
    def _check_correction(self):
        for row in self:
            if row.effect == 'credit':
                original = row.correction_of_id
                if not original or original.state != 'accepted' or original.effect != 'debit':
                    raise ValidationError(_('A cost correction must reference an accepted original cost.'))
                if (original.project_id != row.project_id or original.wbs_id != row.wbs_id
                        or original.contract_line_id != row.contract_line_id or original.location_id != row.location_id
                        or original.discipline_id != row.discipline_id
                        or original.actual_type != row.actual_type or not row.correction_reason):
                    raise ValidationError(_('A cost correction must preserve its project, WBS and resource type and explain the correction.'))
            elif row.correction_of_id:
                raise ValidationError(_('Use the cost-correction effect when referencing an original cost.'))

    def _check_finance_configuration(self):
        self.ensure_one()
        if not self.journal_id or not self.expense_account_id or not self.offset_account_id:
            raise UserError(_('Select a general journal, cost account and accrual/clearing account before posting actual costs.'))
        if self.journal_id.type != 'general' or self.journal_id.company_id != self.company_id:
            raise UserError(_('Resource costs require a general journal in their legal entity.'))
        if self.expense_account_id.account_type not in ('expense', 'expense_direct_cost', 'expense_depreciation'):
            raise UserError(_('Choose an expense account for labour and plant actual costs.'))
        if self.offset_account_id == self.expense_account_id or self.offset_account_id.account_type in ('asset_receivable', 'liability_payable', 'off_balance'):
            raise UserError(_('Choose a separate accrual or clearing account for resource costs.'))
        for account in self.expense_account_id | self.offset_account_id:
            if self.company_id not in account.company_ids:
                raise UserError(_('Actual-cost accounts must be available in the selected legal entity.'))

    def action_accept(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row); _require_project_scope(row)
            if row.state == 'accepted':
                if not row.account_move_id or row.account_move_id.state != 'posted':
                    raise UserError(_('This historical cost needs finance reconciliation before further acceptance.'))
                continue
            if row.state != 'submitted':
                raise UserError(_('Accept a validated labour or plant actual-cost feed.'))
            row._check_finance_configuration()
            if row.effect == 'credit':
                original = row.correction_of_id
                lock_records(original, touch=True)
                if (original.journal_id != row.journal_id or original.expense_account_id != row.expense_account_id
                        or original.offset_account_id != row.offset_account_id):
                    raise UserError(_('Cost corrections must use the original posting accounts and journal.'))
                credits = self.search([('correction_of_id', '=', original.id), ('state', '=', 'accepted')])
                corrected = sum(Decimal(str(value)) for value in credits.mapped('amount'))
                if corrected + Decimal(str(row.amount)) > Decimal(str(original.amount)):
                    raise UserError(_('Cumulative cost corrections cannot exceed the original posted cost.'))
            signed = row.amount if row.effect == 'debit' else -row.amount
            move = self.env['account.move']._create_jakh_actual({
                'move_type': 'entry', 'company_id': row.company_id.id, 'journal_id': row.journal_id.id,
                'date': row.work_date, 'ref': row.source_reference, 'jakh_actual_feed_id': row.id,
                'line_ids': [
                    Command.create({'name': row.name, 'account_id': row.expense_account_id.id,
                                    'analytic_distribution': {str(row.project_id.account_id.id): 100} if row.project_id.account_id else False,
                                    'debit': max(signed, 0), 'credit': max(-signed, 0)}),
                    Command.create({'name': row.name, 'account_id': row.offset_account_id.id,
                                    'debit': max(-signed, 0), 'credit': max(signed, 0)}),
                ],
            })
            move.action_post()
            row._transition(state='accepted', accepted_by=self.env.user.id, accepted_at=fields.Datetime.now(), account_move_id=move.id)
        return True

    def action_open_journal(self):
        self.ensure_one()
        if not self.account_move_id:
            raise UserError(_('Accept and post this cost before opening its journal.'))
        return form_action(self.account_move_id)


class ActualCostMove(models.Model):
    _inherit = 'account.move'

    jakh_actual_feed_id = fields.Many2one('jakh.project.actual.feed', readonly=True, copy=False, check_company=True, ondelete='restrict')
    _one_actual_feed = models.Constraint('UNIQUE(jakh_actual_feed_id)', 'A resource-cost feed can create only one journal.')

    def _jakh_has_project_payable_source(self):
        self.ensure_one()
        move = self.reversed_entry_id or self
        return bool(move.jakh_service_entry_id or move.invoice_line_ids.purchase_line_id.jakh_request_line_id)

    @api.model_create_multi
    def create(self, values_list):
        if (_has_server_defaults(self.env, {'jakh_actual_feed_id'})
                or any(values.get('jakh_actual_feed_id') for values in values_list)):
            raise AccessError(_('Create resource-cost journals through the verified feed workflow.'))
        return super().create(values_list)

    def _create_jakh_actual(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        return super().create(values)

    def write(self, values):
        if 'jakh_actual_feed_id' in values:
            raise AccessError(_('The resource-cost journal identity is immutable.'))
        if self.filtered('jakh_actual_feed_id') and 'state' in values and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING:
            raise AccessError(_('Use a verified cost-correction feed to change an accepted resource cost.'))
        if self.filtered('jakh_actual_feed_id') and set(values) & {'journal_id', 'company_id', 'currency_id', 'date', 'line_ids'}:
            raise AccessError(_('Use a verified cost-correction feed to change an accepted resource cost.'))
        if (self.filtered(lambda move: move.state == 'posted' and move._jakh_has_project_payable_source())
                and set(values) & {'journal_id', 'company_id', 'currency_id', 'date', 'invoice_date', 'move_type', 'partner_id', 'line_ids', 'state'}
                and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING):
            raise AccessError(_('Use a native supplier credit to correct posted project payables.'))
        return super().write(values)

    def button_draft(self):
        if self.filtered('jakh_actual_feed_id'):
            raise UserError(_('Posted resource-cost journals require a controlled correction.'))
        if self.filtered(lambda move: move.state == 'posted' and move._jakh_has_project_payable_source()):
            raise UserError(_('Use a native supplier credit to correct posted project payables.'))
        return super().button_draft()

    def button_cancel(self):
        if self.filtered('jakh_actual_feed_id'):
            raise UserError(_('Posted resource-cost journals require a controlled correction.'))
        return super().button_cancel()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        if self.filtered('jakh_actual_feed_id'):
            raise UserError(_('Use a verified cost-correction feed to reverse a resource cost.'))
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    def _post(self, soft=True):
        for move in self.filtered('jakh_actual_feed_id'):
            row = move.jakh_actual_feed_id
            _require_project_scope(row)
            row._check_finance_configuration()
            signed = row.amount if row.effect == 'debit' else -row.amount
            cost_lines = move.line_ids.filtered(lambda line: line.account_id == row.expense_account_id)
            offset_lines = move.line_ids.filtered(lambda line: line.account_id == row.offset_account_id)
            if (move.journal_id != row.journal_id or move.company_id != row.company_id or move.date != row.work_date
                    or move.currency_id != row.currency_id or len(move.line_ids) != 2
                    or len(cost_lines) != 1 or len(offset_lines) != 1
                    or not row.currency_id.is_zero(cost_lines.balance - signed)
                    or not row.currency_id.is_zero(offset_lines.balance + signed)):
                raise UserError(_('The resource-cost journal must match its approved feed amount and finance configuration.'))
        result = super(ActualCostMove, self.with_context(_jakh_actual_posting=_ACTUAL_POSTING))._post(soft=soft)
        for move in self.filtered(lambda item: item.state == 'posted' and item.jakh_actual_feed_id):
            row = move.jakh_actual_feed_id
            line = move.line_ids.filtered(lambda item: item.account_id == row.expense_account_id)
            entry = self.env['jakh.project.control.entry']._record(
                name=move.name, company=row.company_id, project=row.project_id, wbs=row.wbs_id,
                entry_type='actual_cost', amount=line.balance, source_model=row._name, source_id=row.id,
                source_key='%s:%s:actual_cost' % (row._name, row.id), entry_date=move.date,
                quantity=row.quantity if row.effect == 'debit' else -row.quantity,
                provenance='integration' if row.source_system != 'verified' else 'native', notes=row.source_reference,
            )
            entry._attach_posting(line)
        return result


class ActualCostJournalLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, values_list):
        move_ids = {values.get('move_id') or self.env.context.get('default_move_id') for values in values_list}
        moves = self.env['account.move'].browse([record_id for record_id in move_ids if record_id])
        moves.check_access('read')
        if moves.filtered(lambda row: row.state == 'posted' and row.jakh_actual_feed_id):
            raise AccessError(_('Resource-cost journal lines are protected by their verified feed.'))
        if (moves.filtered(lambda move: move.state == 'posted' and move._jakh_has_project_payable_source())
                and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING):
            raise AccessError(_('Use a native supplier credit to correct posted project payables.'))
        return super().create(values_list)

    def write(self, values):
        if values.get('move_id'):
            destination = self.env['account.move'].browse(values['move_id'])
            destination.check_access('read')
            if destination.jakh_actual_feed_id:
                raise AccessError(_('Resource-cost journal lines are protected by their verified feed.'))
            if destination.state == 'posted' and destination._jakh_has_project_payable_source():
                raise AccessError(_('Use a native supplier credit to correct posted project payables.'))
        if self.move_id.filtered('jakh_actual_feed_id') and set(values) & {
            'move_id', 'account_id', 'debit', 'credit', 'balance', 'amount_currency', 'currency_id', 'tax_ids',
        }:
            raise AccessError(_('Resource-cost journal lines are protected by their verified feed.'))
        if (self.move_id.filtered(lambda move: move.state == 'posted' and move.jakh_actual_feed_id)
                and set(values) & {'analytic_distribution', 'partner_id', 'company_id'}
                and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING):
            raise AccessError(_('Resource-cost journal lines are protected by their verified feed.'))
        sealed = self.move_id.filtered(lambda move: move.state == 'posted' and move._jakh_has_project_payable_source())
        if (sealed and set(values) & {'move_id', 'account_id', 'partner_id', 'company_id', 'currency_id',
                    'product_id', 'product_uom_id', 'purchase_line_id', 'quantity', 'price_unit', 'discount',
                    'debit', 'credit', 'balance', 'amount_currency', 'tax_ids', 'tax_tag_ids', 'analytic_distribution'}
                and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING):
            raise AccessError(_('Use a native supplier credit to correct posted project payables.'))
        return super().write(values)

    def unlink(self):
        if self.move_id.filtered('jakh_actual_feed_id'):
            raise AccessError(_('Resource-cost journal lines cannot be deleted.'))
        if (self.move_id.filtered(lambda move: move.state == 'posted' and move._jakh_has_project_payable_source())
                and self.env.context.get('_jakh_actual_posting') is not _ACTUAL_POSTING):
            raise AccessError(_('Use a native supplier credit to correct posted project payables.'))
        return super().unlink()


class ProjectControlPosting(models.Model):
    _inherit = 'jakh.project.control.entry'

    account_move_line_id = fields.Many2one('account.move.line', readonly=True, check_company=True, ondelete='restrict', string='Posted journal item')

    def _attach_posting(self, line):
        self.ensure_one(); self.check_access('read'); line.check_access('read')
        if line.move_id.state != 'posted' or line.company_id != self.company_id or not self.currency_id.is_zero(line.balance - self.amount):
            raise ValidationError(_('A project-cost fact must reconcile to its posted journal item.'))
        if self.account_move_line_id and self.account_move_line_id != line:
            raise ValidationError(_('A project-cost fact cannot be reassigned to another journal item.'))
        if not self.account_move_line_id:
            # Only posting provenance is associated here after caller access
            # and exact posted-balance checks. Financial facts remain locked.
            super(ProjectControlEntry, self.sudo()).write({'account_move_line_id': line.id})
        return True

    def action_open_source(self):
        self.ensure_one(); self.check_access('read')
        if self.account_move_line_id:
            return form_action(self.account_move_line_id.move_id)
        if self.source_model not in self.env:
            raise UserError(_('The source document is unavailable.'))
        return form_action(self.env[self.source_model].browse(self.source_id).exists())
