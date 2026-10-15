"""Contract cash control and auditable final-account closure.

Cash evidence is a native posted invoice with reconciled receivable lines.
Workflow actions never assign accounting payment states or residual amounts.
"""
from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role
from .execution_controls import _has_server_defaults


# Identity comparison makes this capability unavailable through RPC context.
# It is scoped to native posting; customer payment reconciliation uses normal
# accounting methods and never needs permission to rewrite approved amounts.
_OWNER_FINANCE_POST = object()


def _owner_controlled(moves):
    return moves.filtered(lambda move: move.jakh_certificate_id or move.jakh_cash_document_id
                          or move.reversed_entry_id.jakh_certificate_id or move.reversed_entry_id.jakh_cash_document_id)


def _settled(invoice):
    if not invoice or invoice.state != 'posted' or not invoice.currency_id.is_zero(invoice.amount_residual):
        return False
    if invoice.reversal_move_ids.filtered(lambda row: row.state == 'posted'):
        return False
    if invoice.currency_id.is_zero(invoice.amount_total):
        return True
    receivable = invoice.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable')
    return bool(receivable) and all(line.reconciled for line in receivable)


def _reversal_reconciled(invoice):
    if not invoice or invoice.state != 'posted':
        return False
    reversals = invoice.reversal_move_ids.filtered(lambda row: row.state == 'posted')
    if not reversals or invoice.currency_id.compare_amounts(sum(abs(row.amount_total) for row in reversals), abs(invoice.amount_total)):
        return False
    receivables = (invoice | reversals).line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable')
    return bool(receivables) and all(line.reconciled for line in receivables)


class ProjectContractCash(models.Model):
    _inherit = 'jakh.project.contract'

    cash_document_ids = fields.One2many('jakh.contract.cash.document', 'contract_id', string='Advances and retention releases', groups=FINANCIAL)
    final_account_ids = fields.One2many('jakh.project.final.account', 'contract_id', string='Final accounts', groups=FINANCIAL)
    settled_advance = fields.Monetary(compute='_compute_cash_balances', groups=FINANCIAL)
    retention_held = fields.Monetary(compute='_compute_cash_balances', groups=FINANCIAL)
    retention_released = fields.Monetary(compute='_compute_cash_balances', groups=FINANCIAL)

    @api.depends('cash_document_ids.invoice_id.state', 'cash_document_ids.invoice_id.amount_residual', 'cash_document_ids.amount', 'cash_document_ids.state')
    def _compute_cash_balances(self):
        for contract in self:
            documents = contract.cash_document_ids.filtered(lambda row: row.state == 'invoiced')
            contract.settled_advance = sum(documents.filtered(lambda row: row.kind == 'advance' and _settled(row.invoice_id)).mapped('amount'))
            certificates = self.env['jakh.payment.certificate'].search([('contract_id', '=', contract.id), ('state', '=', 'invoiced'), ('invoice_id.state', '=', 'posted')])
            contract.retention_held = sum(certificates.mapped('retention_amount'))
            contract.retention_released = sum(documents.filtered(lambda row: row.kind == 'retention' and row.invoice_id.state == 'posted').mapped('amount'))

    def _jakh_recoverable_advance_cap(self):
        self.ensure_one()
        documents = self.env['jakh.contract.cash.document'].search([
            ('contract_id', '=', self.id), ('kind', '=', 'advance'), ('state', '=', 'invoiced'),
        ])
        received = sum(documents.filtered(lambda row: _settled(row.invoice_id)).mapped('amount'))
        return min(self.advance_cap, received)

    def action_open_certificates(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Payment certificates'), 'res_model': 'jakh.payment.certificate',
                'view_mode': 'list,form', 'domain': [('contract_id', '=', self.id)], 'context': {'default_contract_id': self.id}}

    def action_open_cash_documents(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Advances and retention releases'), 'res_model': 'jakh.contract.cash.document',
                'view_mode': 'list,form', 'domain': [('contract_id', '=', self.id)], 'context': {'default_contract_id': self.id}}

    def action_open_final_accounts(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Final accounts'), 'res_model': 'jakh.project.final.account',
                'view_mode': 'list,form', 'domain': [('contract_id', '=', self.id)], 'context': {'default_contract_id': self.id}}


class ContractCashDocument(models.Model):
    _name = 'jakh.contract.cash.document'
    _description = 'Controlled contract advance or retention release'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'date desc, id desc'

    name = fields.Char(required=True, tracking=True)
    contract_id = fields.Many2one('jakh.project.contract', required=True, check_company=True, ondelete='restrict', tracking=True)
    project_id = fields.Many2one(related='contract_id.project_id', store=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one(related='contract_id.partner_id', store=True)
    kind = fields.Selection([('advance', 'Customer advance'), ('retention', 'Retention release')], required=True, tracking=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    amount = fields.Monetary(required=True, groups=FINANCIAL, tracking=True)
    reason = fields.Text(required=True, tracking=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('invoiced', 'Invoice created'), ('cancelled', 'Cancelled')], required=True, default='draft', readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    invoice_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True)
    invoice_state = fields.Selection(related='invoice_id.state')
    payment_state = fields.Selection(related='invoice_id.payment_state')
    settled = fields.Boolean(compute='_compute_settled', string='Receivable reconciled')
    _server_fields = {'state', 'approved_by', 'approved_at', 'invoice_id'}

    @api.depends('invoice_id.state', 'invoice_id.amount_residual', 'invoice_id.line_ids.reconciled')
    def _compute_settled(self):
        for row in self:
            row.settled = _settled(row.invoice_id)

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields for values in values_list)):
            raise AccessError(_('Cash document workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the contract cash document actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Submitted cash documents are immutable. Cancel an unposted document and create its replacement.'))
        if 'contract_id' in values:
            raise AccessError(_('Cash documents cannot move between contracts.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft cash documents can be deleted.'))
        return super().unlink()

    @api.constrains('amount', 'reason')
    def _check_amount(self):
        for row in self:
            if row.amount <= 0 or not (row.reason or '').strip():
                raise ValidationError(_('A cash document needs a positive amount and a documented basis.'))

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'contract_cash.' + row.state, row,
                                                {'kind': row.kind, 'amount': row.amount, 'invoice_id': row.invoice_id.id})
        return result

    def _lock_scope(self):
        lock_records(self)
        self.contract_id._locked()
        if self.contract_id.state != 'active':
            raise UserError(_('Contract cash actions require an active contract.'))

    def action_submit(self):
        for row in self:
            row._lock_scope()
            if row.state != 'draft':
                raise UserError(_('Submit a draft contract cash document.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            row._lock_scope()
            if row.state in ('approved', 'invoiced'):
                continue
            if row.state != 'submitted':
                raise UserError(_('Approve a submitted contract cash document.'))
            contract = row.contract_id
            account = contract.advance_account_id if row.kind == 'advance' else contract.retention_account_id
            required_types = ('liability_current', 'liability_non_current') if row.kind == 'advance' else ('asset_current', 'asset_non_current')
            if not account or account.account_type not in required_types or not contract.journal_id:
                raise UserError(_('Configure the approved liability account for advances or asset control account for retention releases.'))
            reserved = self.search([('contract_id', '=', contract.id), ('kind', '=', row.kind), ('state', 'in', ['approved', 'invoiced']), ('id', '!=', row.id)])
            if row.kind == 'advance':
                ceiling = contract.advance_cap
            else:
                certificates = self.env['jakh.payment.certificate'].search([('contract_id', '=', contract.id), ('state', '=', 'invoiced'), ('invoice_id.state', '=', 'posted')])
                ceiling = sum(certificates.mapped('retention_amount'))
            if contract.currency_id.compare_amounts(sum(reserved.mapped('amount')) + row.amount, ceiling) > 0:
                raise UserError(_('The approved amount cannot exceed the contract advance ceiling or posted retention available for release.'))
            row._transition(state='approved', approved_by=self.env.uid, approved_at=fields.Datetime.now())
        return True

    def action_create_invoice(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one()
        self._lock_scope()
        if self.invoice_id:
            return form_action(self.invoice_id)
        if self.state != 'approved':
            raise UserError(_('Approve the cash document before creating its customer invoice.'))
        contract = self.contract_id
        account = contract.advance_account_id if self.kind == 'advance' else contract.retention_account_id
        taxes = contract.recovery_tax_ids if self.kind == 'advance' else contract.retention_tax_ids
        invoice = self.env['account.move']._create_jakh_cash({
            'move_type': 'out_invoice', 'company_id': self.company_id.id, 'partner_id': self.partner_id.id,
            'journal_id': contract.journal_id.id, 'currency_id': self.currency_id.id, 'invoice_date': self.date,
            'invoice_origin': self.name, 'jakh_cash_document_id': self.id,
            'invoice_line_ids': [Command.create({'name': self.name, 'quantity': 1, 'price_unit': self.amount,
                                                'account_id': account.id, 'tax_ids': [Command.set(taxes.ids)]})],
        })
        self._transition(state='invoiced', invoice_id=invoice.id)
        return form_action(invoice)

    def action_open_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_('Create the approved invoice first.'))
        return form_action(self.invoice_id)

    def action_register_payment(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_('Create and post the invoice before registering payment.'))
        return self.invoice_id.action_register_payment()

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            row._lock_scope()
            if row.state == 'cancelled':
                continue
            if row.invoice_id and row.invoice_id.state == 'posted' and not _reversal_reconciled(row.invoice_id):
                raise UserError(_('Reconcile a full native accounting reversal before cancelling a posted cash document.'))
            row._transition(state='cancelled')
            if row.invoice_id and row.invoice_id.state == 'draft':
                row.invoice_id.button_cancel()
        return True


class AccountMoveContractCash(models.Model):
    _inherit = 'account.move'

    jakh_cash_document_id = fields.Many2one('jakh.contract.cash.document', readonly=True, copy=False, check_company=True)
    _unique_contract_cash = models.Constraint('UNIQUE(jakh_cash_document_id)', 'A contract cash document can have only one native invoice.')

    @api.model_create_multi
    def create(self, values_list):
        if (_has_server_defaults(self.env, {'jakh_cash_document_id'})
                or any(values.get('jakh_cash_document_id') for values in values_list)):
            raise AccessError(_('Create cash invoices through their approved contract document.'))
        return super().create(values_list)

    def _create_jakh_cash(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        return super().create(values)

    def write(self, values):
        if 'jakh_cash_document_id' in values:
            raise AccessError(_('The contract cash invoice link is immutable.'))
        native_post = self.env.context.get('_jakh_owner_finance_post') is _OWNER_FINANCE_POST
        controlled = _owner_controlled(self)
        if controlled and not native_post:
            if 'state' in values:
                raise AccessError(_('Contract invoice states are assigned by native posting and reversal actions.'))
            protected = {
                'journal_id', 'company_id', 'currency_id', 'date', 'invoice_date', 'move_type', 'partner_id',
                'invoice_payment_term_id', 'fiscal_position_id', 'line_ids', 'invoice_line_ids',
                'invoice_cash_rounding_id', 'invoice_currency_rate', 'reversed_entry_id',
            }
            if controlled.filtered(lambda move: move.state == 'posted') and set(values) & protected:
                raise AccessError(_('Posted contract invoices and credits preserve their approved financial identity. Use a native reversal.'))
        return super().write(values)

    def _post(self, soft=True):
        for credit in self.filtered(lambda row: row.reversed_entry_id.jakh_certificate_id or row.reversed_entry_id.jakh_cash_document_id):
            original = credit.reversed_entry_id
            lock_records(original, touch=True)
            contract = original.jakh_certificate_id.contract_id or original.jakh_cash_document_id.contract_id
            contract._locked()
            if contract.state != 'active':
                raise UserError(_('Reopen the final account with an approved reason before reversing a closed contract invoice.'))
            if original.jakh_certificate_id and self.env['jakh.payment.certificate'].search_count([
                    ('contract_id', '=', contract.id), ('state', 'in', ['approved', 'invoiced']),
                    ('approval_sequence', '>', original.jakh_certificate_id.approval_sequence)]):
                raise UserError(_('Reverse and acknowledge later certificates before correcting an earlier cumulative measurement.'))
            if original.jakh_cash_document_id.kind == 'advance' and sum(self.env['jakh.payment.certificate'].search([
                    ('contract_id', '=', contract.id), ('state', 'in', ['approved', 'invoiced'])]).mapped('recovery_amount')):
                raise UserError(_('Reverse the affected recovery certificates before reversing an advance already recovered.'))
            posted_reversals = original.reversal_move_ids.filtered(lambda row: row.state == 'posted' and row != credit)
            if posted_reversals:
                raise UserError(_('A controlled contract invoice can have only one posted full reversal.'))
            expected_type = 'out_refund' if original.move_type == 'out_invoice' else 'out_invoice'
            def signature(invoice):
                return sorted((line.account_id.id, line.quantity, line.price_unit, line.discount, tuple(sorted(line.tax_ids.ids)))
                              for line in invoice.invoice_line_ids.filtered(lambda row: row.display_type == 'product'))
            if (original.state != 'posted' or credit.move_type != expected_type or credit.company_id != original.company_id
                    or credit.partner_id != original.partner_id or credit.currency_id != original.currency_id
                    or credit.journal_id != original.journal_id or signature(credit) != signature(original)):
                raise UserError(_('A contract reversal must mirror its posted source customer, company, currency, accounts, quantities, prices and taxes.'))
        for invoice in self.filtered('jakh_cash_document_id'):
            document = invoice.jakh_cash_document_id
            contract = document.contract_id
            document._lock_scope()
            account = contract.advance_account_id if document.kind == 'advance' else contract.retention_account_id
            taxes = contract.recovery_tax_ids if document.kind == 'advance' else contract.retention_tax_ids
            lines = invoice.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
            if (document.state != 'invoiced' or document.invoice_id != invoice or invoice.move_type != 'out_invoice'
                    or invoice.company_id != document.company_id or invoice.partner_id != document.partner_id
                    or invoice.currency_id != document.currency_id or invoice.journal_id != contract.journal_id
                    or invoice.invoice_date != document.date or len(lines) != 1
                    or lines.account_id != account or lines.quantity != 1 or lines.discount
                    or document.currency_id.compare_amounts(lines.price_unit, document.amount)
                    or set(lines.tax_ids.ids) != set(taxes.ids)):
                raise UserError(_('The cash invoice must match its approved amount, customer, date, control account and taxes before posting.'))
        result = super(AccountMoveContractCash, self.with_context(_jakh_owner_finance_post=_OWNER_FINANCE_POST))._post(soft=soft)
        Entry = self.env['jakh.project.control.entry']
        for credit in self.filtered(lambda row: row.state == 'posted' and not row.jakh_certificate_id and row.reversed_entry_id.jakh_certificate_id):
            original = credit.reversed_entry_id
            certificate = original.jakh_certificate_id
            facts = Entry.search([('source_model', '=', 'account.move'), ('source_id', '=', original.id), ('entry_type', '=', 'billed_revenue')])
            for fact in facts:
                Entry._record(name=credit.name, company=credit.company_id, project=fact.project_id, wbs=fact.wbs_id,
                              entry_type='billed_revenue', amount=-fact.amount, source_model=credit._name, source_id=credit.id,
                              source_key='account.move:%s:reversed_billed:%s' % (credit.id, fact.id), entry_date=credit.date,
                              partner=credit.partner_id, contract=certificate.contract_id, contract_line=fact.contract_line_id,
                              location=fact.location_id, discipline=fact.discipline_id,
                              notes=_('Native credit reverses approved certificate billing'))
        return result.with_env(self.env)

    def button_draft(self):
        for move in _owner_controlled(self):
            if move.state == 'posted':
                raise UserError(_('Posted contract invoices are corrected through native reversals, preserving their approved source.'))
        return super(AccountMoveContractCash, self.with_context(_jakh_owner_finance_post=_OWNER_FINANCE_POST)).button_draft()

    def button_cancel(self):
        if _owner_controlled(self).filtered(lambda row: row.state == 'posted'):
            raise UserError(_('Posted contract invoices are corrected through native reversals, preserving their approved source.'))
        return super(AccountMoveContractCash, self.with_context(_jakh_owner_finance_post=_OWNER_FINANCE_POST)).button_cancel()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        for move in self.filtered(lambda row: row.jakh_cash_document_id or row.jakh_certificate_id):
            contract = move.jakh_cash_document_id.contract_id or move.jakh_certificate_id.contract_id
            if contract.state == 'closed':
                raise UserError(_('Reopen the final account with an approved reason before reversing a closed contract invoice.'))
            if move.jakh_certificate_id:
                certificate = move.jakh_certificate_id
                later = self.env['jakh.payment.certificate'].search_count([
                    ('contract_id', '=', contract.id), ('state', 'in', ['approved', 'invoiced']),
                    ('approval_sequence', '>', certificate.approval_sequence),
                ])
                if later:
                    raise UserError(_('Reverse and acknowledge later certificates before correcting an earlier cumulative measurement.'))
            if move.jakh_cash_document_id.kind == 'advance':
                recovered = sum(self.env['jakh.payment.certificate'].search([('contract_id', '=', contract.id), ('state', 'in', ['approved', 'invoiced'])]).mapped('recovery_amount'))
                if recovered:
                    raise UserError(_('Reverse the affected recovery certificates before reversing an advance already recovered.'))
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)


class OwnerContractInvoiceLineSeal(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, values_list):
        default_move = self.default_get(['move_id']).get('move_id') if any('move_id' not in values for values in values_list) else False
        moves = self.env['account.move'].browse({values.get('move_id', default_move) for values in values_list if values.get('move_id', default_move)})
        if (self.env.context.get('_jakh_owner_finance_post') is not _OWNER_FINANCE_POST
                and _owner_controlled(moves).filtered(lambda move: move.state == 'posted')):
            raise AccessError(_('New journal items cannot be appended to posted contract invoices or credits.'))
        return super().create(values_list)

    def write(self, values):
        financial = {
            'move_id', 'account_id', 'partner_id', 'product_id', 'product_uom_id', 'quantity', 'price_unit', 'discount',
            'debit', 'credit', 'balance', 'amount_currency', 'currency_id', 'tax_ids', 'tax_line_id',
            'tax_repartition_line_id', 'tax_tag_ids', 'display_type', 'date', 'date_maturity',
        }
        moves = self.move_id
        if values.get('move_id'):
            moves |= self.env['account.move'].browse(values['move_id'])
        if (set(values) & financial and self.env.context.get('_jakh_owner_finance_post') is not _OWNER_FINANCE_POST
                and _owner_controlled(moves).filtered(lambda move: move.state == 'posted')):
            raise AccessError(_('Posted contract journal item amounts and accounts require a native credit for correction.'))
        return super().write(values)

    def unlink(self):
        if (self.env.context.get('_jakh_owner_finance_post') is not _OWNER_FINANCE_POST
                and _owner_controlled(self.move_id).filtered(lambda move: move.state == 'posted')):
            raise AccessError(_('Posted contract journal items cannot be deleted.'))
        return super().unlink()


class ProjectFinalAccount(models.Model):
    _name = 'jakh.project.final.account'
    _description = 'Controlled contract final account and project closure'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'id desc'

    name = fields.Char(required=True, tracking=True)
    contract_id = fields.Many2one('jakh.project.contract', required=True, check_company=True, ondelete='restrict')
    project_id = fields.Many2one(related='contract_id.project_id', store=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    reason = fields.Text(required=True, tracking=True)
    reopen_reason = fields.Text(tracking=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('closed', 'Closed'), ('reopened', 'Reopened')], required=True, default='draft', readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    closed_by = fields.Many2one('res.users', readonly=True)
    closed_at = fields.Datetime(readonly=True)
    certified_gross = fields.Monetary(readonly=True, groups=FINANCIAL)
    recovered_advance = fields.Monetary(readonly=True, groups=FINANCIAL)
    released_retention = fields.Monetary(readonly=True, groups=FINANCIAL)
    _server_fields = {'state', 'approved_by', 'approved_at', 'closed_by', 'closed_at', 'certified_gross', 'recovered_advance', 'released_retention'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields for values in values_list)):
            raise AccessError(_('Final-account workflow values are assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if set(values) & self._server_fields:
            raise AccessError(_('Use the final-account approval and closure actions.'))
        if self.filtered(lambda row: row.state != 'draft') and set(values) - {'reopen_reason'}:
            raise UserError(_('Submitted final accounts are immutable; reopen a closed account for a documented correction.'))
        if 'contract_id' in values:
            raise AccessError(_('Final accounts cannot move between contracts.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft final accounts can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, 'final_account.' + row.state, row, {
                'contract_id': row.contract_id.id, 'certified_gross': row.certified_gross,
                'reopen_reason': row.reopen_reason or '',
            })
        return result

    def _check_complete(self):
        self.ensure_one()
        contract = self.contract_id
        contract._locked()
        project = self.project_id
        lock_records(project, touch=True)
        if contract.state != 'active':
            raise UserError(_('Final-account approval requires an active contract.'))
        certificates = self.env['jakh.payment.certificate'].search([('contract_id', '=', contract.id), ('state', '!=', 'cancelled')], order='approval_sequence')
        if not certificates or certificates.filtered(lambda row: row.state != 'invoiced' or not _settled(row.invoice_id)):
            raise UserError(_('Every contract certificate must have a posted invoice and reconciled receivable before final closure.'))
        latest = certificates[-1]
        measurements = {line.contract_line_id.id: line.cumulative_quantity for line in latest.line_ids}
        if any(abs(measurements.get(line.id, 0) - line._jakh_measurement_limit()) > 1e-6 for line in contract.line_ids):
            raise UserError(_('Complete the cumulative measurement of every approved BOQ item before final closure.'))
        variations = self.env['jakh.project.variation'].search([('contract_id', '=', contract.id), ('variation_type', '=', 'owner'), ('state', 'not in', ['rejected'])])
        measured_variations = {line.variation_id.id: line.cumulative_quantity for line in latest.variation_line_ids}
        if variations.filtered(lambda row: not row.boq_scope_omission and (row.state != 'certified' or abs(measured_variations.get(row.id, 0) - row.quantity) > 1e-6)):
            raise UserError(_('Approve and fully certify every owner variation before final closure.'))
        documents = contract.cash_document_ids.filtered(lambda row: row.state != 'cancelled')
        if documents.filtered(lambda row: row.state != 'invoiced' or not _settled(row.invoice_id)):
            raise UserError(_('Every advance and retention release must have a posted, reconciled customer invoice before final closure.'))
        advance = sum(documents.filtered(lambda row: row.kind == 'advance').mapped('amount'))
        recovered = sum(certificates.mapped('recovery_amount'))
        retention = sum(certificates.mapped('retention_amount'))
        released = sum(documents.filtered(lambda row: row.kind == 'retention').mapped('amount'))
        if not self.currency_id.is_zero(advance - recovered):
            raise UserError(_('Recover the received advance in full before final closure.'))
        if not self.currency_id.is_zero(retention - released):
            raise UserError(_('Release and collect all contract retention before final closure.'))
        source_invoices = certificates.invoice_id | documents.invoice_id
        if source_invoices.filtered(lambda row: row.reversal_move_ids.filtered(lambda reversal: reversal.state == 'posted')):
            raise UserError(_('Resolve reversed contract invoices and correcting measurements before final closure.'))
        checks = [
            ('jakh.contract.boq.revision', [('contract_id.project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.material.request', [('project_id', '=', project.id), ('state', 'not in', ['done', 'cancelled'])]),
            ('jakh.material.return', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted', 'approved'])]),
            ('jakh.project.variation', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted', 'priced'])]),
            ('jakh.purchase.request', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted', 'approved'])]),
            ('jakh.subcontract.service.entry', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.subcontract.service.reversal', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.subcontract.amendment', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.project.actual.feed', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.project.schedule.progress', [('project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.project.budget.revision', [('wbs_id.project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.project.forecast', [('wbs_id.project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('jakh.assurance.case', [('project_id', '=', project.id), ('state', 'not in', ['closed', 'cancelled'])]),
            ('jakh.document.revision', [('document_id.project_id', '=', project.id), ('state', 'in', ['draft', 'submitted'])]),
            ('project.task', [('project_id', '=', project.id), ('state', 'not in', ['1_done', '1_canceled'])]),
            ('jakh.resource.assignment', [('project_id', '=', project.id), ('state', 'in', ['draft', 'approved'])]),
        ]
        for model_name, domain in checks:
            if model_name in self.env.registry and self.env[model_name].search_count(domain):
                raise UserError(_('Resolve pending project work, approvals, field actions and document reviews before final closure.'))
        requests = self.env['jakh.purchase.request'].search([('project_id', '=', project.id), ('state', '=', 'released')])
        purchase_lines = requests.purchase_order_id.order_line.filtered(lambda line: not line.display_type)
        for line in purchase_lines:
            if line.order_id.state not in ('purchase', 'done'):
                raise UserError(_('Complete or cancel every released purchase order before final closure.'))
            received = line.qty_received
            if line.jakh_requires_ses:
                # Corrections reduce both accepted scope and posted billing.
                # Historical original SES rows are evidence, not net receipts.
                received = float(line._jakh_service_totals()[0])
            if received + 1e-6 < line.product_qty or line.qty_invoiced + 1e-6 < line.product_qty:
                raise UserError(_('Receive or accept and invoice every committed purchase quantity before final closure.'))
        vendor_invoices = purchase_lines.invoice_lines.move_id.filtered(lambda row: row.move_type in ('in_invoice', 'in_refund') and row.state != 'cancel')
        if vendor_invoices.filtered(lambda row: row.state != 'posted' or not row.currency_id.is_zero(row.amount_residual)):
            raise UserError(_('Post and settle every project supplier invoice before final closure.'))
        if 'account_move_id' in self.env['jakh.project.actual.feed']._fields:
            accepted_feeds = self.env['jakh.project.actual.feed'].search([('project_id', '=', project.id), ('state', '=', 'accepted')])
            if accepted_feeds.filtered(lambda row: not row.account_move_id or row.account_move_id.state != 'posted'):
                raise UserError(_('Post the native accounting entry for every accepted labour and plant actual before final closure.'))
        return {'certified_gross': latest.cumulative_gross, 'recovered_advance': recovered, 'released_retention': released}

    def action_submit(self):
        for row in self:
            lock_records(row)
            row.contract_id._locked()
            if row.state != 'draft' or row.contract_id.state != 'active' or not (row.reason or '').strip():
                raise UserError(_('Submit a draft final account with a documented settlement basis against an active contract.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            lock_records(row)
            if row.state == 'approved':
                continue
            if row.state != 'submitted':
                raise UserError(_('Approve a submitted final account.'))
            values = row._check_complete()
            row._transition(state='approved', approved_by=self.env.uid, approved_at=fields.Datetime.now(), **values)
        return True

    def action_close(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            lock_records(row)
            if row.state == 'closed':
                continue
            if row.state != 'approved':
                raise UserError(_('Approve the final account before closing the contract and project.'))
            row._check_complete()
            row.contract_id._transition(state='closed')
            remaining = self.env['jakh.project.contract'].search_count([('project_id', '=', row.project_id.id), ('state', '!=', 'closed')])
            if not remaining:
                row.project_id._jakh_set_closed(True)
            row._transition(state='closed', closed_by=self.env.uid, closed_at=fields.Datetime.now())
        return True

    def action_reopen(self):
        require_role(self.env, ['group_manager'])
        for row in self:
            lock_records(row)
            row.contract_id._locked()
            if row.state != 'closed' or not (row.reopen_reason or '').strip():
                raise UserError(_('A closed final account and documented correction reason are required to reopen.'))
            row.contract_id._transition(state='active')
            row.project_id._jakh_set_closed(False)
            row._transition(state='reopened')
        return True

    def action_open_contract(self):
        self.ensure_one()
        return form_action(self.contract_id)

    def action_open_project(self):
        self.ensure_one()
        return form_action(self.project_id.with_context(active_test=False))


class CertificatePaymentNavigation(models.Model):
    _inherit = 'jakh.payment.certificate'

    state = fields.Selection(selection_add=[('cancelled', 'Reversed / cancelled')], ondelete={'cancelled': 'set default'})

    def action_confirm_reversal(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        Entry = self.env['jakh.project.control.entry']
        for certificate in self:
            lock_records(certificate)
            certificate.contract_id._locked()
            if certificate.state == 'cancelled':
                continue
            if certificate.contract_id.state != 'active' or certificate.state != 'invoiced' or not _reversal_reconciled(certificate.invoice_id):
                raise UserError(_('Reconcile a full native credit against the invoice before acknowledging the certificate reversal.'))
            later = self.search_count([('contract_id', '=', certificate.contract_id.id), ('state', 'in', ['approved', 'invoiced']), ('approval_sequence', '>', certificate.approval_sequence)])
            if later:
                raise UserError(_('Reverse and acknowledge later certificates before correcting an earlier cumulative measurement.'))
            facts = Entry.search([('source_model', '=', certificate._name), ('source_id', '=', certificate.id), ('entry_type', '=', 'certified_revenue')])
            reversal = certificate.invoice_id.reversal_move_ids.filtered(lambda row: row.state == 'posted')[:1]
            for fact in facts:
                Entry._record(name=certificate.name, company=certificate.company_id, project=fact.project_id, wbs=fact.wbs_id,
                              entry_type='certified_revenue', amount=-fact.amount, source_model=reversal._name, source_id=reversal.id,
                              source_key='account.move:%s:reversed_certified:%s' % (reversal.id, fact.id), entry_date=reversal.date,
                              partner=certificate.contract_id.partner_id, contract=certificate.contract_id, contract_line=fact.contract_line_id,
                              location=fact.location_id, discipline=fact.discipline_id,
                              notes=_('Verified native credit reverses approved measurement'))
            for variation in certificate.variation_line_ids.variation_id.filtered(lambda row: row.state == 'certified'):
                variation._transition(state='approved')
            certificate._transition(state='cancelled')
        return True

    def action_register_payment(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_('Create and post the invoice before registering payment.'))
        return self.invoice_id.action_register_payment()

    def action_open_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_('Create the approved invoice first.'))
        return form_action(self.invoice_id)
