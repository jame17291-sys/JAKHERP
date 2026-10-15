"""Subcontract retention/advance controls derived from native AP journals."""
from decimal import Decimal

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import FINANCIAL, form_action, lock_records, require_role, creation_parents
from .execution_controls import SubcontractServiceEntry as OriginalServiceEntry, _has_server_defaults
from .subcontract_workflow import AccountMove as ExistingSubcontractMove


_CONTROL_LINE_CREATION = object()
_FINANCE_POSTING = object()


def _amount(value):
    return Decimal(str(value or 0))


def _reversed_and_reconciled(invoice):
    if not invoice or invoice.state != 'posted':
        return False
    credits = invoice.reversal_move_ids.filtered(lambda move: move.state == 'posted')
    payables = (invoice | credits).line_ids.filtered(lambda line: line.account_id.account_type == 'liability_payable')
    return bool(credits and payables) and invoice.currency_id.is_zero(invoice.amount_residual) and all(payables.mapped('reconciled'))


class SubcontractCashWorkflow(models.AbstractModel):
    _name = 'jakh.subcontract.ap.workflow'
    _description = 'Controlled subcontract finance workflow'

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_finance'])
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(values) & self._server_fields for values in values_list)):
            raise AccessError(_('Subcontract finance values are assigned by controlled actions.'))
        records = super().create(values_list)
        records.project_id._jakh_assert_open()
        return records

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        if set(values) & (self._server_fields | self._scope_fields):
            raise AccessError(_('Use the controlled subcontract finance actions.'))
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Approved subcontract finance documents are immutable.'))
        return super().write(values)

    def unlink(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Only draft subcontract finance documents can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.project_id._jakh_assert_open()
        self.check_access('write')
        result = super().write(values)
        for row in self:
            self.env['jakh.audit.event']._append(row.company_id, row._name + '.' + row.state, row, {'state': row.state})
        return result


class SubcontractFinanceAgreement(models.Model):
    _name = 'jakh.subcontract.finance.agreement'
    _description = 'Subcontract finance agreement'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.subcontract.ap.workflow']
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    purchase_order_id = fields.Many2one('purchase.order', required=True, check_company=True, tracking=True)
    project_id = fields.Many2one(related='purchase_order_id.jakh_request_id.project_id', store=True)
    wbs_id = fields.Many2one(related='purchase_order_id.jakh_request_id.wbs_id', store=True)
    company_id = fields.Many2one(related='purchase_order_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one(related='purchase_order_id.partner_id', store=True)
    journal_id = fields.Many2one('account.journal', required=True, check_company=True, domain=[('type', '=', 'purchase')])
    retention_percent = fields.Float(default=0)
    retention_cap = fields.Monetary(default=0)
    advance_percent = fields.Float(default=0)
    advance_cap = fields.Monetary(default=0)
    recovery_percent = fields.Float(default=0)
    retention_account_id = fields.Many2one('account.account', check_company=True)
    advance_account_id = fields.Many2one('account.account', check_company=True)
    certificate_ids = fields.One2many('jakh.subcontract.payment.certificate', 'agreement_id', readonly=True)
    cash_document_ids = fields.One2many('jakh.subcontract.cash.document', 'agreement_id', readonly=True)
    retention_balance = fields.Monetary(compute='_compute_balances')
    advance_balance = fields.Monetary(compute='_compute_balances')
    state = fields.Selection([('draft', 'Finance configuration'), ('active', 'Active'), ('closed', 'Closed')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _server_fields = {'state', 'approved_by', 'approved_at', 'retention_balance', 'advance_balance'}
    _scope_fields = {'purchase_order_id'}
    _one_order = models.Constraint('UNIQUE(purchase_order_id)', 'Configure one finance agreement per subcontract order.')

    @api.constrains('retention_percent', 'advance_percent', 'recovery_percent', 'retention_cap', 'advance_cap')
    def _check_limits(self):
        if self.filtered(lambda row: any(not 0 <= rate <= 100 for rate in (row.retention_percent, row.advance_percent, row.recovery_percent))
                         or row.retention_cap < 0 or row.advance_cap < 0):
            raise ValidationError(_('Use non-negative subcontract caps and percentages from zero to 100.'))

    def _posted_control_lines(self):
        self.ensure_one(); self.check_access('read')
        candidates = self.env['account.move.line'].search([
            ('jakh_subcontract_agreement_id', '=', self.id), ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
            ('jakh_subcontract_control', 'in', ['retention', 'recovery', 'advance', 'release']),
        ])
        def approved_source(line):
            move = line.move_id
            source = move._jakh_subcontract_finance_source()
            original_source = move.reversed_entry_id._jakh_subcontract_finance_source() if move.reversed_entry_id else False
            return bool((source and source.agreement_id == self and source.invoice_id == move)
                        or (original_source and original_source.agreement_id == self
                            and original_source.invoice_id == move.reversed_entry_id))
        return candidates.filtered(approved_source)

    def _account_balances(self):
        self.ensure_one()
        lines = self._posted_control_lines()
        retention = -sum((_amount(line.balance) for line in lines.filtered(
            lambda item: item.jakh_subcontract_control in ('retention', 'release'))), Decimal())
        advance = sum((_amount(line.balance) for line in lines.filtered(
            lambda item: item.jakh_subcontract_control in ('advance', 'recovery'))), Decimal())
        return retention, advance

    def _compute_balances(self):
        for row in self:
            retention, advance = row._account_balances() if row.id else (0, 0)
            row.retention_balance = float(retention); row.advance_balance = float(advance)

    def _advance_limit(self):
        self.ensure_one()
        return min(_amount(self.advance_cap), _amount(self.purchase_order_id.amount_untaxed) * _amount(self.advance_percent) / 100)

    def _available_paid_advance(self):
        """A payable credit alone is not evidence of money advanced."""
        self.ensure_one()
        paid_advance_moves = self.cash_document_ids.filtered(lambda doc: doc.document_type == 'advance'
            and doc.invoice_id.state == 'posted' and doc.invoice_id.currency_id.is_zero(doc.invoice_id.amount_residual)
            and doc.invoice_id._get_reconciled_payments().filtered(lambda payment: payment.state in ('paid', 'reconciled'))).invoice_id
        lines = self._posted_control_lines()
        funded = sum((_amount(line.balance) for line in lines.filtered(lambda line:
            line.jakh_subcontract_control == 'advance' and (line.move_id in paid_advance_moves
            or line.move_id.reversed_entry_id in paid_advance_moves))), Decimal())
        recovered = -sum((_amount(line.balance) for line in lines.filtered(
            lambda line: line.jakh_subcontract_control == 'recovery')), Decimal())
        return max(funded - recovered, Decimal())

    def _pending_certificates(self):
        return self.certificate_ids.filtered(lambda cert: cert.state in ('approved', 'billed') and cert.invoice_id.state != 'posted')

    def _withheld_retention(self):
        return -sum((_amount(line.balance) for line in self._posted_control_lines().filtered(
            lambda line: line.jakh_subcontract_control == 'retention')), Decimal())

    def action_activate(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row, touch=True)
            order = row.purchase_order_id
            if row.state == 'active':
                continue
            if (row.state != 'draft' or order.state not in ('purchase', 'done') or not order.jakh_request_id
                    or not order.order_line._jakh_service_lines() or row.company_id not in self.env.companies
                    or order.currency_id != row.currency_id or row.journal_id.company_id != row.company_id
                    or row.journal_id.type != 'purchase'):
                raise UserError(_('Approve subcontract finance terms for a confirmed company-currency project order and purchase journal.'))
            if row.retention_percent and (row.retention_cap <= 0 or row.retention_account_id.account_type != 'liability_current'):
                raise UserError(_('Configure a retention cap and current-liability control account.'))
            if row.advance_percent or row.recovery_percent or row.advance_cap:
                if (row.advance_percent <= 0 or row.advance_cap <= 0 or row.recovery_percent <= 0
                        or row.advance_account_id.account_type not in ('asset_prepayments', 'asset_current', 'asset_non_current')):
                    raise UserError(_('Configure the approved advance percentage, cap, recovery percentage and prepaid-asset control account.'))
            if order.invoice_ids.filtered(lambda move: move.state == 'posted'):
                raise UserError(_('Approve subcontract finance terms before posting the first supplier invoice.'))
            row._transition(state='active', approved_by=self.env.user.id, approved_at=fields.Datetime.now())
        return True

    def action_prepare_advance(self):
        self.ensure_one(); require_role(self.env, ['group_manager', 'group_finance'])
        if self.state != 'active' or self._advance_limit() <= 0:
            raise UserError(_('An active agreement with an approved advance ceiling is required.'))
        return {'type': 'ir.actions.act_window', 'name': self.env._('Subcontract advance'),
                'res_model': 'jakh.subcontract.cash.document', 'view_mode': 'form', 'target': 'current',
                'context': {'default_agreement_id': self.id, 'default_document_type': 'advance'}}

    def action_prepare_retention_release(self):
        self.ensure_one(); require_role(self.env, ['group_manager', 'group_finance'])
        balance, _advance = self._account_balances()
        if self.state != 'active' or balance <= 0:
            raise UserError(_('An active agreement with posted retention held is required.'))
        return {'type': 'ir.actions.act_window', 'name': self.env._('Subcontract retention release'),
                'res_model': 'jakh.subcontract.cash.document', 'view_mode': 'form', 'target': 'current',
                'context': {'default_agreement_id': self.id, 'default_document_type': 'release', 'default_amount': float(balance)}}

    def _closing_obstacles(self):
        self.ensure_one()
        obstacles = []
        retention, advance = self._account_balances()
        if not self.currency_id.is_zero(float(retention)) or not self.currency_id.is_zero(float(advance)):
            obstacles.append(self.env._('Subcontract retention or advance control balances remain outstanding.'))
        for document in self.certificate_ids:
            if document.state != 'cancelled' and (document.state != 'billed' or document.invoice_id.state != 'posted'
                    or not self.currency_id.is_zero(document.invoice_id.amount_residual)
                    or document.invoice_id.reversal_move_ids.filtered(lambda move: move.state == 'posted')):
                obstacles.append(self.env._('A subcontract payment certificate is unposted or unpaid.'))
        for document in self.cash_document_ids:
            if document.state != 'cancelled' and (document.state != 'billed' or document.invoice_id.state != 'posted'
                    or not self.currency_id.is_zero(document.invoice_id.amount_residual)
                    or document.invoice_id.reversal_move_ids.filtered(lambda move: move.state == 'posted')):
                obstacles.append(self.env._('A subcontract advance or retention release is unposted or unpaid.'))
        if any(_amount(line.product_qty) > line._jakh_service_totals()[0] + Decimal('0.000001')
               for line in self.purchase_order_id.order_line._jakh_service_lines()):
            obstacles.append(self.env._('The subcontract order still contains unaccepted scope.'))
        return obstacles

    def action_close(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row, touch=True)
            if row.state == 'closed':
                continue
            if row.state != 'active':
                raise UserError(_('Close only an active subcontract finance agreement.'))
            obstacles = row._closing_obstacles()
            if obstacles:
                raise UserError('\n'.join(obstacles))
            row._transition(state='closed')
        return True

    def action_reopen(self):
        require_role(self.env, ['group_manager'])
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row, touch=True)
            if row.state != 'closed':
                raise UserError(_('Reopen only a closed subcontract finance agreement.'))
            row._transition(state='active')
        return True


class SubcontractPaymentCertificate(models.Model):
    _name = 'jakh.subcontract.payment.certificate'
    _description = 'SES-derived subcontract payment certificate'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.subcontract.ap.workflow']
    _check_company_auto = True

    name = fields.Char(required=True, default=lambda self: self.env._('New subcontract payment certificate'))
    agreement_id = fields.Many2one('jakh.subcontract.finance.agreement', required=True, check_company=True)
    service_entry_id = fields.Many2one('jakh.subcontract.service.entry', required=True, check_company=True)
    purchase_order_id = fields.Many2one(related='agreement_id.purchase_order_id', store=True)
    project_id = fields.Many2one(related='agreement_id.project_id', store=True)
    wbs_id = fields.Many2one(related='service_entry_id.wbs_id', store=True)
    company_id = fields.Many2one(related='agreement_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    available_quantity = fields.Float(related='service_entry_id.net_accepted_quantity', digits=(16, 6))
    quantity = fields.Float(readonly=True, digits=(16, 6))
    gross_amount = fields.Monetary(readonly=True)
    retention_amount = fields.Monetary(readonly=True)
    recovery_amount = fields.Monetary(readonly=True)
    net_before_tax = fields.Monetary(readonly=True)
    invoice_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('billed', 'Supplier bill created'), ('cancelled', 'Cancelled')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    _server_fields = {'state', 'quantity', 'gross_amount', 'retention_amount', 'recovery_amount', 'net_before_tax', 'invoice_id', 'approved_by'}
    _scope_fields = {'agreement_id', 'service_entry_id'}
    _one_ses = models.Constraint('UNIQUE(service_entry_id)', 'Create one payment certificate per accepted service entry.')

    @api.constrains('agreement_id', 'service_entry_id')
    def _check_scope(self):
        if self.filtered(lambda row: row.service_entry_id.purchase_order_id != row.purchase_order_id):
            raise ValidationError(_('The service entry and finance agreement must reference the same subcontract order.'))

    def action_submit(self):
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row)
            if row.state != 'draft' or row.agreement_id.state != 'active' or row.service_entry_id.state != 'approved':
                raise UserError(_('Submit a payment certificate for approved service work under an active agreement.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        lock_records(self.agreement_id, touch=True); lock_records(self.service_entry_id.purchase_line_id, touch=True)
        for row in self:
            lock_records(row)
            if row.state in ('approved', 'billed'):
                continue
            agreement = row.agreement_id; ses = row.service_entry_id
            if row.state != 'submitted' or agreement.state != 'active' or ses.invoice_id or row.company_id not in self.env.companies:
                raise UserError(_('Approve an unbilled submitted certificate under the selected active subcontract agreement.'))
            accepted, _value, billed, _billed_value = ses.purchase_line_id._jakh_service_totals()
            quantity = min(_amount(ses.net_accepted_quantity), accepted - billed)
            if quantity <= 0:
                raise UserError(_('No approved subcontract work remains available for this certificate.'))
            gross = _amount(row.currency_id.round(float(quantity * _amount(ses.purchase_line_id.price_unit))))
            pending = agreement._pending_certificates() - row
            retained = agreement._withheld_retention() + sum((_amount(cert.retention_amount) for cert in pending), Decimal())
            retention = min(gross * _amount(agreement.retention_percent) / 100, max(_amount(agreement.retention_cap) - retained, Decimal()))
            available = agreement._available_paid_advance() - sum((_amount(cert.recovery_amount) for cert in pending), Decimal())
            recovery = min(gross * _amount(agreement.recovery_percent) / 100, max(available, Decimal()), gross - retention)
            retention = _amount(row.currency_id.round(float(retention))); recovery = _amount(row.currency_id.round(float(recovery)))
            row._transition(state='approved', quantity=float(quantity), gross_amount=float(gross),
                            retention_amount=float(retention), recovery_amount=float(recovery),
                            net_before_tax=float(gross-retention-recovery), approved_by=self.env.user.id)
        return True

    def _bill_lines(self):
        self.ensure_one(); agreement = self.agreement_id
        gross = self.service_entry_id.purchase_line_id._prepare_account_move_line()
        gross.update({'quantity': self.quantity, 'jakh_subcontract_agreement_id': agreement.id, 'jakh_subcontract_control': 'gross'})
        lines = [gross]
        if self.retention_amount:
            lines.append({'name': self.env._('Subcontract retention held'), 'quantity': 1, 'price_unit': -self.retention_amount,
                          'account_id': agreement.retention_account_id.id, 'tax_ids': [Command.clear()],
                          'jakh_subcontract_agreement_id': agreement.id, 'jakh_subcontract_control': 'retention'})
        if self.recovery_amount:
            lines.append({'name': self.env._('Subcontract advance recovery'), 'quantity': 1, 'price_unit': -self.recovery_amount,
                          'account_id': agreement.advance_account_id.id, 'tax_ids': [Command.clear()],
                          'jakh_subcontract_agreement_id': agreement.id, 'jakh_subcontract_control': 'recovery'})
        return lines

    def action_create_bill(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        self.ensure_one(); lock_records(self.agreement_id, touch=True); lock_records(self)
        if self.invoice_id:
            return form_action(self.invoice_id)
        if self.state != 'approved' or self.agreement_id.state != 'active':
            raise UserError(_('Approve the subcontract payment certificate before creating its supplier bill.'))
        invoice = self.env['account.move']._create_jakh_subcontract_finance_bill({
            'move_type': 'in_invoice', 'company_id': self.company_id.id, 'partner_id': self.agreement_id.partner_id.id,
            'journal_id': self.agreement_id.journal_id.id, 'currency_id': self.currency_id.id,
            'invoice_date': self.date, 'invoice_origin': self.name,
            'jakh_service_entry_id': self.service_entry_id.id, 'jakh_subcontract_certificate_id': self.id,
            'invoice_line_ids': [Command.create(line) for line in self._bill_lines()],
        })
        super(OriginalServiceEntry, self.service_entry_id).write({'invoice_id': invoice.id})
        self._transition(state='billed', invoice_id=invoice.id)
        return form_action(invoice)

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row.agreement_id, touch=True); lock_records(row)
            if row.invoice_id.state == 'posted' and not _reversed_and_reconciled(row.invoice_id):
                raise UserError(_('Correct a posted subcontract certificate through its native supplier credit.'))
            if row.invoice_id and row.invoice_id.state != 'posted':
                row.invoice_id.button_cancel()
            row._transition(state='cancelled')
        return True

    def action_register_payment(self):
        self.ensure_one(); require_role(self.env, ['group_manager', 'group_finance'])
        if self.invoice_id.state != 'posted':
            raise UserError(_('Post the subcontract bill before registering its native payment.'))
        return self.invoice_id.action_register_payment()


class SubcontractCashDocument(models.Model):
    _name = 'jakh.subcontract.cash.document'
    _description = 'Controlled subcontract advance or retention release'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'jakh.subcontract.ap.workflow']
    _check_company_auto = True

    name = fields.Char(required=True, default=lambda self: self.env._('New subcontract cash document'))
    agreement_id = fields.Many2one('jakh.subcontract.finance.agreement', required=True, check_company=True)
    purchase_order_id = fields.Many2one(related='agreement_id.purchase_order_id', store=True)
    project_id = fields.Many2one(related='agreement_id.project_id', store=True)
    wbs_id = fields.Many2one(related='agreement_id.wbs_id', store=True)
    company_id = fields.Many2one(related='agreement_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    document_type = fields.Selection([('advance', 'Supplier advance'), ('release', 'Retention release')], required=True, default='advance')
    date = fields.Date(required=True, default=fields.Date.context_today)
    amount = fields.Monetary(required=True)
    reason = fields.Text(required=True)
    invoice_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('billed', 'Supplier bill created'), ('cancelled', 'Cancelled')], default='draft', required=True, readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    _server_fields = {'state', 'invoice_id', 'approved_by'}
    _scope_fields = {'agreement_id', 'document_type'}
    _positive = models.Constraint('CHECK(amount > 0)', 'A subcontract cash document requires a positive amount.')

    def action_submit(self):
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row)
            if row.state != 'draft' or row.agreement_id.state != 'active' or not row.reason.strip():
                raise UserError(_('Submit a documented cash request under an active subcontract agreement.'))
            row._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        lock_records(self.agreement_id, touch=True)
        for row in self:
            lock_records(row); agreement = row.agreement_id
            if row.state in ('approved', 'billed'):
                continue
            if row.state != 'submitted' or agreement.state != 'active' or row.company_id not in self.env.companies:
                raise UserError(_('Approve a submitted cash request in the selected active subcontract agreement.'))
            other = agreement.cash_document_ids.filtered(lambda doc: doc != row and doc.document_type == row.document_type
                                                        and doc.state in ('approved', 'billed'))
            if row.document_type == 'advance':
                committed = sum((_amount(doc.amount) for doc in other), Decimal())
                if committed + _amount(row.amount) > agreement._advance_limit():
                    raise UserError(_('Subcontract advances cannot exceed the approved percentage and advance cap.'))
            else:
                retention, _advance = agreement._account_balances()
                pending = sum((_amount(doc.amount) for doc in other if doc.invoice_id.state != 'posted'), Decimal())
                if _amount(row.amount) > retention - pending:
                    raise UserError(_('Retention release cannot exceed posted retention held after pending releases.'))
            row._transition(state='approved', approved_by=self.env.user.id)
        return True

    def _bill_lines(self):
        self.ensure_one(); agreement = self.agreement_id
        account = agreement.advance_account_id if self.document_type == 'advance' else agreement.retention_account_id
        return [{'name': self.name, 'quantity': 1, 'price_unit': self.amount, 'account_id': account.id,
                 'tax_ids': [Command.clear()], 'jakh_subcontract_agreement_id': agreement.id,
                 'jakh_subcontract_control': self.document_type}]

    def action_create_bill(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        self.ensure_one(); lock_records(self.agreement_id, touch=True); lock_records(self)
        if self.invoice_id:
            return form_action(self.invoice_id)
        if self.state != 'approved' or self.agreement_id.state != 'active':
            raise UserError(_('Approve the subcontract cash request before creating its supplier bill.'))
        invoice = self.env['account.move']._create_jakh_subcontract_finance_bill({
            'move_type': 'in_invoice', 'company_id': self.company_id.id, 'partner_id': self.agreement_id.partner_id.id,
            'journal_id': self.agreement_id.journal_id.id, 'currency_id': self.currency_id.id,
            'invoice_date': self.date, 'invoice_origin': self.name, 'jakh_subcontract_cash_document_id': self.id,
            'invoice_line_ids': [Command.create(line) for line in self._bill_lines()],
        })
        self._transition(state='billed', invoice_id=invoice.id)
        return form_action(invoice)

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        for row in self:
            lock_records(row.agreement_id, touch=True); lock_records(row)
            if row.invoice_id.state == 'posted' and not _reversed_and_reconciled(row.invoice_id):
                raise UserError(_('Correct a posted subcontract cash bill through its native supplier credit.'))
            if row.invoice_id and row.invoice_id.state != 'posted':
                row.invoice_id.button_cancel()
            row._transition(state='cancelled')
        return True

    def action_register_payment(self):
        self.ensure_one(); require_role(self.env, ['group_manager', 'group_finance'])
        if self.invoice_id.state != 'posted':
            raise UserError(_('Post the subcontract bill before registering its native payment.'))
        return self.invoice_id.action_register_payment()


class SubcontractServiceEntry(models.Model):
    _inherit = 'jakh.subcontract.service.entry'

    def action_create_bill(self):
        self.ensure_one(); require_role(self.env, ['group_manager', 'group_finance'])
        self.project_id._jakh_assert_open()
        agreement = self.env['jakh.subcontract.finance.agreement'].search([
            ('purchase_order_id', '=', self.purchase_order_id.id), ('state', '=', 'active'),
        ], limit=1)
        if not agreement:
            return super().action_create_bill()
        lock_records(agreement, touch=True); lock_records(self.purchase_line_id, touch=True); lock_records(self)
        if self.invoice_id:
            return form_action(self.invoice_id)
        certificate = self.env['jakh.subcontract.payment.certificate'].search([('service_entry_id', '=', self.id)], limit=1)
        if not certificate:
            certificate = self.env['jakh.subcontract.payment.certificate'].create({
                'name': self.name, 'agreement_id': agreement.id, 'service_entry_id': self.id, 'date': self.service_date,
            })
        return form_action(certificate)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    jakh_subcontract_agreement_id = fields.Many2one('jakh.subcontract.finance.agreement', readonly=True, copy=True, check_company=True)
    jakh_subcontract_control = fields.Selection([('gross', 'Certified gross work'), ('retention', 'Retention held'),
                                               ('recovery', 'Advance recovery'), ('advance', 'Supplier advance'),
                                               ('release', 'Retention release')], readonly=True, copy=True)

    @api.model_create_multi
    def create(self, values_list):
        if ((any(values.get('jakh_subcontract_agreement_id') or values.get('jakh_subcontract_control') for values in values_list)
                or _has_server_defaults(self.env, {'jakh_subcontract_agreement_id', 'jakh_subcontract_control'}))
                and self.env.context.get('_jakh_subcontract_control_lines') is not _CONTROL_LINE_CREATION):
            raise AccessError(_('Create subcontract control lines through an approved finance bill or its native credit.'))
        moves = creation_parents(self, values_list, 'move_id', 'account.move')
        if (moves.filtered(lambda move: move.state == 'posted' and move._jakh_is_subcontract_finance())
                and self.env.context.get('_jakh_subcontract_posting') is not _FINANCE_POSTING):
            raise AccessError(_('Posted subcontract finance bills cannot receive additional journal lines.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'jakh_subcontract_agreement_id', 'jakh_subcontract_control'}:
            raise AccessError(_('Subcontract control-account references are assigned by certificate actions.'))
        target = self.env['account.move'].browse(values.get('move_id'))
        if (target and target.state == 'posted' and target._jakh_is_subcontract_finance()
                and self.env.context.get('_jakh_subcontract_posting') is not _FINANCE_POSTING):
            raise AccessError(_('Posted subcontract finance bills cannot receive additional journal lines.'))
        sealed = self.filtered(lambda line: line.move_id.state == 'posted' and line.move_id._jakh_is_subcontract_finance())
        protected = {'move_id', 'account_id', 'partner_id', 'company_id', 'currency_id', 'journal_id',
                     'product_id', 'product_uom_id', 'purchase_line_id', 'quantity', 'price_unit', 'discount',
                     'debit', 'credit', 'balance', 'amount_currency', 'tax_ids', 'tax_tag_ids', 'name',
                     'amount_residual', 'amount_residual_currency', 'reconciled', 'parent_state'}
        if sealed and set(values) & protected and self.env.context.get('_jakh_subcontract_posting') is not _FINANCE_POSTING:
            raise AccessError(_('Posted subcontract finance lines are immutable. Use a native supplier credit.'))
        return super().write(values)

    def unlink(self):
        if (self.filtered(lambda line: line.move_id.state == 'posted' and line.move_id._jakh_is_subcontract_finance())
                and self.env.context.get('_jakh_subcontract_posting') is not _FINANCE_POSTING):
            raise AccessError(_('Posted subcontract finance lines cannot be deleted.'))
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    jakh_subcontract_certificate_id = fields.Many2one('jakh.subcontract.payment.certificate', readonly=True, copy=False, check_company=True)
    jakh_subcontract_cash_document_id = fields.Many2one('jakh.subcontract.cash.document', readonly=True, copy=False, check_company=True)
    _one_subcontract_certificate_bill = models.Constraint('UNIQUE(jakh_subcontract_certificate_id)', 'Create a subcontract certificate bill only once.')
    _one_subcontract_cash_bill = models.Constraint('UNIQUE(jakh_subcontract_cash_document_id)', 'Create a subcontract cash bill only once.')

    @api.model_create_multi
    def create(self, values_list):
        if (_has_server_defaults(self.env, {'jakh_subcontract_certificate_id', 'jakh_subcontract_cash_document_id'})
                or any(set(values) & {'jakh_subcontract_certificate_id', 'jakh_subcontract_cash_document_id'} for values in values_list)):
            raise AccessError(_('Create subcontract finance bills through their approved documents.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'jakh_subcontract_certificate_id', 'jakh_subcontract_cash_document_id'}:
            raise AccessError(_('A subcontract finance bill keeps its original document reference.'))
        controlled = self.filtered(lambda move: move._jakh_is_subcontract_finance())
        trusted_post = self.env.context.get('_jakh_subcontract_posting') is _FINANCE_POSTING
        if controlled and values.get('state') == 'posted' and not trusted_post:
            raise AccessError(_('Post subcontract finance bills through the native validated posting action.'))
        sealed = controlled.filtered(lambda move: move.state == 'posted')
        protected = {'state', 'journal_id', 'company_id', 'currency_id', 'date', 'invoice_date', 'partner_id',
                     'move_type', 'reversed_entry_id', 'invoice_payment_term_id', 'document_tax_mode',
                     'line_ids', 'invoice_line_ids', 'payment_state', 'amount_residual', 'amount_total',
                     'amount_untaxed', 'amount_tax', 'amount_total_signed', 'amount_residual_signed'}
        if sealed and set(values) & protected and not trusted_post:
            raise AccessError(_('Posted subcontract finance bills are immutable. Use a native supplier credit.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda move: move.state == 'posted' and move._jakh_is_subcontract_finance()):
            raise AccessError(_('Posted subcontract finance bills cannot be deleted.'))
        return super().unlink()

    def _jakh_is_subcontract_finance(self):
        self.ensure_one()
        return bool(self._jakh_subcontract_finance_source()
                    or (self.reversed_entry_id and self.reversed_entry_id._jakh_subcontract_finance_source()))

    def _create_jakh_subcontract_finance_bill(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        certificate = self.env['jakh.subcontract.payment.certificate'].browse(values.get('jakh_subcontract_certificate_id'))
        cash = self.env['jakh.subcontract.cash.document'].browse(values.get('jakh_subcontract_cash_document_id'))
        source = certificate or cash
        if (values.get('move_type') != 'in_invoice' or not source or (certificate and cash)
                or source.state != 'approved' or source.invoice_id):
            raise UserError(_('Create a subcontract finance bill only from one approved source document.'))
        return super(ExistingSubcontractMove, self.with_context(_jakh_subcontract_control_lines=_CONTROL_LINE_CREATION)).create(values).with_env(self.env)

    def _reverse_moves(self, default_values_list=None, cancel=False):
        controlled = self.filtered(lambda move: move._jakh_subcontract_finance_source())
        if controlled and controlled.filtered(lambda move: move.state != 'posted'):
            raise UserError(_('Post a subcontract finance bill before preparing its native credit.'))
        if controlled:
            return super(AccountMove, self.with_context(_jakh_subcontract_control_lines=_CONTROL_LINE_CREATION))._reverse_moves(default_values_list=default_values_list, cancel=cancel).with_env(self.env)
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    def _jakh_subcontract_finance_source(self):
        self.ensure_one()
        return self.jakh_subcontract_certificate_id or self.jakh_subcontract_cash_document_id

    @staticmethod
    def _jakh_line_signature(line):
        return (line.product_id.id, line.purchase_line_id.id, line.account_id.id, line.product_uom_id.id,
                round(line.quantity, 6), round(line.price_unit, 6), round(line.discount, 6), tuple(sorted(line.tax_ids.ids)),
                line.jakh_subcontract_agreement_id.id, line.jakh_subcontract_control)

    def _validate_jakh_subcontract_finance(self):
        incoming = self.filtered(lambda move: move.state != 'posted')
        seen_credits = set()
        for move in incoming:
            source = move._jakh_subcontract_finance_source()
            original_source = move.reversed_entry_id._jakh_subcontract_finance_source() if move.reversed_entry_id else False
            if source and move.reversed_entry_id:
                raise UserError(_('Create a subcontract finance credit through the original posted bill reversal action.'))
            agreement = source.agreement_id if source else (original_source.agreement_id if original_source else False)
            linked_agreements = self.env['jakh.subcontract.finance.agreement'].search([
                ('purchase_order_id', 'in', move.invoice_line_ids.purchase_line_id.order_id.ids), ('state', '=', 'active'),
            ])
            if linked_agreements and not source and not original_source:
                raise UserError(_('Bill an agreed subcontract through its SES-derived payment certificate.'))
            if not agreement:
                if move.line_ids.filtered('jakh_subcontract_agreement_id') or move.line_ids.filtered('jakh_subcontract_control'):
                    raise UserError(_('Subcontract control lines require an approved source document.'))
                continue
            agreement.project_id._jakh_assert_open()
            lock_records(agreement, touch=True)
            if (agreement.state != 'active' or move.company_id != agreement.company_id or move.currency_id != agreement.currency_id
                    or move.partner_id != agreement.partner_id or move.journal_id != agreement.journal_id):
                raise UserError(_('A subcontract finance bill must retain its active agreement supplier, company, currency and journal.'))
            if original_source:
                original = move.reversed_entry_id
                if (move.move_type != 'in_refund' or original.state != 'posted' or original.id in seen_credits
                        or self.search_count([('reversed_entry_id', '=', original.id), ('state', '=', 'posted')])):
                    raise UserError(_('Use one complete native credit of the original subcontract finance bill.'))
                seen_credits.add(original.id)
                expected = sorted(self._jakh_line_signature(line) for line in original.invoice_line_ids.filtered(lambda line: not line.display_type or line.display_type == 'product'))
                actual = sorted(self._jakh_line_signature(line) for line in move.invoice_line_ids.filtered(lambda line: not line.display_type or line.display_type == 'product'))
                if actual != expected:
                    raise UserError(_('Subcontract finance credits must reverse the original certified gross and control lines exactly.'))
                retention, advance = agreement._account_balances()
                for line in move.invoice_line_ids:
                    if line.jakh_subcontract_control == 'retention':
                        retention -= _amount(-line.price_subtotal)
                    elif line.jakh_subcontract_control == 'release':
                        retention += _amount(line.price_subtotal)
                    elif line.jakh_subcontract_control == 'advance':
                        advance -= _amount(line.price_subtotal)
                    elif line.jakh_subcontract_control == 'recovery':
                        advance += _amount(-line.price_subtotal)
                if retention < -Decimal('0.000001') or advance < -Decimal('0.000001'):
                    raise UserError(_('Reverse later retention releases or recoveries before crediting their original subcontract bill.'))
                continue
            if not source or source.state != 'billed' or source.invoice_id != move or move.move_type != 'in_invoice':
                raise UserError(_('Post only the native bill created from the approved subcontract finance document.'))
            expected = source._bill_lines()
            actual = move.invoice_line_ids.filtered(lambda line: not line.display_type or line.display_type == 'product')
            if len(actual) != len(expected):
                raise UserError(_('Subcontract finance bill totals must equal the approved gross and control lines.'))
            for values in expected:
                control = values['jakh_subcontract_control']
                line = actual.filtered(lambda item: item.jakh_subcontract_control == control)
                if (len(line) != 1 or line.jakh_subcontract_agreement_id != agreement or abs(line.quantity-values['quantity']) > 0.000001
                        or abs(line.price_unit-values['price_unit']) > 0.000001 or line.discount
                        or (values.get('account_id') and line.account_id.id != values['account_id'])
                        or (control != 'gross' and (line.tax_ids or line.purchase_line_id or line.product_id))):
                    raise UserError(_('Subcontract finance bill totals must equal the approved gross and control lines.'))
                if control == 'gross':
                    account = line.product_id.product_tmpl_id.get_product_accounts(fiscal_pos=move.fiscal_position_id)['expense'] or move.journal_id.default_account_id
                    if line.account_id != account or line.account_id.account_type not in ('expense', 'expense_direct_cost'):
                        raise UserError(_('Certified subcontract work must post to its approved product expense account.'))
            if source._name == 'jakh.subcontract.payment.certificate':
                if source.service_entry_id.net_accepted_quantity + 0.000001 < source.quantity:
                    raise UserError(_('Correct the certificate before billing service work whose acceptance was reversed.'))
                retention, _advance = agreement._account_balances()
                if _amount(source.recovery_amount) > agreement._available_paid_advance() + Decimal('0.000001'):
                    raise UserError(_('Advance recovery cannot exceed the outstanding paid subcontract advance.'))
            elif source.document_type == 'release':
                retention, _advance = agreement._account_balances()
                if _amount(source.amount) > retention + Decimal('0.000001'):
                    raise UserError(_('Retention release cannot exceed the posted retention liability.'))

    def _post(self, soft=True):
        self._validate_jakh_subcontract_finance()
        return super(AccountMove, self.with_context(_jakh_subcontract_posting=_FINANCE_POSTING))._post(soft=soft).with_env(self.env)


class ProjectFinalAccount(models.Model):
    _inherit = 'jakh.project.final.account'

    def _check_complete(self):
        result = super()._check_complete()
        agreements = self.env['jakh.subcontract.finance.agreement'].search([
            ('project_id', '=', self.project_id.id), ('state', '!=', 'closed'),
        ])
        lock_records(agreements, touch=True)
        obstacles = []
        for agreement in agreements:
            if agreement.state != 'active':
                obstacles.append(self.env._('Approve or remove the draft subcontract finance configuration before final closure.'))
            else:
                obstacles.extend(agreement._closing_obstacles())
        if obstacles:
            raise UserError('\n'.join(obstacles))
        return result
