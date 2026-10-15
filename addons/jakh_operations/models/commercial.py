from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError
from ..services import certificate_amounts
from .procurement import FINANCIAL, lock_records, require_role, form_action, creation_parents, lock_editable
from .execution_controls import _has_server_defaults


class ProjectContract(models.Model):
    _name = 'jakh.project.contract'
    _description = 'Measured construction contract'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    name = fields.Char(required=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', required=True, check_company=True)
    sale_order_id = fields.Many2one('sale.order', check_company=True)
    line_ids = fields.One2many('jakh.project.contract.line', 'contract_id')
    state = fields.Selection([('draft', 'Finance configuration'), ('active', 'Active'), ('closed', 'Closed')], default='draft', readonly=True, required=True, tracking=True)
    retention_percent = fields.Float(default=0, groups=FINANCIAL)
    retention_cap = fields.Monetary(default=0, groups=FINANCIAL)
    recovery_percent = fields.Float(default=0, groups=FINANCIAL)
    advance_cap = fields.Monetary(string='Approved recoverable advance', default=0, groups=FINANCIAL, help='Finance-approved contract ceiling. This field does not assert cash receipt.')
    journal_id = fields.Many2one('account.journal', check_company=True, groups=FINANCIAL, domain=[('type', '=', 'sale')])
    income_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)
    retention_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)
    advance_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)
    deduction_account_id = fields.Many2one('account.account', check_company=True, groups=FINANCIAL)
    tax_ids = fields.Many2many('account.tax', 'jakh_contract_gross_tax_rel', check_company=True, groups=FINANCIAL, domain=[('type_tax_use', '=', 'sale')])
    retention_tax_ids = fields.Many2many('account.tax', 'jakh_contract_retention_tax_rel', check_company=True, groups=FINANCIAL, domain=[('type_tax_use', '=', 'sale')])
    recovery_tax_ids = fields.Many2many('account.tax', 'jakh_contract_recovery_tax_rel', check_company=True, groups=FINANCIAL, domain=[('type_tax_use', '=', 'sale')])
    deduction_tax_ids = fields.Many2many('account.tax', 'jakh_contract_deduction_tax_rel', check_company=True, groups=FINANCIAL, domain=[('type_tax_use', '=', 'sale')])
    finance_approved_by = fields.Many2one('res.users', readonly=True)
    revision = fields.Integer(default=0, readonly=True)

    @api.model_create_multi
    def create(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if (_has_server_defaults(self.env, {'state', 'revision', 'finance_approved_by'})
                or any(vals.get('state', 'draft') != 'draft' or set(vals) & {'revision', 'finance_approved_by'} for vals in values)):
            raise AccessError(_('Contract workflow fields are server controlled.'))
        return super().create(values)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if set(values) & {'state', 'revision', 'finance_approved_by'}:
            raise AccessError(_('Use the contract approval actions.'))
        if self.filtered(lambda contract: contract.state != 'draft'):
            raise UserError(_('Active contract quantities and finance policy are immutable in this release.'))
        return super().write(values)

    def unlink(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        if self.filtered(lambda contract: contract.state != 'draft'):
            raise UserError(_('Only draft contract configurations can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write'); result = super().write(values)
        for rec in self:
            self.env['jakh.audit.event']._append(rec.company_id, 'contract.' + values.get('state', 'revision'), rec, {'revision': rec.revision})
        return result

    def _locked(self):
        lock_records(self)
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select the contract company.'))

    def action_activate(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for contract in self:
            contract._locked()
            if contract.state != 'draft' or not contract.line_ids or not contract.journal_id or not contract.income_account_id:
                raise UserError(_('Configure BOQ lines, a sales journal and an income account before finance approval.'))
            if contract.income_account_id.account_type not in ('income', 'income_other'):
                raise UserError(_('Choose an income account for certified gross revenue.'))
            if (contract.tax_ids | contract.retention_tax_ids | contract.recovery_tax_ids | contract.deduction_tax_ids).filtered('price_include'):
                raise UserError(_('This release requires tax-exclusive BOQ rates and finance-approved tax configuration.'))
            if contract.retention_percent and (not contract.retention_account_id or contract.retention_cap <= 0):
                raise UserError(_('Configure the retention control account and cap.'))
            if contract.recovery_percent and (not contract.advance_account_id or contract.advance_cap <= 0):
                raise UserError(_('Configure the advance control account and approved recovery ceiling.'))
            for account in contract.retention_account_id | contract.advance_account_id | contract.deduction_account_id:
                if account.account_type in ('asset_receivable', 'liability_payable', 'off_balance'):
                    raise UserError(_('Deduction invoice lines require approved control accounts, not payment-term or off-balance accounts.'))
            contract._transition(state='active', finance_approved_by=self.env.user.id, revision=contract.revision + 1)
        return True

    @api.constrains('retention_percent', 'recovery_percent', 'retention_cap', 'advance_cap', 'sale_order_id')
    def _check_configuration(self):
        for rec in self:
            if not 0 <= rec.retention_percent <= 100 or not 0 <= rec.recovery_percent <= 100 or rec.retention_cap < 0 or rec.advance_cap < 0:
                raise ValidationError(_('Use non-negative limits and percentages between zero and 100.'))
            if rec.sale_order_id and (rec.sale_order_id.company_id != rec.company_id or rec.sale_order_id.partner_id != rec.partner_id):
                raise ValidationError(_('The sales order must match the contract customer and company.'))


class ContractLine(models.Model):
    _name = 'jakh.project.contract.line'
    _description = 'Contract BOQ measurement item'
    _check_company_auto = True
    contract_id = fields.Many2one('jakh.project.contract', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    wbs_id = fields.Many2one('jakh.wbs', required=True, check_company=True)
    uom_id = fields.Many2one('uom.uom', required=True)
    quantity = fields.Float(required=True, digits=(16, 6))
    unit_price = fields.Float(required=True, digits=(16, 6), groups=FINANCIAL)
    _unique_code = models.Constraint('UNIQUE(contract_id,code)', 'BOQ codes must be unique within the contract.')
    _nonnegative = models.Constraint('CHECK(quantity >= 0 AND unit_price >= 0)', 'Quantities and unit prices must be non-negative.')

    def _editable(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        lock_editable(self.contract_id, ('draft',), _('An active contract BOQ cannot be rewritten.'))

    @api.model_create_multi
    def create(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        parents = creation_parents(self, values, 'contract_id', 'jakh.project.contract')
        lock_editable(parents, ('draft',), _('An active contract BOQ cannot be rewritten.'))
        records = super().create(values); records._editable(); return records

    def write(self, values):
        self._editable()
        if 'contract_id' in values:
            raise AccessError(_('Contract lines cannot move between contracts.'))
        return super().write(values)

    def unlink(self):
        self._editable(); return super().unlink()

    @api.constrains('wbs_id', 'contract_id')
    def _check_project(self):
        for rec in self:
            if rec.wbs_id.project_id != rec.contract_id.project_id:
                raise ValidationError(_('The BOQ WBS must belong to this contract project.'))


class PaymentCertificate(models.Model):
    _name = 'jakh.payment.certificate'
    _description = 'Cumulative customer measurement certificate'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    name = fields.Char(required=True, default=lambda self: self.env._('Draft certificate'))
    contract_id = fields.Many2one('jakh.project.contract', required=True, check_company=True)
    company_id = fields.Many2one(related='contract_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(required=True, default=fields.Date.context_today)
    line_ids = fields.One2many('jakh.payment.certificate.line', 'certificate_id')
    variation_line_ids = fields.One2many('jakh.payment.certificate.variation.line', 'certificate_id')
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('approved', 'Approved'), ('invoiced', 'Draft invoice created')], default='draft', readonly=True, required=True, tracking=True)
    cumulative_gross = fields.Monetary(readonly=True, groups=FINANCIAL)
    previous_gross = fields.Monetary(readonly=True, groups=FINANCIAL)
    period_gross = fields.Monetary(readonly=True, groups=FINANCIAL)
    retention_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    recovery_amount = fields.Monetary(readonly=True, groups=FINANCIAL)
    other_deductions = fields.Monetary(default=0, groups=FINANCIAL)
    deduction_reason = fields.Text(groups=FINANCIAL)
    net_before_tax = fields.Monetary(readonly=True, groups=FINANCIAL)
    approved_by = fields.Many2one('res.users', readonly=True)
    approval_sequence = fields.Integer(readonly=True)
    invoice_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, groups=FINANCIAL)
    _server_fields = {'state', 'cumulative_gross', 'previous_gross', 'period_gross', 'retention_amount', 'recovery_amount', 'net_before_tax', 'approved_by', 'approval_sequence', 'invoice_id'}

    @api.model_create_multi
    def create(self, values):
        if (_has_server_defaults(self.env, self._server_fields)
                or any(set(vals) & self._server_fields for vals in values)):
            raise AccessError(_('Certificate calculations and states are assigned by server actions.'))
        return super().create(values)

    def write(self, values):
        if set(values) & self._server_fields:
            raise AccessError(_('Use the controlled certificate actions.'))
        if self.filtered(lambda cert: cert.state != 'draft'):
            raise UserError(_('Submitted and approved measurements are immutable.'))
        if 'contract_id' in values:
            raise AccessError(_('Create a new certificate instead of moving measurements to another contract.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda rec: rec.state != 'draft'):
            raise UserError(_('Only draft certificates can be deleted.'))
        return super().unlink()

    def _transition(self, **values):
        self.check_access('write'); result = super().write(values)
        for rec in self:
            self.env['jakh.audit.event']._append(rec.company_id, 'certificate.' + values.get('state', 'updated'), rec, {'state': rec.state})
        return result

    def _ensure_draft_measurement(self):
        lock_editable(self, ('draft',), _('Only draft measurements can be edited.'))

    def action_prepare_lines(self):
        self.ensure_one(); lock_records(self)
        if self.state != 'draft' or self.contract_id.state != 'active' or self.line_ids or self.variation_line_ids:
            raise UserError(_('Prepare an empty draft against an active contract. Existing measurements are never overwritten.'))
        prior = self.search([('contract_id', '=', self.contract_id.id), ('state', 'in', ['approved', 'invoiced'])], order='approval_sequence desc', limit=1)
        previous = {line.contract_line_id.id: line.cumulative_quantity for line in prior.line_ids}
        previous_variations = {line.variation_id.id: line.cumulative_quantity for line in prior.variation_line_ids}
        variations = self.env['jakh.project.variation'].search([
            ('variation_type', '=', 'owner'), ('contract_id', '=', self.contract_id.id),
            ('state', 'in', ['approved', 'certified']),
            ('boq_scope_omission', '=', False),
        ])
        self.write({
            'line_ids': [Command.create({'contract_line_id': line.id, 'cumulative_quantity': previous.get(line.id, 0)}) for line in self.contract_id.line_ids],
            'variation_line_ids': [Command.create({'variation_id': line.id, 'cumulative_quantity': previous_variations.get(line.id, 0)}) for line in variations],
        })
        return True

    def action_submit(self):
        for cert in self:
            lock_records(cert)
            if cert.state != 'draft' or cert.contract_id.state != 'active' or not cert.line_ids:
                raise UserError(_('Submit a draft measurement against an active contract.'))
            if set(cert.line_ids.contract_line_id.ids) != set(cert.contract_id.line_ids.ids):
                raise UserError(_('Include every BOQ line with its cumulative quantity, including unchanged quantities.'))
            for line in cert.variation_line_ids:
                if line.variation_id.contract_id != cert.contract_id or line.variation_id.state not in ('approved', 'certified'):
                    raise UserError(_('Certificate variation measurements must remain linked to approved owner variations.'))
            cert._transition(state='submitted')
        return True

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for cert in self:
            lock_records(cert); contract = cert.contract_id; contract._locked()
            if cert.state in ('approved', 'invoiced'):
                continue
            if cert.state != 'submitted' or contract.state != 'active':
                raise UserError(_('Approve a submitted certificate on an active contract.'))
            approved = self.search([('contract_id', '=', contract.id), ('state', 'in', ['approved', 'invoiced']), ('id', '!=', cert.id)], order='approval_sequence')
            if approved and cert.date < max(approved.mapped('date')):
                raise UserError(_('New certificates cannot precede the last approved measurement date.'))
            previous = approved[-1:] if approved else self.browse()
            prior_quantities = {line.contract_line_id.id: line.cumulative_quantity for line in previous.line_ids}
            prior_variations = {line.variation_id.id: line.cumulative_quantity for line in previous.variation_line_ids}
            if cert.other_deductions and (not cert.deduction_reason or not contract.deduction_account_id):
                raise UserError(_('Other deductions need a documented reason and a finance-approved control account.'))
            try:
                totals = certificate_amounts([
                    {'cumulative_quantity': line.cumulative_quantity, 'prior_quantity': prior_quantities.get(line.contract_line_id.id, 0),
                     'contract_quantity': line.contract_line_id._jakh_measurement_limit(cert.date), 'unit_price': line.contract_line_id.unit_price}
                    for line in cert.line_ids] + [
                    {'cumulative_quantity': line.cumulative_quantity, 'prior_quantity': prior_variations.get(line.variation_id.id, 0),
                     'contract_quantity': line.variation_id.quantity, 'unit_price': line.variation_id.unit_price,
                     'factor': -1 if line.variation_id.effect == 'omission' else 1}
                    for line in cert.variation_line_ids], previous.cumulative_gross or 0,
                    contract.retention_percent, contract.retention_cap, sum(approved.mapped('retention_amount')),
                    contract.recovery_percent, contract._jakh_recoverable_advance_cap(), sum(approved.mapped('recovery_amount')),
                    cert.other_deductions, cert.currency_id.decimal_places, translate=self.env._)
            except ValueError as exc:
                raise UserError(str(exc))
            contract._transition(revision=contract.revision + 1)
            cert._transition(state='approved', approved_by=self.env.user.id, approval_sequence=contract.revision, **{key: float(value) for key, value in totals.items() if key != 'other_deductions'})
            Entry = self.env['jakh.project.control.entry']
            for line in cert.line_ids:
                amount = (line.cumulative_quantity - prior_quantities.get(line.contract_line_id.id, 0)) * line.contract_line_id.unit_price
                Entry._record(
                    name=cert.name, company=cert.company_id, project=contract.project_id, wbs=line.contract_line_id.wbs_id,
                    entry_type='certified_revenue', amount=amount, source_model=cert._name, source_id=cert.id,
                    source_key='%s:%s:certified_boq:%s' % (cert._name, cert.id, line.contract_line_id.id),
                    entry_date=cert.date, partner=contract.partner_id, contract=contract, contract_line=line.contract_line_id,
                )
            for line in cert.variation_line_ids:
                variation = line.variation_id
                factor = -1 if variation.effect == 'omission' else 1
                amount = factor * (line.cumulative_quantity - prior_variations.get(variation.id, 0)) * variation.unit_price
                Entry._record(
                    name=cert.name, company=cert.company_id, project=contract.project_id, wbs=variation.wbs_id,
                    entry_type='certified_revenue', amount=amount, source_model=cert._name, source_id=cert.id,
                    source_key='%s:%s:certified_variation:%s' % (cert._name, cert.id, variation.id),
                    entry_date=cert.date, partner=contract.partner_id, contract=contract,
                    contract_line=variation.contract_line_id,
                    location=variation.location_id, discipline=variation.discipline_id,
                )
                if line.cumulative_quantity == variation.quantity:
                    variation._mark_certified()
        return True

    def action_create_invoice(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one(); lock_records(self); self.contract_id._locked()
        if self.invoice_id:
            return form_action(self.invoice_id)
        if self.state != 'approved':
            raise UserError(_('Finance must approve the measurement before creating an invoice.'))
        contract = self.contract_id
        lines = [Command.create({'name': self.env._('%s — certified period gross', self.name), 'quantity': 1, 'price_unit': self.period_gross,
                   'account_id': contract.income_account_id.id, 'tax_ids': [Command.set(contract.tax_ids.ids)]})]
        for label, amount, account, taxes in [
                (self.env._('Retention control'), self.retention_amount, contract.retention_account_id, contract.retention_tax_ids),
                (self.env._('Advance recovery'), self.recovery_amount, contract.advance_account_id, contract.recovery_tax_ids),
                (self.env._('Documented deduction'), self.other_deductions, contract.deduction_account_id, contract.deduction_tax_ids)]:
            if amount:
                lines.append(Command.create({'name': label, 'quantity': 1, 'price_unit': -amount, 'account_id': account.id, 'tax_ids': [Command.set(taxes.ids)]}))
        invoice = self.env['account.move']._create_jakh({'move_type': 'out_refund' if self.period_gross < 0 else 'out_invoice', 'company_id': self.company_id.id,
            'partner_id': contract.partner_id.id, 'journal_id': contract.journal_id.id,
            'currency_id': self.currency_id.id, 'invoice_date': self.date,
            'invoice_origin': self.name, 'invoice_line_ids': lines, 'jakh_certificate_id': self.id})
        self._transition(state='invoiced', invoice_id=invoice.id)
        return form_action(invoice)


class MeasurementLine(models.Model):
    _name = 'jakh.payment.certificate.line'
    _description = 'Cumulative BOQ measurement'
    _check_company_auto = True
    certificate_id = fields.Many2one('jakh.payment.certificate', required=True, ondelete='cascade', check_company=True)
    company_id = fields.Many2one(related='certificate_id.company_id', store=True)
    contract_line_id = fields.Many2one('jakh.project.contract.line', required=True, check_company=True)
    cumulative_quantity = fields.Float(required=True, digits=(16, 6))
    _one_item = models.Constraint('UNIQUE(certificate_id,contract_line_id)', 'Measure each BOQ item once per certificate.')
    _positive = models.Constraint('CHECK(cumulative_quantity >= 0)', 'Measured quantities cannot be negative.')

    def _editable(self):
        lock_editable(self.certificate_id, ('draft',), _('Only draft measurements can be edited.'))

    @api.model_create_multi
    def create(self, values):
        parents = creation_parents(self, values, 'certificate_id', 'jakh.payment.certificate')
        lock_editable(parents, ('draft',), _('Only draft measurements can be edited.'))
        records = super().create(values); records._editable(); return records

    def write(self, values):
        self._editable()
        if 'certificate_id' in values:
            raise AccessError(_('Measurements cannot move between certificates.'))
        return super().write(values)

    def unlink(self):
        self._editable(); return super().unlink()

    @api.constrains('certificate_id', 'contract_line_id')
    def _check_contract(self):
        for line in self:
            if line.contract_line_id.contract_id != line.certificate_id.contract_id:
                raise ValidationError(_('The measurement line must belong to this contract.'))


class AccountMove(models.Model):
    _inherit = 'account.move'
    jakh_certificate_id = fields.Many2one('jakh.payment.certificate', readonly=True, copy=False, check_company=True)
    _one_certificate = models.Constraint('UNIQUE(jakh_certificate_id)', 'A certificate can have only one native invoice.')

    @api.model_create_multi
    def create(self, values):
        if (_has_server_defaults(self.env, {'jakh_certificate_id'})
                or any(vals.get('jakh_certificate_id') for vals in values)):
            raise AccessError(_('Create a certificate invoice through the approved certificate.'))
        return super().create(values)

    def _create_jakh(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        return super().create(values)

    def write(self, values):
        if 'jakh_certificate_id' in values:
            raise AccessError(_('The certificate invoice link is immutable.'))
        return super().write(values)

    def _post(self, soft=True):
        for invoice in self.filtered('jakh_certificate_id'):
            cert = invoice.jakh_certificate_id; contract = cert.contract_id
            expected_type = 'out_refund' if cert.period_gross < 0 else 'out_invoice'
            if cert.state != 'invoiced' or cert.invoice_id != invoice or invoice.move_type != expected_type or invoice.company_id != cert.company_id or invoice.currency_id != cert.currency_id or invoice.partner_id != contract.partner_id or invoice.journal_id != contract.journal_id or invoice.invoice_date != cert.date:
                raise UserError(_('The draft invoice must retain its approved certificate identity and finance configuration.'))
            expected = [(contract.income_account_id.id, cert.period_gross, tuple(sorted(contract.tax_ids.ids)))]
            for value, account, taxes in [(cert.retention_amount, contract.retention_account_id, contract.retention_tax_ids),
                                         (cert.recovery_amount, contract.advance_account_id, contract.recovery_tax_ids),
                                         (cert.other_deductions, contract.deduction_account_id, contract.deduction_tax_ids)]:
                if value:
                    expected.append((account.id, -value, tuple(sorted(taxes.ids))))
            lines = invoice.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
            actual = [(line.account_id.id, line.price_unit, tuple(sorted(line.tax_ids.ids))) for line in lines]
            if any(line.quantity != 1 or line.discount for line in lines) or sorted(actual) != sorted(expected):
                raise UserError(_('Invoice amounts, control accounts and taxes must match the approved certificate before native posting.'))
        return super()._post(soft=soft)
