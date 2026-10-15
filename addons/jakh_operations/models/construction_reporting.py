"""Permission-scoped construction reports and governed project exceptions.

The report is a read model, not another ledger. Every signed fact is assigned
to exactly one BOQ/location bucket. Older WBS-only facts remain unallocated.
PDF, XLSX, the cockpit and threshold rules reuse these server calculations.
"""
import csv
import io
import logging
import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import xlsxwriter

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .procurement import form_action, lock_records, require_role
from .reporting_workflow import REPORT_ROLES, OriginalDelivery, _totals


_logger = logging.getLogger(__name__)
COST_COLUMNS = (
    'original_budget', 'revised_budget', 'commitments', 'received_cost',
    'actual_cost', 'original_revenue', 'variation_revenue', 'certified_revenue', 'billed_revenue',
)
METRICS = [
    ('commitment_percent', 'Commitments / revised budget (%)'),
    ('forecast_overrun_percent', 'Forecast cost over revised budget (%)'),
    ('forecast_margin_percent', 'Forecast margin / revised contract value (%)'),
    ('cpi', 'Cost performance index (CPI)'),
    ('spi', 'Schedule performance index (SPI)'),
    ('unbilled_amount', 'Approved unbilled gross amount'),
    ('unbilled_days', 'Oldest approved unbilled age (days)'),
]


def _number(value):
    number = Decimal(str(value or 0))
    if not number.is_finite():
        raise ValidationError(_('Report source amounts must be finite decimals.'))
    return number


def _percent(numerator, denominator):
    return float(_number(numerator) * 100 / _number(denominator)) if denominator > 0 else None


def _location_building(location):
    """Use actual location ancestry; never infer a building from a WBS label."""
    seen = set()
    while location and location.id not in seen:
        seen.add(location.id)
        if location.kind == 'building':
            return location
        location = location.parent_id
    return location.browse()


class ConstructionControl(models.AbstractModel):
    _name = 'jakh.construction.control'
    _description = 'Construction control calculation service'

    @api.model
    def _scope(self, project):
        require_role(self.env, REPORT_ROLES)
        project = self.env['project.project'].with_context(active_test=False).browse(project.id).exists()
        if not project:
            raise ValidationError(_('Choose an existing construction project.'))
        project.check_access('read')
        if project.company_id not in self.env.companies:
            raise AccessError(_('Select this project company in the company switcher.'))
        return project

    @api.model
    def _entries(self, project, as_of):
        self._scope(project)
        return self.env['jakh.project.control.entry'].search([
            ('project_id', '=', project.id), ('date', '<=', as_of),
        ], order='date,id')

    @api.model
    def _summary(self, project, as_of):
        project = self._scope(project)
        as_of = fields.Date.to_date(as_of)
        rows = self.env['jakh.project.control.report']._calculation_rows(project, as_of)
        totals = _totals(rows)
        statuses = self.env['jakh.project.schedule.progress'].search([
            ('project_id', '=', project.id), ('state', '=', 'accepted'), ('as_of_date', '<=', as_of),
        ], order='as_of_date desc,id desc')
        latest = {}
        for status in statuses:
            latest.setdefault(status.wbs_id.id, status)
        weighted = Decimal(0)
        total_weight = Decimal(0)
        missing = []
        for row in rows:
            weight = _number(row['original_budget'])
            if weight <= 0:
                continue
            total_weight += weight
            status = latest.get(row['wbs_id'])
            if status:
                weighted += weight * _number(status.progress_percent)
            else:
                missing.append(row['wbs_id'])
        # Missing progress is not silently treated as 0% or excluded from a total.
        physical = float(weighted / total_weight) if total_weight > 0 and not missing else None
        accounting_access = self.env['account.move'].has_access('read')
        unbilled = self._unbilled_rows(project, as_of) if accounting_access else []
        receipts = self._collections(project, as_of)
        totals.update({
            'physical_percent': physical,
            'physical_complete': not missing and total_weight > 0,
            'missing_progress_wbs': missing,
            'financial_percent': _percent(totals['certified_revenue'], totals['revised_revenue']),
            'billing_percent': _percent(totals['billed_revenue'], totals['revised_revenue']),
            'unbilled_amount': float(sum(_number(row['unbilled_gross']) for row in unbilled)) if accounting_access else None,
            'unbilled_days': max((row['age_days'] for row in unbilled if row['unbilled_gross'] > 0), default=0) if accounting_access else None,
            'matched_receipts': receipts['amount'],
        })
        return totals

    @api.model
    def _collections(self, project, as_of):
        """Signed company-currency allocations of posted native payments.

        Credits, exchange differences and write-offs are not cash receipts.
        Tax-inclusive payment allocations remain separate from tax-exclusive
        certification/billing percentages. This is not a bank-cleared balance.
        """
        project = self._scope(project)
        as_of = fields.Date.to_date(as_of)
        if not self.env['account.move'].has_access('read'):
            return {'amount': None, 'payment_move_ids': []}
        invoices = self.env['account.move'].search([
            ('company_id', '=', project.company_id.id), ('state', '=', 'posted'), ('date', '<=', as_of),
            '|', ('jakh_certificate_id.contract_id.project_id', '=', project.id),
            ('jakh_cash_document_id.contract_id.project_id', '=', project.id),
        ])
        sources = invoices | invoices.reversal_move_ids.filtered(lambda move: move.state == 'posted' and move.date <= as_of)
        amount = Decimal(0)
        payment_move_ids = set()
        for line in sources.line_ids.filtered(lambda row: row.account_id.account_type == 'asset_receivable'):
            for partial in line.matched_debit_ids | line.matched_credit_ids:
                if partial.max_date > as_of:
                    continue
                counterpart = partial.credit_move_id if partial.debit_move_id == line else partial.debit_move_id
                move = counterpart.move_id
                if move.state != 'posted' or move.date > as_of or move.company_id != project.company_id:
                    continue
                if not move.origin_payment_id and not move.statement_line_id:
                    continue
                amount += _number(partial.amount) * (1 if line.balance > 0 else -1)
                payment_move_ids.add(move.id)
        return {'amount': float(amount), 'payment_move_ids': sorted(payment_move_ids)}

    @api.model
    def _cost_rows(self, project, as_of):
        project = self._scope(project)
        as_of = fields.Date.to_date(as_of)
        buckets = defaultdict(lambda: {key: Decimal(0) for key in COST_COLUMNS})
        def key_for(wbs, line=False, location=False, discipline=False):
            return (wbs.id, line.id if line else False, location.id if location else False,
                    discipline.id if discipline else False)

        contracts = self.env['jakh.project.contract'].search([
            ('project_id', '=', project.id), ('state', 'in', ['active', 'closed']),
        ])
        lines_by_wbs = defaultdict(list)
        allocations = (self.env['jakh.boq.location.budget']._latest(project, as_of)
                       if 'jakh.boq.location.budget' in self.env else {})
        allocated_budget_by_wbs = defaultdict(Decimal)
        for line in contracts.line_ids:
            plan = allocations.get(line.id)
            if plan:
                allocated_quantity = Decimal(0)
                for allocation in plan.line_ids:
                    key = key_for(line.wbs_id, line, allocation.location_id, allocation.discipline_id)
                    quantity = _number(allocation.quantity)
                    amount = _number(allocation.budget_amount)
                    allocated_quantity += quantity
                    allocated_budget_by_wbs[line.wbs_id.id] += amount
                    buckets[key]['original_budget'] += amount
                    buckets[key]['revised_budget'] += amount
                    buckets[key]['original_revenue'] += quantity * _number(line.unit_price)
                remainder = _number(line.quantity) - allocated_quantity
                if remainder > 0:
                    buckets[key_for(line.wbs_id, line)]['original_revenue'] += remainder * _number(line.unit_price)
                lines_by_wbs[line.wbs_id.id].append(line)
                continue
            location = line.location_id if 'location_id' in line._fields else False
            discipline = line.discipline_id if 'discipline_id' in line._fields else False
            key = key_for(line.wbs_id, line, location, discipline)
            buckets[key]['original_revenue'] += _number(line.quantity) * _number(line.unit_price)
            lines_by_wbs[line.wbs_id.id].append(line)
        for wbs in self.env['jakh.wbs'].search([('project_id', '=', project.id)]):
            candidates = lines_by_wbs[wbs.id]
            # Only a unique, source-linked frozen award budget may be attributed
            # to a BOQ line. Existing WBS labels are not migration evidence.
            line = candidates[0] if len(candidates) == 1 else False
            attributable = bool(line and 'source_line_uuid' in line._fields and line.source_line_uuid
                                and 'original_budget_total' in line._fields
                                and wbs.currency_id.is_zero(line.original_budget_total - wbs.budget_amount))
            has_plan = any(candidate.id in allocations for candidate in candidates)
            remainder = _number(wbs.budget_amount) - allocated_budget_by_wbs[wbs.id]
            if remainder < -Decimal('0.000001'):
                raise ValidationError(_('Approved building allocations exceed their frozen WBS budget. Review source allocation evidence.'))
            key = key_for(wbs, line if attributable else False,
                          line.location_id if attributable and not has_plan and 'location_id' in line._fields else False,
                          line.discipline_id if attributable and not has_plan and 'discipline_id' in line._fields else False)
            if remainder > 0 or not candidates:
                buckets[key]['original_budget'] += remainder
                buckets[key]['revised_budget'] += remainder
        mapping = {
            'budget_revision': 'revised_budget', 'commitment': 'commitments', 'receipt': 'received_cost',
            'actual_cost': 'actual_cost', 'variation_revenue': 'variation_revenue',
            'certified_revenue': 'certified_revenue', 'billed_revenue': 'billed_revenue',
        }
        for fact in self._entries(project, as_of):
            column = mapping.get(fact.entry_type)
            if not column:
                continue
            key = key_for(fact.wbs_id, fact.contract_line_id, fact.location_id, fact.discipline_id)
            buckets[key][column] += _number(fact.amount)
        rows = []
        for (wbs_id, line_id, location_id, discipline_id), values in buckets.items():
            wbs = self.env['jakh.wbs'].browse(wbs_id)
            line = self.env['jakh.project.contract.line'].browse(line_id)
            location = self.env['jakh.project.location'].browse(location_id)
            discipline = self.env['jakh.discipline'].browse(discipline_id)
            building = _location_building(location) if location else location
            rows.append({
                **{key: float(value) for key, value in values.items()},
                'wbs_id': wbs_id, 'contract_line_id': line_id, 'location_id': location_id,
                'discipline_id': discipline_id, 'building_id': building.id if building else False,
                'wbs': wbs.display_name,
                'boq': line.display_name if line else _('Unallocated BOQ'),
                'location': location.display_name if location else _('Unallocated location'),
                'building': building.display_name if building else _('Unallocated building'),
                'discipline': discipline.display_name if discipline else _('Unallocated discipline'),
                'dimension_complete': bool(line and location and building),
            })
        return sorted(rows, key=lambda row: (row['wbs'], row['building'], row['location'], row['boq']))

    @api.model
    def _unbilled_rows(self, project, as_of):
        project = self._scope(project)
        if not self.env['account.move'].has_access('read'):
            raise AccessError(_('Accounting document access is required for the unbilled certificate report.'))
        as_of = fields.Date.to_date(as_of)
        certificates = self.env['jakh.payment.certificate'].search([
            ('contract_id.project_id', '=', project.id), ('date', '<=', as_of),
            ('approval_sequence', '>', 0),
        ], order='date,id')
        facts = self.env['jakh.project.control.entry'].search([
            ('project_id', '=', project.id), ('entry_type', 'in', ['certified_revenue', 'billed_revenue']),
            ('date', '<=', as_of),
        ])
        approved_by_certificate = defaultdict(Decimal)
        approved_by_credit = defaultdict(Decimal)
        billed_by_move = defaultdict(Decimal)
        for fact in facts:
            if fact.entry_type == 'certified_revenue' and fact.source_model == 'jakh.payment.certificate':
                approved_by_certificate[fact.source_id] += _number(fact.amount)
            elif fact.entry_type == 'certified_revenue' and fact.source_model == 'account.move':
                approved_by_credit[fact.source_id] += _number(fact.amount)
            elif fact.entry_type == 'billed_revenue' and fact.source_model == 'account.move':
                billed_by_move[fact.source_id] += _number(fact.amount)
        rows = []
        for certificate in certificates:
            # A later cancellation/credit does not rewrite an earlier as-of view.
            # Use signed approval and posting facts, not the current invoice badge.
            original_ids = certificate.invoice_id.ids
            credit_ids = certificate.invoice_id.reversal_move_ids.ids
            approved = approved_by_certificate[certificate.id] + sum((approved_by_credit[identity] for identity in credit_ids), Decimal(0))
            billed = sum((billed_by_move[identity] for identity in original_ids + credit_ids), Decimal(0))
            gap = approved - billed
            if certificate.currency_id.is_zero(float(gap)):
                continue
            invoice = certificate.invoice_id
            credits = invoice.reversal_move_ids.filtered(lambda move: move.state == 'posted' and move.date <= as_of)
            invoice_state = ('credited' if credits else 'posted') if invoice.state == 'posted' and invoice.date <= as_of else (
                'draft' if invoice and invoice.state == 'draft' else 'cancelled' if invoice.state == 'cancel' else 'not_created')
            approved_date = (fields.Date.to_date(certificate.approved_at) if 'approved_at' in certificate._fields
                             and certificate.approved_at else certificate.date)
            rows.append({
                'certificate_id': certificate.id, 'contract_id': certificate.contract_id.id,
                'certificate': certificate.display_name, 'contract': certificate.contract_id.display_name,
                'date': str(certificate.date), 'approved_date': str(approved_date),
                'age_days': max(0, (as_of - approved_date).days), 'approved_gross': float(approved),
                'posted_gross': float(billed), 'unbilled_gross': float(gap), 'invoice_id': invoice.id if invoice else False,
                'invoice_status': invoice_state,
            })
        return rows

    @api.model
    def _basis(self):
        return _('Company currency, tax-exclusive signed approved and posted facts as of the report date. Received exposure is not added to actual costs. Each BOQ/location fact appears once; historical WBS-only data remains unallocated. WBS forecasts remain at project summary level, never silently split by building. Physical progress uses the latest accepted schedule percentage, weighted by original WBS budget; it is undefined if a funded WBS lacks status. Financial progress is approved certified gross divided by revised contract value; billing is posted gross divided by that same value. Values can be negative or exceed 100% after controlled adjustments. Matched receipts are tax-inclusive allocations of posted native payments or bank statements, including advances and retention releases; credits and write-offs are excluded and bank clearance is not asserted. Unbilled includes draft invoices and posted credits not yet acknowledged as cancelled measurements; age starts at approval, or certificate date for historical approvals without a timestamp.')

    @api.model
    def get_portfolio(self):
        require_role(self.env, REPORT_ROLES)
        as_of = fields.Date.context_today(self)
        sections = []
        for company in self.env.companies:
            projects = self.env['project.project'].search([('company_id', '=', company.id), ('active', '=', True)])
            overview = []
            for project in projects:
                summary = self._summary(project, as_of)
                overview.append({'id': project.id, 'name': project.display_name,
                                 **{key: summary[key] for key in ('revised_budget', 'forecast_final_cost', 'forecast_margin',
                                                                 'physical_percent', 'financial_percent', 'cost_performance_index', 'schedule_performance_index')}})
            domain = [('company_id', '=', company.id), ('project_id', 'in', projects.ids), ('date', '<=', as_of)]
            facts = self.env['jakh.project.control.entry'].search(domain)
            cards = []
            for kind, label in [('commitment', _('Approved commitments')), ('actual_cost', _('Posted project costs')),
                                ('certified_revenue', _('Certified gross')), ('billed_revenue', _('Posted billing gross'))]:
                amount = float(sum((_number(fact.amount) for fact in facts if fact.entry_type == kind), Decimal(0)))
                cards.append({'label': label, 'amount': amount, 'action': 'jakh_operations.action_control_entries',
                              'domain': domain + [('entry_type', '=', kind)]})
            alerts = self.env['jakh.project.threshold.alert'].search([
                ('company_id', '=', company.id), ('project_id', 'in', projects.ids), ('state', 'in', ['open', 'escalated']),
            ])
            sections.append({'company': company.display_name, 'company_id': company.id, 'currency': company.currency_id.name,
                             'cards': cards, 'projects': overview, 'alerts': len(alerts),
                             'alert_domain': [('id', 'in', alerts.ids)], 'as_of': str(as_of)})
        return sections


class ConstructionReport(models.TransientModel):
    _name = 'jakh.construction.report'
    _description = 'BOQ and building construction control report'
    _check_company_auto = True

    report_type = fields.Selection([('cost', 'BOQ and building cost'), ('unbilled', 'Approved unbilled certificates')], required=True, default='cost')
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    as_of_date = fields.Date(required=True, default=fields.Date.context_today)
    wbs_id = fields.Many2one('jakh.wbs', check_company=True)
    contract_line_id = fields.Many2one('jakh.project.contract.line', string='BOQ item', check_company=True)
    location_id = fields.Many2one('jakh.project.location', check_company=True)
    discipline_id = fields.Many2one('jakh.discipline', check_company=True)
    generated_at = fields.Datetime(readonly=True)
    basis = fields.Text(readonly=True)
    dimension_complete = fields.Boolean(readonly=True)
    physical_progress = fields.Char(readonly=True)
    financial_progress = fields.Char(readonly=True)
    billing_progress = fields.Char(readonly=True)
    revised_budget = fields.Monetary(readonly=True)
    commitments = fields.Monetary(readonly=True)
    actual_cost = fields.Monetary(readonly=True)
    forecast_final_cost = fields.Monetary(readonly=True)
    forecast_margin = fields.Monetary(readonly=True)
    matched_receipts = fields.Monetary(readonly=True, string='Matched posted receipts (tax-inclusive)')
    receipts_available = fields.Boolean(readonly=True)
    cost_line_ids = fields.One2many('jakh.construction.report.line', 'report_id')
    unbilled_line_ids = fields.One2many('jakh.unbilled.report.line', 'report_id')
    xlsx_attachment_id = fields.Many2one('ir.attachment', readonly=True, copy=False)

    _output_fields = {'generated_at', 'basis', 'dimension_complete', 'physical_progress', 'financial_progress',
                      'billing_progress', 'revised_budget', 'commitments', 'actual_cost', 'forecast_final_cost',
                      'forecast_margin', 'matched_receipts', 'receipts_available', 'xlsx_attachment_id',
                      'company_id', 'currency_id', 'cost_line_ids', 'unbilled_line_ids'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, REPORT_ROLES)
        if any(set(values) & self._output_fields for values in values_list) or any(
                self.env.context.get('default_' + name) not in (None, False) for name in self._output_fields):
            raise AccessError(_('Generate construction report outputs through the report actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, REPORT_ROLES)
        if set(values) & self._output_fields:
            raise AccessError(_('Generate construction report outputs through the report actions.'))
        criteria = {'project_id', 'report_type', 'as_of_date', 'wbs_id', 'contract_line_id', 'location_id', 'discipline_id'}
        if set(values) & criteria:
            lock_records(self, touch=True)
            if self.filtered('xlsx_attachment_id'):
                raise UserError(_('Create a new report to change the scope after exporting a permission-bound snapshot.'))
            self.cost_line_ids._clear_output()
            self.unbilled_line_ids._clear_output()
            values = {**values, 'generated_at': False, 'basis': False, 'dimension_complete': False,
                      'physical_progress': False, 'financial_progress': False, 'billing_progress': False,
                      'revised_budget': 0, 'commitments': 0, 'actual_cost': 0, 'forecast_final_cost': 0,
                      'forecast_margin': 0, 'matched_receipts': 0, 'receipts_available': False}
        return super().write(values)

    @api.constrains('project_id', 'wbs_id', 'contract_line_id', 'location_id', 'discipline_id')
    def _check_scope(self):
        for report in self:
            self.env['jakh.construction.control']._scope(report.project_id)
            if ((report.wbs_id and report.wbs_id.project_id != report.project_id)
                    or (report.contract_line_id and report.contract_line_id.contract_id.project_id != report.project_id)
                    or (report.location_id and report.location_id.project_id != report.project_id)
                    or (report.discipline_id and report.discipline_id.company_id != report.company_id)):
                raise ValidationError(_('Report filters must belong to the selected company and project.'))

    def _payload(self):
        self.ensure_one()
        self.check_access('read')
        self._check_scope()
        Control = self.env['jakh.construction.control']
        rows = Control._cost_rows(self.project_id, self.as_of_date)
        location_ids = self.env['jakh.project.location'].with_context(active_test=False).search([('id', 'child_of', self.location_id.id)]).ids if self.location_id else []
        rows = [row for row in rows if
                (not self.wbs_id or row['wbs_id'] == self.wbs_id.id)
                and (not self.contract_line_id or row['contract_line_id'] == self.contract_line_id.id)
                and (not self.location_id or row['location_id'] in location_ids)
                and (not self.discipline_id or row['discipline_id'] == self.discipline_id.id)]
        summary = Control._summary(self.project_id, self.as_of_date)
        return {
            'company': self.company_id.display_name, 'currency': self.currency_id.name,
            'project': self.project_id.display_name, 'as_of': str(self.as_of_date),
            'generated_at': fields.Datetime.to_string(fields.Datetime.now()), 'basis': Control._basis(),
            'rows': rows, 'totals': {key: float(sum(_number(row[key]) for row in rows)) for key in COST_COLUMNS},
            'summary': summary, 'unbilled': (Control._unbilled_rows(self.project_id, self.as_of_date)
                                           if self.report_type == 'unbilled' or self.env['account.move'].has_access('read') else []),
            'dimension_complete': all(row['dimension_complete'] for row in rows),
            'report_type': self.report_type,
        }

    def _display_percent(self, value):
        return self.env._('Undefined') if value is None else ('%.2f%%' % value)

    def action_generate(self):
        self.ensure_one(); self.check_access('write')
        lock_records(self, touch=True)
        payload = self._payload()
        self.cost_line_ids._clear_output()
        self.unbilled_line_ids._clear_output()
        self.env['jakh.construction.report.line']._generate(self, payload['rows'])
        self.env['jakh.unbilled.report.line']._generate(self, payload['unbilled'])
        summary = payload['summary']
        super().write({
            'generated_at': fields.Datetime.now(), 'basis': payload['basis'],
            'dimension_complete': payload['dimension_complete'],
            'physical_progress': self._display_percent(summary['physical_percent']),
            'financial_progress': self._display_percent(summary['financial_percent']),
            'billing_progress': self._display_percent(summary['billing_percent']),
            'receipts_available': summary['matched_receipts'] is not None,
            **{key: summary[key] for key in ('revised_budget', 'commitments', 'actual_cost', 'forecast_final_cost', 'forecast_margin', 'matched_receipts')},
        })
        self.env['jakh.audit.event']._append(self.company_id, 'construction_report.generated', self, {
            'project': self.project_id.id, 'as_of': str(self.as_of_date), 'type': self.report_type,
        })
        return form_action(self)

    def action_open_detail(self):
        self.ensure_one(); self.action_generate()
        action = self.env.ref('jakh_operations.action_construction_cost_rows').read()[0]
        action['domain'] = [('report_id', '=', self.id)]
        return action

    def action_print_pdf(self):
        self.ensure_one(); self._payload()
        return self.env.ref('jakh_operations.report_construction_control').report_action(self)

    def action_open_receipts(self):
        self.ensure_one(); self.check_access('read')
        receipts = self.env['jakh.construction.control']._collections(self.project_id, self.as_of_date)
        return {'type': 'ir.actions.act_window', 'name': self.env._('Matched posted payment sources'),
                'res_model': 'account.move', 'view_mode': 'list,form',
                'domain': [('id', 'in', receipts['payment_move_ids'])],
                'context': {'search_disable_custom_filters': True}}

    def _table_data(self, payload):
        if self.report_type == 'unbilled':
            columns = ['certificate', 'contract', 'date', 'approved_date', 'age_days', 'approved_gross', 'posted_gross', 'unbilled_gross', 'invoice_status']
            labels = [self.env._(label) for label in ('Certificate', 'Contract', 'Measurement date', 'Approval date', 'Age (days)', 'Approved gross', 'Posted gross', 'Unbilled gross', 'Invoice status')]
            statuses = dict(self.env['jakh.unbilled.report.line']._fields['invoice_status']._description_selection(self.env))
            rows = [{**row, 'invoice_status': statuses.get(row['invoice_status'], row['invoice_status'])} for row in payload['unbilled']]
        else:
            columns = ['wbs', 'boq', 'building', 'location', 'discipline'] + list(COST_COLUMNS)
            labels = [self.env._(label) for label in ('WBS', 'BOQ item', 'Building', 'Location', 'Discipline')]
            field_labels = self.env['jakh.construction.report.line'].fields_get(list(COST_COLUMNS))
            labels += [field_labels[key]['string'] for key in COST_COLUMNS]
            rows = payload['rows']
        return columns, labels, rows

    def _xlsx_bytes(self, payload):
        self.ensure_one(); self.check_access('read')
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
        sheet = workbook.add_worksheet(self.env._('Construction control')[:31])
        if (self.env.context.get('lang') or self.env.user.lang or '').startswith('ar'):
            sheet.right_to_left()
        header = workbook.add_format({'bold': True, 'bg_color': '#dcebe8', 'border': 1})
        money = workbook.add_format({'num_format': '#,##0.00'})
        sheet.merge_range('A1:F1', self.env._('JAKH Construction Control'), header)
        metadata = [(self.env._('Company'), payload['company']), (self.env._('Project'), payload['project']),
                    (self.env._('Currency'), payload['currency']), (self.env._('Report date'), payload['as_of']),
                    (self.env._('Generated at'), payload['generated_at']), (self.env._('Calculation basis'), payload['basis'])]
        for index, (label, value) in enumerate(metadata, 2):
            sheet.write_string(index, 0, label)
            sheet.write_string(index, 1, value)
        if self.company_id.logo:
            # Odoo 20 BinaryValue exposes raw bytes. Decoding it as legacy
            # base64 would corrupt the company image and fail workbook output.
            sheet.insert_image('H1', 'company-logo.png', {'image_data': io.BytesIO(bytes(self.company_id.logo)), 'x_scale': 0.4, 'y_scale': 0.4})
        columns, labels, rows = self._table_data(payload)
        start = len(metadata) + 4
        for column, label in enumerate(labels):
            sheet.write_string(start, column, label, header)
        for index, row in enumerate(rows, start + 1):
            for column, key in enumerate(columns):
                value = row[key]
                if isinstance(value, (int, float)):
                    sheet.write_number(index, column, value, money)
                else:
                    sheet.write_string(index, column, str(value))
        sheet.freeze_panes(start + 1, 5 if self.report_type == 'cost' else 2)
        sheet.autofilter(start, 0, max(start, start + len(rows)), len(columns) - 1)
        sheet.set_column(0, len(columns) - 1, 22)
        sheet.set_column(0, 0, 30)
        if self.report_type == 'cost':
            total_row = start + len(rows) + 2
            sheet.write_string(total_row, 0, self.env._('Filtered detail total'), header)
            for column, key in enumerate(columns):
                if key in payload['totals']:
                    sheet.write_number(total_row, column, payload['totals'][key], money)
            summary_sheet = workbook.add_worksheet(self.env._('Project overview')[:31])
            if (self.env.context.get('lang') or self.env.user.lang or '').startswith('ar'):
                summary_sheet.right_to_left()
            summary_sheet.set_column(0, 0, 40); summary_sheet.set_column(1, 1, 30)
            labels = [('physical_percent', self.env._('Physical progress (%)')), ('financial_percent', self.env._('Certified financial progress (%)')),
                      ('billing_percent', self.env._('Posted billing progress (%)')), ('forecast_final_cost', self.env._('Forecast final cost')),
                      ('forecast_margin', self.env._('Forecast margin')), ('matched_receipts', self.env._('Matched posted receipts (tax-inclusive)'))]
            for index, (key, label) in enumerate(labels):
                summary_sheet.write_string(index, 0, label)
                value = payload['summary'][key]
                if value is None:
                    summary_sheet.write_string(index, 1, self.env._('Undefined'))
                else:
                    summary_sheet.write_number(index, 1, value, money)
            summary_sheet.write_string(len(labels) + 1, 0, self.env._('Project totals are unfiltered; the detail table uses the selected BOQ/location filters.'))
        workbook.close()
        return output.getvalue()

    def action_export_xlsx(self):
        self.ensure_one(); self.check_access('write')
        lock_records(self, touch=True)
        content = self._xlsx_bytes(self._payload())
        attachment = self.env['ir.attachment'].create({
            'name': 'JAKH-%s-%s.xlsx' % (self.report_type, self.as_of_date), 'raw': content,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': self._name, 'res_id': self.id, 'public': False,
        })
        super().write({'xlsx_attachment_id': attachment.id})
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}


class ConstructionReportSchedule(models.Model):
    _inherit = 'jakh.report.schedule'

    report_type = fields.Selection(selection_add=[('construction_cost', 'BOQ and building cost snapshot'),
                                                  ('unbilled', 'Approved unbilled certificate snapshot')],
                                   ondelete={'construction_cost': 'set default', 'unbilled': 'set default'})

    def _snapshot(self, as_of=False):
        if self.report_type not in ('construction_cost', 'unbilled'):
            return super()._snapshot(as_of=as_of)
        self.ensure_one(); self.check_access('read')
        report = self.env['jakh.construction.report'].create({
            'project_id': self.project_id.id, 'as_of_date': as_of or fields.Date.context_today(self),
            'report_type': 'cost' if self.report_type == 'construction_cost' else 'unbilled',
        })
        payload = report._payload()
        return {**payload, 'view_type': report.report_type, 'report_type': self.report_type,
                'generated_by': self.env.uid}


class ConstructionReportDelivery(models.Model):
    _inherit = 'jakh.report.delivery'

    def _create_exports(self, snapshot):
        if snapshot['report_type'] not in ('construction_cost', 'unbilled'):
            return super()._create_exports(snapshot)
        self.ensure_one(); self.check_access('write')
        report = self.env['jakh.construction.report'].create({
            'project_id': self.project_id.id, 'as_of_date': snapshot['as_of'], 'report_type': snapshot['view_type'],
        })
        columns, labels, rows = report._table_data(snapshot)
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        metadata = [(self.env._('Company'), snapshot['company']), (self.env._('Project'), snapshot['project']),
                    (self.env._('Currency'), snapshot['currency']), (self.env._('Report date'), snapshot['as_of']),
                    (self.env._('Calculation basis'), snapshot['basis'])]
        table = [labels] + [[row[key] for key in columns] for row in rows]
        if report.report_type == 'cost':
            table.append([self.env._('Filtered detail total')] + [''] * 4 + [snapshot['totals'][key] for key in COST_COLUMNS])
        for row in metadata + [[]] + table:
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
        prefix = 'JAKH-%s-%s-%s' % (self.report_type, self.schedule_id.id, self.run_on)
        attachments = self.env['ir.attachment'].create([
            {'name': prefix + '.csv', 'raw': b'\xef\xbb\xbf' + csv_buffer.getvalue().encode('utf-8'), 'mimetype': 'text/csv',
             'res_model': self._name, 'res_id': self.id, 'public': False},
            {'name': prefix + '.xlsx', 'raw': report._xlsx_bytes(snapshot),
             'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
             'res_model': self._name, 'res_id': self.id, 'public': False},
        ])
        super(OriginalDelivery, self).write({'csv_attachment_id': attachments[0].id, 'xlsx_attachment_id': attachments[1].id})

    def action_open_report(self):
        if self.report_type not in ('construction_cost', 'unbilled'):
            return super().action_open_report()
        self.ensure_one(); self.check_access('read')
        if self.state != 'generated':
            raise UserError(_('Generate the report successfully before opening its detail.'))
        report = self.env['jakh.construction.report'].create({
            'project_id': self.project_id.id, 'as_of_date': self.run_on,
            'report_type': 'cost' if self.report_type == 'construction_cost' else 'unbilled',
        })
        return report.action_generate()


class ConstructionReportLine(models.TransientModel):
    _name = 'jakh.construction.report.line'
    _description = 'BOQ and location cost report detail'

    report_id = fields.Many2one('jakh.construction.report', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='report_id.company_id', store=True)
    currency_id = fields.Many2one(related='report_id.currency_id')
    wbs_id = fields.Many2one('jakh.wbs', required=True)
    contract_line_id = fields.Many2one('jakh.project.contract.line', string='BOQ item')
    building_id = fields.Many2one('jakh.project.location', string='Building')
    location_id = fields.Many2one('jakh.project.location', string='Location')
    discipline_id = fields.Many2one('jakh.discipline', string='Discipline')
    allocation_status = fields.Selection([('allocated', 'Allocated'), ('unallocated', 'Unallocated / incomplete')], required=True)
    original_budget = fields.Monetary(string='Original budget')
    revised_budget = fields.Monetary(string='Revised budget')
    commitments = fields.Monetary(string='Approved commitments')
    received_cost = fields.Monetary(string='Received exposure')
    actual_cost = fields.Monetary(string='Posted actual cost')
    original_revenue = fields.Monetary(string='Original contract value')
    variation_revenue = fields.Monetary(string='Approved owner changes')
    certified_revenue = fields.Monetary(string='Approved certified gross')
    billed_revenue = fields.Monetary(string='Posted billed gross')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Generate construction report rows through the report wizard.'))

    def write(self, values):
        raise AccessError(_('Generated construction report rows are immutable.'))

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Regenerate the construction report to replace its output.'))

    def _clear_output(self):
        self.check_access('unlink')
        return super().unlink()

    @api.model
    def _generate(self, report, rows):
        report.check_access('write')
        return super().create([{
            'report_id': report.id, **{key: row[key] for key in COST_COLUMNS},
            **{key: row[key] for key in ('wbs_id', 'contract_line_id', 'building_id', 'location_id', 'discipline_id')},
            'allocation_status': 'allocated' if row['dimension_complete'] else 'unallocated',
        } for row in rows]) if rows else self.browse()

    def action_open_sources(self):
        self.ensure_one(); self.check_access('read')
        report = self.report_id
        self.env['jakh.construction.control']._scope(report.project_id)
        domain = [('project_id', '=', report.project_id.id), ('date', '<=', report.as_of_date), ('wbs_id', '=', self.wbs_id.id),
                  ('contract_line_id', '=', self.contract_line_id.id or False),
                  ('location_id', '=', self.location_id.id or False), ('discipline_id', '=', self.discipline_id.id or False)]
        return {'type': 'ir.actions.act_window', 'name': self.env._('Contributing project facts'),
                'res_model': 'jakh.project.control.entry', 'view_mode': 'list,form', 'domain': domain,
                'context': {'search_disable_custom_filters': True}}


class UnbilledReportLine(models.TransientModel):
    _name = 'jakh.unbilled.report.line'
    _description = 'Approved unbilled certificate detail'

    report_id = fields.Many2one('jakh.construction.report', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='report_id.company_id', store=True)
    currency_id = fields.Many2one(related='report_id.currency_id')
    certificate_id = fields.Many2one('jakh.payment.certificate', required=True)
    contract_id = fields.Many2one('jakh.project.contract', required=True)
    date = fields.Date(string='Measurement date')
    approved_date = fields.Date(string='Approval date')
    age_days = fields.Integer(string='Age (days)')
    approved_gross = fields.Monetary(string='Approved gross')
    posted_gross = fields.Monetary(string='Posted gross')
    unbilled_gross = fields.Monetary(string='Unbilled gross')
    invoice_id = fields.Many2one('account.move')
    invoice_status = fields.Selection([('not_created', 'No invoice'), ('draft', 'Draft invoice'), ('posted', 'Posted invoice'),
                                       ('credited', 'Posted credit / measurement review'), ('cancelled', 'Cancelled draft')])

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Generate construction report rows through the report wizard.'))

    def write(self, values):
        raise AccessError(_('Generated construction report rows are immutable.'))

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Regenerate the construction report to replace its output.'))

    def _clear_output(self):
        self.check_access('unlink')
        return super().unlink()

    @api.model
    def _generate(self, report, rows):
        report.check_access('write')
        columns = ('certificate_id', 'contract_id', 'date', 'approved_date', 'age_days',
                   'approved_gross', 'posted_gross', 'unbilled_gross', 'invoice_id', 'invoice_status')
        return super().create([{'report_id': report.id, **{key: row[key] for key in columns}} for row in rows]) if rows else self.browse()

    def action_open_certificate(self):
        self.ensure_one(); self.check_access('read')
        self.env['jakh.construction.control']._scope(self.report_id.project_id)
        return form_action(self.certificate_id)


class ConstructionPdf(models.AbstractModel):
    _name = 'report.jakh_operations.construction_control_document'
    _description = 'Permission-scoped construction PDF'

    @api.model
    def _get_report_values(self, docids, data=None):
        reports = self.env['jakh.construction.report'].browse(docids)
        reports.check_access('read')
        payloads = {report.id: report._payload() for report in reports}
        return {'doc_ids': docids, 'doc_model': reports._name, 'docs': reports, 'payloads': payloads}


def _company_workbook(env, company, title, metadata, headers, rows):
    """Presentation only: callers supply the existing governed calculations."""
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
    sheet = workbook.add_worksheet(env._('Report')[:31])
    if (env.context.get('lang') or env.user.lang or '').startswith('ar'):
        sheet.right_to_left()
    heading = workbook.add_format({'bold': True, 'bg_color': '#dcebe8', 'border': 1})
    number = workbook.add_format({'num_format': '#,##0.00'})
    sheet.merge_range(0, 0, 0, max(3, len(headers) - 1), title, heading)
    if company.logo:
        sheet.insert_image('H2', 'company-logo.png', {'image_data': io.BytesIO(bytes(company.logo)), 'x_scale': 0.4, 'y_scale': 0.4})
    for index, row in enumerate(metadata, 2):
        for column, value in enumerate(row):
            sheet.write_string(index, column, str(value))
    start = len(metadata) + 4
    for column, value in enumerate(headers):
        sheet.write_string(start, column, value, heading)
    for index, row in enumerate(rows, start + 1):
        for column, value in enumerate(row):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sheet.write_number(index, column, value, number)
            else:
                sheet.write_string(index, column, str(value))
    sheet.freeze_panes(start + 1, 1)
    sheet.autofilter(start, 0, max(start, start + len(rows)), len(headers) - 1)
    sheet.set_column(0, len(headers) - 1, 24)
    workbook.close()
    return output.getvalue()


class CashForecastPresentation(models.TransientModel):
    _inherit = 'jakh.project.cashflow.report'

    def _presentation_payload(self):
        self.ensure_one(); self.check_access('read')
        rows = self._calculation_rows(self.project_id, self.date_from, self.date_to, self.opening_cash)
        return {'rows': rows, 'basis': self._basis_note(), 'generated_at': fields.Datetime.to_string(fields.Datetime.now()),
                'closing_cash': rows[-1]['closing_cash'] if rows else self.opening_cash}

    def action_print_pdf(self):
        self.ensure_one(); self._presentation_payload()
        return self.env.ref('jakh_operations.report_project_cash_forecast').report_action(self)

    def action_export_xlsx(self):
        self.ensure_one(); self.check_access('write')
        payload = self._presentation_payload()
        metadata = [(self.env._('Company'), self.company_id.display_name), (self.env._('Project'), self.project_id.display_name),
                    (self.env._('Currency'), self.currency_id.name), (self.env._('From date'), str(self.date_from)),
                    (self.env._('To date'), str(self.date_to)), (self.env._('Calculation basis'), payload['basis']),
                    (self.env._('Opening project cash assumption'), str(self.opening_cash))]
        headers = [self.env._('Month'), self.env._('Cash in'), self.env._('Cash out'), self.env._('Net cash'), self.env._('Closing cash')]
        rows = [[str(row['period']), row['cash_in'], row['cash_out'], row['net_cash'], row['closing_cash']] for row in payload['rows']]
        attachment = self.env['ir.attachment'].create({
            'name': 'JAKH-cash-forecast-%s.xlsx' % self.date_to,
            'raw': _company_workbook(self.env, self.company_id, self.env._('Project cash forecast'), metadata, headers, rows),
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': self._name, 'res_id': self.id, 'public': False,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}


class CashForecastPdf(models.AbstractModel):
    _name = 'report.jakh_operations.project_cash_forecast_document'
    _description = 'Permission-scoped cash forecast PDF'

    @api.model
    def _get_report_values(self, docids, data=None):
        reports = self.env['jakh.project.cashflow.report'].browse(docids)
        reports.check_access('read')
        return {'doc_ids': docids, 'doc_model': reports._name, 'docs': reports,
                'payloads': {report.id: report._presentation_payload() for report in reports}}


class FinancialReportPdfAction(models.TransientModel):
    _inherit = 'jakh.financial.report'

    def action_print_pdf(self):
        self.ensure_one(); self.action_generate()
        return self.env.ref('jakh_operations.report_financial_control').report_action(self)


class FinancialControlPdf(models.AbstractModel):
    _name = 'report.jakh_operations.financial_control_document'
    _description = 'Posted-ledger company-branded PDF'

    @api.model
    def _get_report_values(self, docids, data=None):
        reports = self.env['jakh.financial.report'].browse(docids)
        reports.check_access('read')
        # Reuse the native report's current-permission posted-ledger computation.
        # Nothing is posted, reconciled or reclassified by presentation code.
        for report in reports:
            report.action_generate()
        return {'doc_ids': docids, 'doc_model': reports._name, 'docs': reports,
                'generated_at': fields.Datetime.to_string(fields.Datetime.now())}


class ProjectThresholdPolicy(models.Model):
    _name = 'jakh.project.threshold.policy'
    _description = 'Approved project alert threshold'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    owner_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user)
    metric = fields.Selection(METRICS, required=True, default='forecast_overrun_percent')
    comparison = fields.Selection([('gt', 'Greater than'), ('lt', 'Less than')], required=True, default='gt')
    threshold = fields.Float(required=True, digits=(16, 4))
    escalation_days = fields.Integer(required=True, default=3)
    state = fields.Selection([('draft', 'Draft'), ('active', 'Approved / active'), ('retired', 'Retired')], required=True, default='draft', readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    alert_ids = fields.One2many('jakh.project.threshold.alert', 'policy_id', readonly=True)
    last_checked_at = fields.Datetime(readonly=True)
    evaluation_note = fields.Char(readonly=True)
    retirement_reason = fields.Text(string='Retirement reason')
    _server_fields = {'state', 'approved_by', 'approved_at', 'last_checked_at', 'evaluation_note',
                      'company_id', 'currency_id', 'alert_ids'}

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, REPORT_ROLES)
        if any(set(values) & self._server_fields for values in values_list) or any(
                self.env.context.get('default_' + name) not in (None, False) for name in self._server_fields):
            raise AccessError(_('Use the controlled project threshold actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, REPORT_ROLES)
        if set(values) == {'retirement_reason'} and all(row.state == 'active' for row in self):
            require_role(self.env, ['group_manager', 'group_finance'])
            return super().write(values)
        if set(values) & self._server_fields or self.filtered(lambda row: row.state != 'draft'):
            raise AccessError(_('Approved threshold rules are immutable; retire the rule and create its replacement.'))
        return super().write(values)

    def unlink(self):
        if self.filtered(lambda row: row.state != 'draft'):
            raise UserError(_('Approved threshold rules and their alert history cannot be deleted.'))
        return super().unlink()

    @api.constrains('threshold', 'escalation_days', 'project_id', 'owner_id')
    def _check_policy(self):
        for policy in self:
            self.env['jakh.construction.control']._scope(policy.project_id)
            if not math.isfinite(policy.threshold) or policy.escalation_days < 1:
                raise ValidationError(_('Use a finite threshold and at least one day before escalation.'))
            if not policy.owner_id.active or policy.company_id not in policy.owner_id.company_ids:
                raise ValidationError(_('The alert owner must be active and authorised for this company.'))

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for policy in self:
            lock_records(policy)
            if policy.state != 'draft':
                raise UserError(_('Approve a draft threshold rule.'))
            owner = policy.with_user(policy.owner_id).with_context(allowed_company_ids=policy.company_id.ids)
            owner.env['jakh.construction.control']._scope(owner.project_id)
            if policy.metric in ('unbilled_amount', 'unbilled_days') and not owner.env['account.move'].has_access('read'):
                raise AccessError(_('Accounting document access is required for the unbilled certificate report.'))
            super(ProjectThresholdPolicy, policy).write({'state': 'active', 'approved_by': self.env.uid, 'approved_at': fields.Datetime.now()})
            self.env['jakh.audit.event']._append(policy.company_id, 'threshold.approved', policy, {
                'metric': policy.metric, 'comparison': policy.comparison, 'threshold': policy.threshold, 'owner': policy.owner_id.id,
            })
        return True

    def action_retire(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for policy in self:
            lock_records(policy)
            if policy.state != 'active':
                raise UserError(_('Retire an active threshold rule.'))
            if not (policy.retirement_reason or '').strip():
                raise UserError(_('Document a reason before retiring a project threshold rule.'))
            super(ProjectThresholdPolicy, policy).write({'state': 'retired'})
            policy.alert_ids.filtered(lambda alert: alert.state != 'resolved')._transition(
                state='resolved', resolved_at=fields.Datetime.now(), resolution_note=policy.retirement_reason)
            self.env['jakh.audit.event']._append(policy.company_id, 'threshold.retired', policy, {'reason': policy.retirement_reason})
        return True

    def _metric_value(self):
        self.ensure_one()
        summary = self.env['jakh.construction.control']._summary(self.project_id, fields.Date.context_today(self))
        if self.metric == 'commitment_percent':
            return _percent(summary['commitments'], summary['revised_budget'])
        if self.metric == 'forecast_overrun_percent':
            return _percent(summary['forecast_final_cost'] - summary['revised_budget'], summary['revised_budget'])
        if self.metric == 'forecast_margin_percent':
            return _percent(summary['forecast_margin'], summary['revised_revenue'])
        return summary[{'cpi': 'cost_performance_index', 'spi': 'schedule_performance_index'}.get(self.metric, self.metric)]

    def _breaches(self, value):
        return value is not None and (value > self.threshold if self.comparison == 'gt' else value < self.threshold)

    def _evaluate(self):
        self.ensure_one(); self.check_access('write')
        if self.state != 'active':
            return False
        lock_records(self)
        value = self._metric_value()
        now = fields.Datetime.now()
        super(ProjectThresholdPolicy, self).write({'last_checked_at': now, 'evaluation_note': self.env._('Undefined measure; existing alerts remain open.') if value is None else False})
        Alert = self.env['jakh.project.threshold.alert']
        alert = Alert.search([('policy_id', '=', self.id)], limit=1)
        if value is None:
            return alert
        if self._breaches(value):
            if not alert:
                alert = Alert._record(self, value)
            elif alert.state == 'resolved':
                alert._transition(state='open', observed_value=value, opened_at=now,
                                  occurrence_count=alert.occurrence_count + 1, resolved_at=False, resolution_note=False,
                                  escalation_at=False)
            else:
                alert._transition(observed_value=value)
            if alert.state == 'open' and (now - alert.opened_at).total_seconds() >= self.escalation_days * 86400:
                alert._transition(state='escalated', escalation_at=now)
                alert.message_post(body=self.env._('The project exception exceeded its escalation deadline. The rule owner must review the contributing records.'))
        elif alert and alert.state != 'resolved':
            alert._transition(state='resolved', observed_value=value, resolved_at=now,
                              resolution_note=self.env._('The governed measure no longer breaches the approved threshold.'))
        return alert

    def action_evaluate(self):
        require_role(self.env, REPORT_ROLES)
        for policy in self:
            policy.check_access('read')
            if not policy.owner_id.active or policy.company_id not in policy.owner_id.company_ids:
                raise AccessError(_('The alert owner no longer has access to this company.'))
            policy.with_user(policy.owner_id).with_context(allowed_company_ids=policy.company_id.ids)._evaluate()
        return True

    @api.model
    def _cron_evaluate(self):
        # Only discover policy metadata as the scheduler. Each business read,
        # notification and alert mutation runs as the current authorised owner.
        for policy in self.sudo().search([('state', '=', 'active')]):
            try:
                with self.env.cr.savepoint():
                    policy.with_user(policy.owner_id).with_context(allowed_company_ids=policy.company_id.ids).action_evaluate()
            except Exception:
                _logger.exception('JAKH project threshold policy %s could not be evaluated under its owner', policy.id)


class ProjectThresholdAlert(models.Model):
    _name = 'jakh.project.threshold.alert'
    _description = 'Governed project exception'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True
    _order = 'opened_at desc,id desc'

    name = fields.Char(required=True, readonly=True)
    policy_id = fields.Many2one('jakh.project.threshold.policy', required=True, readonly=True, ondelete='restrict', check_company=True)
    project_id = fields.Many2one(related='policy_id.project_id', store=True)
    company_id = fields.Many2one(related='policy_id.company_id', store=True)
    owner_id = fields.Many2one(related='policy_id.owner_id', store=True)
    metric = fields.Selection(related='policy_id.metric')
    threshold = fields.Float(related='policy_id.threshold')
    observed_value = fields.Float(readonly=True, digits=(16, 4))
    opened_at = fields.Datetime(readonly=True)
    escalation_at = fields.Datetime(readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    occurrence_count = fields.Integer(readonly=True, default=1)
    state = fields.Selection([('open', 'Open'), ('escalated', 'Escalated'), ('resolved', 'Resolved')], readonly=True, required=True, default='open', tracking=True)
    resolution_note = fields.Text(readonly=True)
    _policy_once = models.Constraint('UNIQUE(policy_id)', 'A policy has one deduplicated exception history.')

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Project exceptions are generated by approved threshold rules.'))

    def write(self, values):
        raise AccessError(_('Use the governed project exception actions.'))

    def unlink(self):
        raise AccessError(_('Project exception history cannot be deleted.'))

    @api.model
    def _record(self, policy, value):
        self.check_access('create')
        alert = super().create({'name': policy.name, 'policy_id': policy.id, 'observed_value': value, 'opened_at': fields.Datetime.now()})
        self.env['jakh.audit.event']._append(policy.company_id, 'threshold.opened', alert, {'value': value, 'threshold': policy.threshold})
        alert.activity_schedule('mail.mail_activity_data_todo', user_id=policy.owner_id.id,
                                date_deadline=fields.Date.context_today(self) + timedelta(days=policy.escalation_days),
                                summary=self.env._('Review project threshold exception'))
        return alert

    def _transition(self, **values):
        self.check_access('write')
        result = super().write(values)
        for alert in self:
            if 'state' in values:
                self.env['jakh.audit.event']._append(alert.company_id, 'threshold.' + alert.state, alert,
                                                    {'value': alert.observed_value, 'occurrence': alert.occurrence_count})
                if alert.state == 'resolved':
                    alert.activity_feedback(['mail.mail_activity_data_todo'], feedback=alert.resolution_note)
                elif alert.state == 'open':
                    alert.activity_schedule('mail.mail_activity_data_todo', user_id=alert.owner_id.id,
                                            date_deadline=fields.Date.context_today(self) + timedelta(days=alert.policy_id.escalation_days),
                                            summary=self.env._('Review project threshold exception'))
        return result

    def action_recheck(self):
        for alert in self:
            alert.check_access('read')
            alert.policy_id.action_evaluate()
        return True

    def action_open_report(self):
        self.ensure_one(); self.check_access('read')
        self.env['jakh.construction.control']._scope(self.project_id)
        return {'type': 'ir.actions.act_window', 'res_model': 'jakh.construction.report', 'view_mode': 'form',
                'context': {'default_project_id': self.project_id.id,
                            'default_report_type': 'unbilled' if self.metric in ('unbilled_amount', 'unbilled_days') else 'cost'}}
