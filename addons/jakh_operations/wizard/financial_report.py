from collections import defaultdict
from decimal import Decimal
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from ..models.procurement import require_role, form_action
from ..services import aging_bucket


class FinancialReport(models.TransientModel):
    _name = 'jakh.financial.report'
    _description = 'Posted ledger reporting'
    name = fields.Char(compute='_compute_name')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    report_type = fields.Selection([('trial_balance', 'Trial balance'), ('general_ledger', 'General ledger'), ('profit_loss', 'P&L account movement'), ('balance_sheet', 'Balance sheet account balances'), ('receivable_aging', 'Receivable aging'), ('payable_aging', 'Payable aging')], required=True, default='trial_balance')
    date_from = fields.Date(default=lambda self: fields.Date.context_today(self).replace(month=1, day=1))
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    line_ids = fields.One2many('jakh.financial.report.line', 'wizard_id', readonly=True)
    scope_note = fields.Text(readonly=True)

    @api.depends('report_type', 'company_id', 'date_to')
    def _compute_name(self):
        labels = dict(self._fields['report_type']._description_selection(self.env))
        for row in self:
            row.name = '%s · %s' % (labels.get(row.report_type, self.env._('Financial report')), row.company_id.name or self.env.company.name)

    @api.model_create_multi
    def create(self, values):
        if any(set(value) & {'scope_note', 'line_ids'} for value in values):
            raise AccessError(_('Generate report output from posted journal items.'))
        return super().create(values)

    def write(self, values):
        if set(values) & {'line_ids', 'scope_note'}:
            raise AccessError(_('Ledger report output is generated from posted journal items.'))
        if set(values) & {'company_id', 'report_type', 'date_from', 'date_to'}:
            self.line_ids._clear_report()
            values = {**values, 'scope_note': _('Regenerate this report after changing its scope.')}
        return super().write(values)

    def action_generate(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one(); self.check_access('write')
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select this report company in the company switcher.'))
        if not self.date_to or self.date_from and self.date_from > self.date_to:
            raise UserError(_('Choose a valid report period.'))
        if self.report_type in ('general_ledger', 'profit_loss') and not self.date_from:
            raise UserError(_('Choose a period start for this report.'))
        domain = [('company_id', '=', self.company_id.id), ('parent_state', '=', 'posted'), ('date', '<=', self.date_to)]
        if self.report_type == 'profit_loss':
            domain += [('date', '>=', self.date_from), ('account_id.account_type', 'in', ['income', 'income_other', 'expense', 'expense_depreciation', 'expense_direct_cost'])]
        elif self.report_type == 'balance_sheet':
            domain += [('account_id.account_type', 'not in', ['income', 'income_other', 'expense', 'expense_depreciation', 'expense_direct_cost', 'off_balance'])]
        elif self.report_type in ('receivable_aging', 'payable_aging'):
            domain += [('account_id.account_type', '=', 'asset_receivable' if self.report_type == 'receivable_aging' else 'liability_payable')]
        items = self.env['account.move.line'].search(domain, order='date,id')
        values = []
        note = _('Posted journal items only, in company currency. Debit balances are positive; credit balances are negative. No currency conversion, cash-flow or statutory-statement inference is made.')
        if self.report_type in ('receivable_aging', 'payable_aging'):
            for item in items:
                # Historical company-currency residual reconstructed from partial
                # settlements dated on/before cutoff, rather than today's residual.
                residual = Decimal(str(item.balance))
                for partial in item.matched_credit_ids.filtered(lambda rec: rec.max_date <= self.date_to):
                    residual -= Decimal(str(partial.amount))
                for partial in item.matched_debit_ids.filtered(lambda rec: rec.max_date <= self.date_to):
                    residual += Decimal(str(partial.amount))
                if self.currency_id.is_zero(float(residual)):
                    continue
                values.append({'account_id': item.account_id.id, 'partner_id': item.partner_id.id, 'move_id': item.move_id.id,
                    'move_line_id': item.id, 'date': item.date, 'due_date': item.date_maturity,
                    'label': item.name or item.move_id.name, 'balance': float(residual),
                    'bucket': aging_bucket(item.date_maturity or None, self.date_to)})
            note += _(' Aging uses native due dates; missing dates stay Undated. Partial settlements after cutoff do not reduce historical balances. Advance control accounts outside AR/AP are not included.')
        elif self.report_type == 'general_ledger':
            for item in items.filtered(lambda rec: rec.date >= self.date_from):
                values.append({'account_id': item.account_id.id, 'partner_id': item.partner_id.id, 'move_id': item.move_id.id,
                    'move_line_id': item.id, 'date': item.date, 'label': item.name or item.move_id.name,
                    'debit': item.debit, 'credit': item.credit, 'balance': item.balance})
            note += _(' This view shows period transactions, not an invented running balance.')
        else:
            sums = defaultdict(lambda: {'opening': Decimal(0), 'debit': Decimal(0), 'credit': Decimal(0)})
            for item in items:
                row = sums[item.account_id.id]
                if self.report_type == 'trial_balance' and self.date_from and item.date < self.date_from:
                    row['opening'] += Decimal(str(item.balance))
                else:
                    row['debit'] += Decimal(str(item.debit)); row['credit'] += Decimal(str(item.credit))
            for account_id, row in sums.items():
                values.append({'account_id': account_id, 'label': self.env['account.account'].browse(account_id).display_name,
                    'opening': float(row['opening']), 'debit': float(row['debit']), 'credit': float(row['credit']),
                    'balance': float(row['opening'] + row['debit'] - row['credit'])})
            if self.report_type == 'balance_sheet':
                note += _(' Account balances as of cutoff; unclosed earnings are not automatically moved to equity. Use configured native closing entries and the statutory reporting package for a full balance-sheet statement.')
            elif self.report_type == 'profit_loss':
                note += _(' Income and expense account movements for the selected period; income credits appear negative.')
            else:
                note += _(' Opening is before the selected start; closing is opening plus period debit less period credit.')
        self.line_ids._clear_report()
        self.env['jakh.financial.report.line']._create_report([{**value, 'wizard_id': self.id} for value in values])
        super().write({'scope_note': note})
        self.env['jakh.audit.event']._append(self.company_id, 'ledger.report_generated', self, {'report_type': self.report_type, 'date_to': str(self.date_to), 'rows': len(values)})
        return {'type': 'ir.actions.act_window', 'res_model': 'jakh.financial.report', 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}

    def action_pivot(self):
        self.ensure_one(); self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Posted ledger analysis'), 'res_model': 'jakh.financial.report.line',
                'view_mode': 'pivot,list', 'domain': [('wizard_id', '=', self.id)], 'context': {'create': False}}


class FinancialReportLine(models.TransientModel):
    _name = 'jakh.financial.report.line'
    _description = 'Posted ledger report detail'
    wizard_id = fields.Many2one('jakh.financial.report', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='wizard_id.company_id')
    currency_id = fields.Many2one(related='company_id.currency_id')
    account_id = fields.Many2one('account.account', required=True)
    partner_id = fields.Many2one('res.partner')
    move_id = fields.Many2one('account.move')
    move_line_id = fields.Many2one('account.move.line')
    date = fields.Date()
    due_date = fields.Date()
    label = fields.Char()
    opening = fields.Monetary()
    debit = fields.Monetary()
    credit = fields.Monetary()
    balance = fields.Monetary()
    bucket = fields.Selection([('current', 'Not due'), ('1_30', '1–30'), ('31_60', '31–60'), ('61_90', '61–90'), ('over_90', 'Over 90'), ('undated', 'Undated')])

    @api.model_create_multi
    def create(self, values):
        raise AccessError(_('Generate ledger report rows through the report wizard.'))

    def _create_report(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        return super().create(values) if values else self.browse()

    def write(self, values):
        raise AccessError(_('Generated ledger rows are immutable.'))

    def unlink(self):
        # Odoo's private transient autovacuum removes expired rows in an
        # elevated environment. env.su is server authority, not caller context.
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Clear ledger output by regenerating its report.'))

    def _clear_report(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        return super().unlink()

    def action_drill(self):
        self.ensure_one(); self.check_access('read')
        if self.move_id:
            return form_action(self.move_id)
        domain = [('company_id', '=', self.company_id.id), ('parent_state', '=', 'posted'), ('account_id', '=', self.account_id.id), ('date', '<=', self.wizard_id.date_to)]
        if self.wizard_id.report_type == 'profit_loss':
            domain.append(('date', '>=', self.wizard_id.date_from))
        return {'type': 'ir.actions.act_window', 'name': _('Posted journal items'), 'res_model': 'account.move.line', 'view_mode': 'list,form', 'domain': domain}
