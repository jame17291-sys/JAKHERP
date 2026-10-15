"""One calculation basis for interactive reports and permission-bound schedules."""
import csv
import io
import json
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import xlsxwriter
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .execution_controls import (
    ProjectCashForecast as OriginalCashForecast,
    ProjectCashflowReport as OriginalCashflowReport,
    ReportDelivery as OriginalDelivery,
    ReportSchedule as OriginalSchedule,
    _require_project_scope,
)
from .project_control import ProjectControlReport as OriginalControlReport
from .procurement import FINANCIAL, creation_parents, form_action, lock_editable, lock_records, require_role
from ..wizard.financial_report import FinancialReport as OriginalFinancialReport

_logger = logging.getLogger(__name__)
REPORT_ROLES = ['group_manager', 'group_project_manager', 'group_finance']
MONEY_COLUMNS = (
    'original_budget', 'revised_budget', 'commitments', 'received_cost', 'actual_cost',
    'forecast_final_cost', 'forecast_to_complete', 'original_revenue', 'variation_revenue',
    'variation_cost', 'revised_revenue', 'certified_revenue', 'billed_revenue', 'forecast_margin',
    'planned_value', 'earned_value', 'cash_in_forecast', 'cash_out_forecast', 'forecast_cash_net',
)


def _totals(rows):
    """Indices are ratios of sums, never a sum or average of WBS ratios."""
    totals = {key: float(sum(Decimal(str(row[key])) for row in rows)) for key in MONEY_COLUMNS}
    totals['cost_performance_index'] = totals['earned_value'] / totals['actual_cost'] if totals['actual_cost'] > 0 else None
    totals['schedule_performance_index'] = totals['earned_value'] / totals['planned_value'] if totals['planned_value'] > 0 else None
    return totals


class ProjectControlReport(models.TransientModel):
    _inherit = 'jakh.project.control.report'

    total_cost_performance_index = fields.Float(string='Project CPI', compute='_compute_project_indices', digits=(16, 4))
    total_schedule_performance_index = fields.Float(string='Project SPI', compute='_compute_project_indices', digits=(16, 4))
    total_cpi_display = fields.Char(string='Project CPI', compute='_compute_project_indices')
    total_spi_display = fields.Char(string='Project SPI', compute='_compute_project_indices')

    @api.depends('line_ids.actual_cost', 'line_ids.planned_value', 'line_ids.earned_value')
    def _compute_project_indices(self):
        for report in self:
            actual = sum(report.line_ids.mapped('actual_cost'))
            planned = sum(report.line_ids.mapped('planned_value'))
            earned = sum(report.line_ids.mapped('earned_value'))
            report.total_cost_performance_index = earned / actual if actual > 0 else 0
            report.total_schedule_performance_index = earned / planned if planned > 0 else 0
            report.total_cpi_display = '%.4f' % (earned / actual) if actual > 0 else self.env._('Undefined')
            report.total_spi_display = '%.4f' % (earned / planned) if planned > 0 else self.env._('Undefined')

    @api.model
    def _calculation_rows(self, project, as_of):
        require_role(self.env, REPORT_ROLES)
        project = self.env['project.project'].browse(project.id)
        project.check_access('read')
        if project.company_id not in self.env.companies:
            raise AccessError(_('Select this project company in the company switcher.'))
        as_of = fields.Date.to_date(as_of)
        entries = self.env['jakh.project.control.entry'].search([
            ('project_id', '=', project.id), ('date', '<=', as_of),
        ])
        grouped = defaultdict(lambda: defaultdict(Decimal))
        latest_forecast = {}
        for entry in entries:
            if entry.entry_type == 'forecast_final_cost':
                current = latest_forecast.get(entry.wbs_id.id)
                if not current or (entry.date, entry.id) > (current.date, current.id):
                    latest_forecast[entry.wbs_id.id] = entry
            elif entry.entry_type not in ('planned_value', 'earned_value', 'cash_in_forecast', 'cash_out_forecast'):
                grouped[entry.wbs_id.id][entry.entry_type] += Decimal(str(entry.amount))
        # P6 values are cumulative status observations. A later status replaces
        # the previous observation for that WBS; historic statuses remain auditable.
        progress = self.env['jakh.project.schedule.progress'].search([
            ('project_id', '=', project.id), ('state', '=', 'accepted'), ('as_of_date', '<=', as_of),
        ], order='as_of_date desc, id desc')
        latest_progress = {}
        for status in progress:
            latest_progress.setdefault(status.wbs_id.id, status)
        for forecast in self.env['jakh.project.cash.forecast'].search([
            ('project_id', '=', project.id), ('state', '=', 'approved'), ('expected_date', '<=', as_of),
        ]):
            key = 'cash_in_forecast' if forecast.cash_direction == 'in' else 'cash_out_forecast'
            grouped[forecast.wbs_id.id][key] += Decimal(str(forecast.amount))
        revenue = defaultdict(Decimal)
        contracts = self.env['jakh.project.contract'].search([
            ('project_id', '=', project.id), ('state', 'in', ['active', 'closed']),
        ])
        for line in contracts.line_ids:
            revenue[line.wbs_id.id] += Decimal(str(line.quantity)) * Decimal(str(line.unit_price))
        rows = []
        for wbs in self.env['jakh.wbs'].search([('project_id', '=', project.id)], order='sequence, code, id'):
            facts = grouped[wbs.id]
            budget = Decimal(str(wbs.budget_amount))
            revised_budget = budget + facts['budget_revision']
            actual = facts['actual_cost']
            final_cost = Decimal(str(latest_forecast[wbs.id].amount)) if wbs.id in latest_forecast else revised_budget
            status = latest_progress.get(wbs.id)
            planned = Decimal(str(status.planned_value)) if status else Decimal(0)
            earned = Decimal(str(status.earned_value)) if status else Decimal(0)
            revised_revenue = revenue[wbs.id] + facts['variation_revenue']
            rows.append({
                'wbs_id': wbs.id, 'original_budget': float(budget), 'revised_budget': float(revised_budget),
                'commitments': float(facts['commitment']), 'received_cost': float(facts['receipt']),
                'actual_cost': float(actual), 'forecast_final_cost': float(final_cost),
                'forecast_to_complete': float(max(Decimal(0), final_cost - actual)),
                'original_revenue': float(revenue[wbs.id]), 'variation_revenue': float(facts['variation_revenue']),
                'variation_cost': float(facts['variation_cost']), 'revised_revenue': float(revised_revenue),
                'certified_revenue': float(facts['certified_revenue']), 'billed_revenue': float(facts['billed_revenue']),
                'forecast_margin': float(revised_revenue - final_cost), 'planned_value': float(planned),
                'earned_value': float(earned), 'cost_performance_index': float(earned / actual) if actual > 0 else 0,
                'schedule_performance_index': float(earned / planned) if planned > 0 else 0,
                'cash_in_forecast': float(facts['cash_in_forecast']), 'cash_out_forecast': float(facts['cash_out_forecast']),
                'forecast_cash_net': float(facts['cash_in_forecast'] - facts['cash_out_forecast']),
            })
        return rows

    @api.model
    def _basis_note(self):
        return _('Company-currency project control as of the selected date. Actual costs are posted accounting costs, including approved labour/plant journals. Received exposure is separate. Forecast final cost uses the latest approved WBS forecast, or revised budget. Planned and earned values use the latest accepted cumulative schedule status per WBS on or before the report date. Project CPI and SPI are ratios of total earned value to total actual cost and total planned value. An index is undefined where its denominator is not positive. Cash values use current approved forecasts due on or before that date.')

    def action_generate(self):
        self.ensure_one()
        self.check_access('write')
        rows = self._calculation_rows(self.project_id, self.as_of_date)
        self.line_ids._clear_report()
        self.env['jakh.project.control.report.line']._create_report([{**row, 'wizard_id': self.id} for row in rows])
        super(OriginalControlReport, self).write({'scope_note': self._basis_note()})
        self.env['jakh.audit.event']._append(self.company_id, 'project_control.report_generated', self, {
            'project': self.project_id.id, 'rows': len(rows), 'as_of': str(self.as_of_date),
        })
        return form_action(self)


class ProjectControlReportLine(models.TransientModel):
    _inherit = 'jakh.project.control.report.line'

    # Native pivot sum/average would misstate project indices. WBS indices stay
    # available in list output; the report computes project ratios explicitly.
    cost_performance_index = fields.Float(aggregator=None)
    schedule_performance_index = fields.Float(aggregator=None)
    cost_performance_display = fields.Char(string='CPI', compute='_compute_index_display')
    schedule_performance_display = fields.Char(string='SPI', compute='_compute_index_display')

    @api.depends('actual_cost', 'planned_value', 'earned_value')
    def _compute_index_display(self):
        for line in self:
            line.cost_performance_display = '%.4f' % (line.earned_value / line.actual_cost) if line.actual_cost > 0 else self.env._('Undefined')
            line.schedule_performance_display = '%.4f' % (line.earned_value / line.planned_value) if line.planned_value > 0 else self.env._('Undefined')


class ProjectCashForecast(models.Model):
    _inherit = 'jakh.project.cash.forecast'

    replaces_id = fields.Many2one('jakh.project.cash.forecast', string='Replaces forecast', copy=False, check_company=True)
    replaced_by_id = fields.Many2one('jakh.project.cash.forecast', string='Replacement forecast', readonly=True, copy=False, check_company=True)
    cancellation_reason = fields.Text(string='Cancellation reason', copy=False)
    cancelled_at = fields.Datetime(readonly=True, copy=False, string='Cancelled at')
    state = fields.Selection(selection_add=[('superseded', 'Superseded'), ('cancelled', 'Cancelled')], ondelete={'superseded': 'set default', 'cancelled': 'set default'})
    _server_fields = OriginalCashForecast._server_fields | {'replaced_by_id', 'cancelled_at'}

    @api.constrains('replaces_id', 'project_id', 'wbs_id', 'cash_direction')
    def _check_replacement_scope(self):
        for forecast in self.filtered('replaces_id'):
            old = forecast.replaces_id
            old.check_access('read')
            if old == forecast or old.project_id != forecast.project_id or old.wbs_id != forecast.wbs_id or old.cash_direction != forecast.cash_direction:
                raise ValidationError(_('A replacement cash forecast must retain the original project, WBS and direction.'))

    def write(self, values):
        if set(values) == {'cancellation_reason'} and all(row.state == 'approved' for row in self):
            require_role(self.env, ['group_manager', 'group_finance'])
            return super(OriginalCashForecast, self).write(values)
        return super().write(values)

    def _reverse_projection(self, suffix):
        self.ensure_one()
        self.env['jakh.project.control.entry']._record(
            name=self.name, company=self.company_id, project=self.project_id, wbs=self.wbs_id,
            entry_type='cash_in_forecast' if self.cash_direction == 'in' else 'cash_out_forecast',
            amount=-self.amount, source_model=self._name, source_id=self.id,
            source_key='%s:%s:%s' % (self._name, self.id, suffix), entry_date=self.expected_date,
            notes=self.source_reference,
        )

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            if row.state == 'approved':
                continue
            lock_records(row.wbs_id, budget=True, touch=True)
            old = row.replaces_id
            if old:
                lock_records(old)
                if old.state != 'approved' or old.replaced_by_id:
                    raise UserError(_('Replace only the current approved cash forecast.'))
            duplicate = self.search([
                ('project_id', '=', row.project_id.id), ('wbs_id', '=', row.wbs_id.id),
                ('cash_direction', '=', row.cash_direction), ('source_reference', '=', row.source_reference),
                ('state', '=', 'approved'), ('id', 'not in', (row | old).ids),
            ], limit=1)
            if duplicate:
                raise UserError(_('This source already has an approved cash forecast. Create a linked replacement.'))
            super(ProjectCashForecast, row).action_approve()
            if old:
                old._reverse_projection('superseded')
                old._transition(state='superseded', replaced_by_id=row.id)
        return True

    def action_cancel(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for row in self:
            lock_records(row)
            _require_project_scope(row)
            if row.state == 'cancelled':
                continue
            if row.state != 'approved' or not (row.cancellation_reason or '').strip():
                raise UserError(_('Cancel an approved cash forecast with a documented reason.'))
            lock_records(row.wbs_id, budget=True, touch=True)
            row._reverse_projection('cancelled')
            row._transition(state='cancelled', cancelled_at=fields.Datetime.now())
        return True


class ProjectCashflowReport(models.TransientModel):
    _inherit = 'jakh.project.cashflow.report'

    @api.model
    def _calculation_rows(self, project, date_from, date_to, opening_cash=0):
        require_role(self.env, REPORT_ROLES)
        project = self.env['project.project'].browse(project.id)
        project.check_access('read')
        if project.company_id not in self.env.companies:
            raise AccessError(_('Select this project company in the company switcher.'))
        date_from, date_to = fields.Date.to_date(date_from), fields.Date.to_date(date_to)
        if date_from > date_to:
            raise UserError(_('The cash forecast end date must be on or after the start date.'))
        forecasts = self.env['jakh.project.cash.forecast'].search([
            ('project_id', '=', project.id), ('state', '=', 'approved'),
            ('expected_date', '>=', date_from), ('expected_date', '<=', date_to),
        ])
        monthly = defaultdict(lambda: defaultdict(Decimal))
        for forecast in forecasts:
            monthly[forecast.expected_date.replace(day=1)][forecast.cash_direction] += Decimal(str(forecast.amount))
        rows, running = [], Decimal(str(opening_cash))
        month = date_from.replace(day=1)
        while month <= date_to:
            cash_in, cash_out = monthly[month]['in'], monthly[month]['out']
            net = cash_in - cash_out
            running += net
            rows.append({'period': month, 'cash_in': float(cash_in), 'cash_out': float(cash_out),
                         'net_cash': float(net), 'closing_cash': float(running)})
            month += relativedelta(months=1)
        return rows

    @api.model
    def _basis_note(self):
        return _('Company-currency forecast of current approved project cash plans by expected date. Replaced and cancelled plans are excluded. Opening project cash is the entered planning assumption. This report is a cash forecast and does not assert bank reconciliation or statutory cash flow.')

    def action_generate(self):
        self.ensure_one()
        self.check_access('write')
        rows = self._calculation_rows(self.project_id, self.date_from, self.date_to, self.opening_cash)
        self.line_ids._clear_report()
        self.env['jakh.project.cashflow.report.line']._create_report([{**row, 'wizard_id': self.id} for row in rows])
        super(OriginalCashflowReport, self).write({'scope_note': self._basis_note()})
        self.env['jakh.audit.event']._append(self.company_id, 'cash_forecast.report_generated', self, {'rows': len(rows)})
        return form_action(self)


class ReportSchedule(models.Model):
    _inherit = 'jakh.report.schedule'

    cash_horizon_days = fields.Integer(default=180, required=True, string='Cash forecast horizon (days)')
    opening_cash = fields.Monetary(default=0, currency_field='currency_id', groups=FINANCIAL, string='Opening project cash assumption')
    currency_id = fields.Many2one(related='company_id.currency_id')

    @api.constrains('cash_horizon_days')
    def _check_cash_horizon(self):
        if any(not 1 <= row.cash_horizon_days <= 1095 for row in self):
            raise ValidationError(_('Use a cash forecast horizon between 1 and 1095 days.'))

    def write(self, values):
        if set(values) & {'project_id', 'owner_id', 'report_type'} and any(row.delivery_ids for row in self):
            raise UserError(_('Create a new schedule to change the owner, project or report type after a report run.'))
        return super().write(values)

    def _owner_schedule(self):
        """Cron may discover metadata with sudo; every report read uses this env."""
        self.ensure_one()
        owner = self.owner_id
        if not owner.active or self.company_id not in owner.company_ids:
            raise AccessError(_('The report owner is inactive or no longer has access to this legal entity.'))
        schedule = self.with_user(owner).sudo(False).with_context(allowed_company_ids=[self.company_id.id])
        require_role(schedule.env, REPORT_ROLES)
        schedule.check_access('read')
        schedule.project_id.check_access('read')
        return schedule

    def _snapshot(self, as_of=False):
        self.ensure_one()
        self.check_access('read')
        self.project_id.check_access('read')
        as_of = fields.Date.to_date(as_of or fields.Date.context_today(self))
        snapshot = {
            'report_type': self.report_type, 'as_of': str(as_of), 'project': self.project_id.name,
            'company': self.company_id.name, 'currency': self.company_id.currency_id.name,
            'generated_by': self.env.user.id,
        }
        if self.report_type == 'cvr':
            Report = self.env['jakh.project.control.report']
            rows = Report._calculation_rows(self.project_id, as_of)
            for row in rows:
                row['wbs'] = self.env['jakh.wbs'].browse(row['wbs_id']).display_name
                if row['actual_cost'] <= 0:
                    row['cost_performance_index'] = None
                if row['planned_value'] <= 0:
                    row['schedule_performance_index'] = None
            snapshot.update({'basis': Report._basis_note(), 'rows': rows, 'totals': _totals(rows)})
            # Keep the established top-level measures for existing consumers.
            snapshot.update(snapshot['totals'])
        else:
            Report = self.env['jakh.project.cashflow.report']
            end = as_of + timedelta(days=self.cash_horizon_days)
            rows = Report._calculation_rows(self.project_id, as_of, end, self.opening_cash)
            snapshot.update({
                'basis': Report._basis_note(), 'date_from': str(as_of), 'date_to': str(end),
                'opening_cash': self.opening_cash,
                'rows': [{**row, 'period': str(row['period'])} for row in rows],
                'cash_in_forecast': float(sum(Decimal(str(row['cash_in'])) for row in rows)),
                'cash_out_forecast': float(sum(Decimal(str(row['cash_out'])) for row in rows)),
                'closing_cash': rows[-1]['closing_cash'] if rows else self.opening_cash,
            })
        return snapshot

    def _run_once(self, scheduled=False, run_on=False, run_key=False):
        self.ensure_one()
        self.check_access('write')
        if self.state != 'active':
            return self.env['jakh.report.delivery'].browse()
        lock_records(self, touch=True)
        date = fields.Date.to_date(run_on or (self.next_run_on if scheduled else fields.Date.context_today(self)))
        key = run_key or ('due:%s' % date if scheduled else 'manual:%s' % uuid4().hex)
        # Elevated operations here hold only scheduler metadata: history, failure
        # status and the next-run cursor. No business data is read through sudo.
        Delivery = self.env['jakh.report.delivery'].sudo()
        existing = Delivery.search([('schedule_id', '=', self.id), ('run_key', '=', key)], limit=1)
        if existing.state == 'generated':
            return existing.with_env(self.env)
        try:
            with self.env.cr.savepoint():
                owner_schedule = self._owner_schedule()
                snapshot = owner_schedule._snapshot(as_of=date)
                delivery = owner_schedule.env['jakh.report.delivery']._record_run(owner_schedule, snapshot, date, key, existing=existing)
                delivery._create_exports(snapshot)
                super(OriginalSchedule, owner_schedule).write({'last_run_at': fields.Datetime.now()})
                owner_schedule.env['jakh.audit.event']._append(owner_schedule.company_id, 'report_schedule.generated', owner_schedule, {
                    'delivery': delivery.id, 'type': owner_schedule.report_type, 'run_key': key,
                })
            if scheduled and self.next_run_on == date:
                super(OriginalSchedule, self.sudo()).write({'next_run_on': self._next_date(date)})
            return delivery.with_env(self.env)
        except Exception as exc:
            if isinstance(exc, AccessError):
                message = _('The owner no longer has permission to generate this report. Restore access or replace the schedule.')
            elif isinstance(exc, (UserError, ValidationError)):
                message = str(exc)
            else:
                _logger.exception('JAKH scheduled report %s failed', self.id)
                message = _('Report generation failed. Review the server log, correct the cause and retry.')
            failure = Delivery._record_run(self.sudo(), {}, date, key, existing=existing, error=message)
            self.env['jakh.audit.event']._append(self.company_id, 'report_schedule.failed', self, {
                'delivery': failure.id, 'run_key': key, 'error_type': type(exc).__name__,
            })
            return failure.with_env(self.env)

    def action_run_now(self):
        require_role(self.env, REPORT_ROLES)
        for row in self:
            row.check_access('write')
            row._run_once()
        return True

    @api.model
    def _cron_run_due(self):
        today = fields.Date.context_today(self)
        schedules = self.sudo().search([('state', '=', 'active'), ('next_run_on', '<=', today)])
        for schedule in schedules:
            # One failing customer/project scope cannot abort all other runs.
            with self.env.cr.savepoint():
                schedule._run_once(scheduled=True, run_on=schedule.next_run_on)


class ReportDelivery(models.Model):
    _inherit = 'jakh.report.delivery'

    run_key = fields.Char(readonly=True, index=True, copy=False, string='Run identity')
    run_on = fields.Date(readonly=True, string='Report date')
    attempt_count = fields.Integer(readonly=True, default=1, string='Attempts')
    last_attempt_at = fields.Datetime(readonly=True, string='Last attempt')
    error_message = fields.Text(readonly=True, string='Failure reason')
    csv_attachment_id = fields.Many2one('ir.attachment', readonly=True, copy=False, string='CSV output')
    xlsx_attachment_id = fields.Many2one('ir.attachment', readonly=True, copy=False, string='XLSX output')
    state = fields.Selection(selection_add=[('failed', 'Failed')], ondelete={'failed': 'set default'})
    _run_once_constraint = models.Constraint('UNIQUE(schedule_id, run_key)', 'A scheduled reporting date is generated once.')

    @api.model
    def _record_run(self, schedule, snapshot, date, key, existing=False, error=False):
        self.check_access('create')
        values = {
            'schedule_id': schedule.id, 'generated_at': fields.Datetime.now(), 'last_attempt_at': fields.Datetime.now(),
            'run_on': date, 'run_key': key, 'snapshot': json.dumps(snapshot, sort_keys=True),
            'state': 'failed' if error else 'generated', 'error_message': error or False,
        }
        if existing:
            existing = existing.with_env(self.env)
            values['attempt_count'] = existing.attempt_count + 1
            super(OriginalDelivery, existing).write(values)
            return existing
        return super(OriginalDelivery, self).create(values)

    def _create_exports(self, snapshot):
        self.ensure_one()
        self.check_access('write')
        if snapshot['report_type'] == 'cvr':
            columns = ['wbs'] + list(MONEY_COLUMNS) + ['cost_performance_index', 'schedule_performance_index']
            labels = self.env['jakh.project.control.report.line'].fields_get(list(MONEY_COLUMNS) + ['cost_performance_index', 'schedule_performance_index'])
            headers = [self.env._('WBS')] + [labels[key]['string'] for key in columns[1:]]
        else:
            columns = ['period', 'cash_in', 'cash_out', 'net_cash', 'closing_cash']
            labels = self.env['jakh.project.cashflow.report.line'].fields_get(columns)
            headers = [labels[key]['string'] for key in columns]
        metadata = [
            [self.env._('Project'), snapshot['project']], [self.env._('Company'), snapshot['company']],
            [self.env._('Currency'), snapshot['currency']], [self.env._('Report date'), snapshot['as_of']],
            [self.env._('Generated at'), str(self.generated_at)],
            [self.env._('Calculation basis'), snapshot['basis']],
        ]
        table = [headers] + [[row.get(key, '') if row.get(key) is not None else self.env._('Undefined') for key in columns] for row in snapshot['rows']]
        if snapshot['report_type'] == 'cvr':
            table.append([self.env._('Project total')] + [snapshot['totals'].get(key) if snapshot['totals'].get(key) is not None else self.env._('Undefined') for key in columns[1:]])
        else:
            table.append([self.env._('Period total'), snapshot['cash_in_forecast'], snapshot['cash_out_forecast'],
                          snapshot['cash_in_forecast'] - snapshot['cash_out_forecast'], snapshot['closing_cash']])
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        for row in metadata + [[]] + table:
            writer.writerow([("'" + value) if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
        workbook_buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(workbook_buffer, {'in_memory': True})
        sheet = workbook.add_worksheet(self.env._('Report')[:31])
        if (self.env.context.get('lang') or '').startswith('ar'):
            sheet.right_to_left()
        header_format = workbook.add_format({'bold': True, 'bg_color': '#dcebe8', 'border': 1})
        number_format = workbook.add_format({'num_format': '#,##0.00'})
        index_format = workbook.add_format({'num_format': '0.0000'})
        for index, row in enumerate(metadata + [[]] + table):
            for column, value in enumerate(row):
                format_ = header_format if index == len(metadata) + 1 else None
                if isinstance(value, (int, float)):
                    measure_format = index_format if column < len(columns) and columns[column] in ('cost_performance_index', 'schedule_performance_index') else number_format
                    sheet.write_number(index, column, value, format_ or measure_format)
                else:
                    sheet.write_string(index, column, str(value), format_)
        sheet.freeze_panes(len(metadata) + 2, 1)
        sheet.set_column(0, 0, 30)
        sheet.set_column(1, len(columns) - 1, 22)
        sheet.autofilter(len(metadata) + 1, 0, len(metadata) + len(table), len(columns) - 1)
        workbook.close()
        prefix = 'JAKH-%s-%s-%s' % (snapshot['report_type'], self.schedule_id.id, self.run_on)
        attachments = self.env['ir.attachment'].create([
            {'name': prefix + '.csv', 'raw': b'\xef\xbb\xbf' + csv_buffer.getvalue().encode('utf-8'), 'mimetype': 'text/csv', 'res_model': self._name, 'res_id': self.id},
            {'name': prefix + '.xlsx', 'raw': workbook_buffer.getvalue(), 'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'res_model': self._name, 'res_id': self.id},
        ])
        super(OriginalDelivery, self).write({'csv_attachment_id': attachments[0].id, 'xlsx_attachment_id': attachments[1].id})

    def action_retry(self):
        require_role(self.env, REPORT_ROLES)
        self.ensure_one()
        self.check_access('read')
        if self.state != 'failed' or not self.run_key:
            raise UserError(_('Retry a failed report run.'))
        self.schedule_id._run_once(scheduled=self.run_key.startswith('due:'), run_on=self.run_on, run_key=self.run_key)
        return form_action(self)

    def action_open_report(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'generated':
            raise UserError(_('Generate the report successfully before opening its detail.'))
        snapshot = json.loads(self.snapshot)
        report_date = self.run_on or snapshot.get('as_of') or fields.Date.to_date(self.generated_at)
        if self.report_type == 'cvr':
            report = self.env['jakh.project.control.report'].create({'project_id': self.project_id.id, 'as_of_date': report_date})
        else:
            report = self.env['jakh.project.cashflow.report'].create({
                'project_id': self.project_id.id, 'date_from': snapshot.get('date_from', report_date),
                'date_to': snapshot.get('date_to', fields.Date.to_date(report_date) + timedelta(days=self.schedule_id.cash_horizon_days)),
                'opening_cash': snapshot.get('opening_cash', self.schedule_id.opening_cash),
            })
        return report.action_generate()

    def _download(self, field):
        self.ensure_one()
        self.check_access('read')
        attachment = self[field]
        if self.state != 'generated' or not attachment:
            raise UserError(_('Generate the report successfully before downloading its output.'))
        attachment.check_access('read')
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}

    def action_download_csv(self):
        return self._download('csv_attachment_id')

    def action_download_xlsx(self):
        return self._download('xlsx_attachment_id')


class CashflowPolicy(models.Model):
    _name = 'jakh.cashflow.policy'
    _description = 'Approved company cash-flow classification policy'
    _check_company_auto = True
    _order = 'effective_from desc, id desc'

    name = fields.Char(required=True, string='Policy')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, string='Company')
    effective_from = fields.Date(required=True, default=fields.Date.context_today, string='Effective from')
    effective_to = fields.Date(string='Effective to')
    cash_account_ids = fields.Many2many('account.account', 'jakh_cashflow_policy_cash_account_rel', 'policy_id', 'account_id', string='Cash and cash-equivalent accounts', required=True)
    line_ids = fields.One2many('jakh.cashflow.policy.line', 'policy_id', string='Counterpart account classification')
    basis = fields.Text(required=True, string='Accounting policy and evidence reference')
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('retired', 'Retired')], default='draft', readonly=True, required=True)
    approved_by = fields.Many2one('res.users', readonly=True, string='Approved by')
    approved_at = fields.Datetime(readonly=True, string='Approved at')

    @api.model_create_multi
    def create(self, values_list):
        require_role(self.env, ['group_manager', 'group_finance'])
        if any(set(values) & {'approved_by', 'approved_at'} or values.get('state', 'draft') != 'draft' for values in values_list):
            raise AccessError(_('Cash-flow policy approval is assigned by server actions.'))
        return super().create(values_list)

    def write(self, values):
        require_role(self.env, ['group_manager', 'group_finance'])
        if set(values) & {'state', 'approved_by', 'approved_at'}:
            raise AccessError(_('Use the cash-flow policy approval actions.'))
        lock_editable(self, ['draft'], _('Approved cash-flow policy is immutable. Create a new policy version.'))
        return super().write(values)

    def unlink(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        lock_editable(self, ['draft'], _('Only draft cash-flow policies can be deleted.'))
        return super().unlink()

    @api.constrains('company_id', 'cash_account_ids', 'effective_from', 'effective_to')
    def _check_policy(self):
        for policy in self:
            if policy.effective_to and policy.effective_to < policy.effective_from:
                raise ValidationError(_('The policy end date cannot precede its start date.'))
            if any(policy.company_id not in account.company_ids for account in policy.cash_account_ids):
                raise ValidationError(_('Cash-flow policy accounts must belong to the selected legal entity.'))
            if any(account.account_type not in ('asset_cash', 'asset_current') for account in policy.cash_account_ids):
                raise ValidationError(_('Select cash or current-asset clearing accounts supported by the approved cash-equivalent policy.'))

    def action_approve(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for policy in self:
            lock_records(policy, touch=True)
            if policy.state == 'approved':
                continue
            if policy.company_id not in self.env.companies:
                raise AccessError(_('Select this report company in the company switcher.'))
            if policy.state != 'draft' or not policy.cash_account_ids or not policy.line_ids or not (policy.basis or '').strip():
                raise UserError(_('Approve a draft policy with cash accounts, counterpart mappings and a documented accounting basis.'))
            policy.line_ids._check_account_scope()
            super(CashflowPolicy, policy).write({'state': 'approved', 'approved_by': self.env.uid, 'approved_at': fields.Datetime.now()})
            self.env['jakh.audit.event']._append(policy.company_id, 'cashflow.policy_approved', policy, {'mapped_accounts': len(policy.line_ids)})
        return True

    def action_retire(self):
        require_role(self.env, ['group_manager', 'group_finance'])
        for policy in self:
            lock_records(policy)
            if policy.state != 'approved':
                raise UserError(_('Retire an approved cash-flow policy.'))
            super(CashflowPolicy, policy).write({'state': 'retired'})
            self.env['jakh.audit.event']._append(policy.company_id, 'cashflow.policy_retired', policy)
        return True


class CashflowPolicyLine(models.Model):
    _name = 'jakh.cashflow.policy.line'
    _description = 'Company cash-flow counterpart classification'
    _check_company_auto = True

    policy_id = fields.Many2one('jakh.cashflow.policy', required=True, ondelete='cascade', string='Policy')
    company_id = fields.Many2one(related='policy_id.company_id', store=True, string='Company')
    account_id = fields.Many2one('account.account', required=True, string='Counterpart account')
    category = fields.Selection([('operating', 'Operating'), ('investing', 'Investing'), ('financing', 'Financing'), ('noncash', 'Noncash adjustment')], required=True, string='Cash-flow category')
    rationale = fields.Char(required=True, string='Classification basis')
    _account_once = models.Constraint('UNIQUE(policy_id, account_id)', 'Classify each account once per cash-flow policy.')

    @api.model_create_multi
    def create(self, values_list):
        parents = creation_parents(self, values_list, 'policy_id', 'jakh.cashflow.policy')
        lock_editable(parents, ['draft'], _('Edit counterpart mappings only in a draft cash-flow policy.'))
        return super().create(values_list)

    def write(self, values):
        lock_editable(self.policy_id, ['draft'], _('Edit counterpart mappings only in a draft cash-flow policy.'))
        if 'policy_id' in values:
            raise AccessError(_('Create a new account mapping instead of moving it between policies.'))
        return super().write(values)

    def unlink(self):
        lock_editable(self.policy_id, ['draft'], _('Edit counterpart mappings only in a draft cash-flow policy.'))
        return super().unlink()

    @api.constrains('policy_id', 'account_id', 'category', 'rationale')
    def _check_account_scope(self):
        for line in self:
            if line.company_id not in line.account_id.company_ids or line.account_id in line.policy_id.cash_account_ids:
                raise ValidationError(_('Counterpart mappings require a noncash account from the same legal entity.'))
            if not (line.rationale or '').strip():
                raise ValidationError(_('Document the accounting basis for each cash-flow mapping.'))


class FinancialReport(models.TransientModel):
    _inherit = 'jakh.financial.report'

    report_type = fields.Selection(selection_add=[('cash_flow', 'Posted company cash movements')], ondelete={'cash_flow': 'set default'})
    cashflow_policy_id = fields.Many2one('jakh.cashflow.policy', string='Approved cash-flow policy', check_company=True)
    cash_opening_balance = fields.Monetary(readonly=True, string='Opening cash')
    cash_closing_balance = fields.Monetary(readonly=True, string='Closing cash')
    cash_operating = fields.Monetary(readonly=True, string='Operating cash movement')
    cash_investing = fields.Monetary(readonly=True, string='Investing cash movement')
    cash_financing = fields.Monetary(readonly=True, string='Financing cash movement')
    cash_unresolved = fields.Monetary(readonly=True, string='Unresolved cash movement')
    cash_reconciliation_residual = fields.Monetary(readonly=True, string='Unclassified reconciliation residual')
    cash_report_complete = fields.Boolean(readonly=True, string='Classification and cash reconciliation complete')
    generated_at = fields.Datetime(readonly=True, string='Generated at')
    _output_fields = {
        'cash_opening_balance', 'cash_closing_balance', 'cash_operating', 'cash_investing', 'cash_financing',
        'cash_unresolved', 'cash_reconciliation_residual', 'cash_report_complete', 'generated_at',
    }

    @api.model_create_multi
    def create(self, values_list):
        if any(set(values) & self._output_fields for values in values_list):
            raise AccessError(_('Financial report totals are generated from posted journal items.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & self._output_fields:
            raise AccessError(_('Financial report totals are generated from posted journal items.'))
        if set(values) & {'company_id', 'report_type', 'date_from', 'date_to', 'cashflow_policy_id'}:
            reset = {key: False for key in self._output_fields}
            super(OriginalFinancialReport, self).write(reset)
            if 'cashflow_policy_id' in values:
                self.line_ids._clear_report()
        return super().write(values)

    def _cashflow_values(self):
        self.ensure_one()
        policy = self.cashflow_policy_id
        policy.check_access('read')
        if not policy or policy.state not in ('approved', 'retired') or policy.company_id != self.company_id:
            raise UserError(_('Select an approved cash-flow classification policy for this legal entity.'))
        if not self.date_from or self.date_from < policy.effective_from or policy.effective_to and self.date_to > policy.effective_to:
            raise UserError(_('The report period must fall within the approved policy effective dates.'))
        cash_accounts = set(policy.cash_account_ids.ids)
        mappings = {line.account_id.id: line.category for line in policy.line_ids}
        posted_cash = self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id), ('parent_state', '=', 'posted'),
            ('account_id', 'in', list(cash_accounts)), ('date', '<=', self.date_to),
        ], order='date, id')
        opening = sum(Decimal(str(line.balance)) for line in posted_cash if line.date < self.date_from)
        closing = sum(Decimal(str(line.balance)) for line in posted_cash)
        period = posted_cash.filtered(lambda line: line.date >= self.date_from)
        values, classified = [], defaultdict(Decimal)
        has_exception = False
        for move in period.move_id.sorted(lambda item: (item.date, item.id)):
            cash_lines = period.filtered(lambda line: line.move_id == move)
            cash = sum(Decimal(str(line.balance)) for line in cash_lines)
            counterpart = move.line_ids.filtered(lambda line: line.account_id.id not in cash_accounts and not self.currency_id.is_zero(line.balance))
            counterpart.check_access('read')
            unknown = counterpart.filtered(lambda line: line.account_id.id not in mappings)
            categories = {mappings[line.account_id.id] for line in counterpart if line.account_id.id in mappings}
            if not counterpart and self.currency_id.is_zero(float(cash)):
                category, reason = 'transfer', _('Transfer within configured cash accounts.')
            elif unknown:
                category, reason = 'unresolved', _('Unmapped counterpart accounts: %s') % ', '.join(unknown.account_id.mapped('display_name'))
            elif 'noncash' in categories:
                category, reason = 'unresolved', _('A noncash-adjustment account is mixed with a cash journal. Review the source classification.')
            elif len(categories) != 1:
                category, reason = 'unresolved', _('Cash journal has mixed counterpart categories. Finance must resolve the source classification before accepting the report.')
            else:
                category, reason = next(iter(categories)), _('Classified from the approved counterpart account policy.')
            has_exception |= category == 'unresolved'
            classified[category] += cash
            values.append({
                'account_id': cash_lines[0].account_id.id, 'move_id': move.id, 'date': move.date,
                'label': move.name, 'debit': float(max(cash, Decimal(0))), 'credit': float(max(-cash, Decimal(0))),
                'balance': float(cash), 'cashflow_category': category, 'classification_note': reason,
            })
        residual = closing - opening - classified['operating'] - classified['investing'] - classified['financing']
        totals = {
            'cash_opening_balance': float(opening), 'cash_closing_balance': float(closing),
            'cash_operating': float(classified['operating']), 'cash_investing': float(classified['investing']),
            'cash_financing': float(classified['financing']), 'cash_unresolved': float(classified['unresolved']),
            'cash_reconciliation_residual': float(residual),
            'cash_report_complete': not has_exception and self.currency_id.is_zero(float(residual)),
        }
        return values, totals

    def action_generate(self):
        if self.report_type != 'cash_flow':
            result = super().action_generate()
            super(OriginalFinancialReport, self).write({'generated_at': fields.Datetime.now()})
            return result
        require_role(self.env, ['group_manager', 'group_finance'])
        self.ensure_one()
        self.check_access('write')
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select this report company in the company switcher.'))
        if not self.date_from or not self.date_to or self.date_from > self.date_to:
            raise UserError(_('Choose a valid report period.'))
        values, totals = self._cashflow_values()
        self.line_ids._clear_report()
        self.env['jakh.financial.report.line']._create_report([{**row, 'wizard_id': self.id} for row in values])
        note = _('Posted company-currency cash movements on a direct basis, using the explicitly approved cash-account and counterpart policy. Positive amounts increase cash. Accrual invoices, depreciation and other journals without a configured cash account are excluded. Transfers within configured cash accounts net to zero. Unmapped, noncash or mixed classifications remain unresolved and prevent a complete report. Opening and closing cash reconcile to posted GL balances. This output requires finance review and does not assert an approved statutory indirect cash-flow statement.')
        super(OriginalFinancialReport, self).write({**totals, 'generated_at': fields.Datetime.now(), 'scope_note': note})
        self.env['jakh.audit.event']._append(self.company_id, 'cashflow.report_generated', self, {
            'policy': self.cashflow_policy_id.id, 'rows': len(values), 'complete': totals['cash_report_complete'],
            'residual': totals['cash_reconciliation_residual'],
        })
        return form_action(self)

    def _export(self, extension):
        self.ensure_one()
        # Regeneration enforces current GL permissions and current scope before
        # exporting, including after permissions change on a previously open tab.
        self.action_generate()
        names = ['Account', 'Partner', 'Date', 'Due date', 'Description', 'Opening', 'Debit', 'Credit', 'Signed balance', 'Aging bucket', 'Cash-flow category', 'Journal']
        headers = [self.env._(name) for name in names]
        bucket_labels = dict(self.env['jakh.financial.report.line']._fields['bucket']._description_selection(self.env))
        cash_labels = dict(self.env['jakh.financial.report.line']._fields['cashflow_category']._description_selection(self.env))
        rows = []
        for line in self.line_ids:
            rows.append([line.account_id.display_name, line.partner_id.display_name or '', str(line.date or ''),
                         str(line.due_date or ''), line.label or '', line.opening, line.debit, line.credit,
                         line.balance, bucket_labels.get(line.bucket, ''), cash_labels.get(line.cashflow_category, ''), line.move_id.name or ''])
        metadata = [[self.env._('Report'), self.name], [self.env._('Currency'), self.currency_id.name],
                    [self.env._('From date'), str(self.date_from or '')], [self.env._('To date'), str(self.date_to)],
                    [self.env._('Calculation basis'), self.scope_note]]
        if self.report_type == 'cash_flow':
            metadata.extend([[self.env._('Opening cash'), self.cash_opening_balance], [self.env._('Closing cash'), self.cash_closing_balance],
                             [self.env._('Operating cash movement'), self.cash_operating], [self.env._('Investing cash movement'), self.cash_investing],
                             [self.env._('Financing cash movement'), self.cash_financing], [self.env._('Unresolved cash movement'), self.cash_unresolved],
                             [self.env._('Unclassified reconciliation residual'), self.cash_reconciliation_residual],
                             [self.env._('Classification complete'), self.cash_report_complete]])
        table = metadata + [[]] + [headers] + rows
        if extension == 'csv':
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            for row in table:
                writer.writerow([("'" + value) if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
            payload, mimetype = b'\xef\xbb\xbf' + buffer.getvalue().encode('utf-8'), 'text/csv'
        else:
            buffer = io.BytesIO()
            workbook = xlsxwriter.Workbook(buffer, {'in_memory': True})
            sheet = workbook.add_worksheet(self.env._('Report')[:31])
            if (self.env.context.get('lang') or '').startswith('ar'):
                sheet.right_to_left()
            for index, row in enumerate(table):
                for col, value in enumerate(row):
                    if isinstance(value, bool):
                        sheet.write_boolean(index, col, value)
                    elif isinstance(value, (float, int)):
                        sheet.write_number(index, col, value)
                    else:
                        sheet.write_string(index, col, str(value))
            sheet.freeze_panes(len(metadata) + 2, 1)
            sheet.set_column(0, len(headers) - 1, 24)
            workbook.close()
            payload, mimetype = buffer.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        attachment = self.env['ir.attachment'].create({
            'name': 'JAKH-%s-%s.%s' % (self.report_type, self.date_to, extension), 'raw': payload,
            'mimetype': mimetype, 'res_model': self._name, 'res_id': self.id,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}

    def action_export_csv(self):
        return self._export('csv')

    def action_export_xlsx(self):
        return self._export('xlsx')


class FinancialReportLine(models.TransientModel):
    _inherit = 'jakh.financial.report.line'

    cashflow_category = fields.Selection([
        ('operating', 'Operating'), ('investing', 'Investing'), ('financing', 'Financing'),
        ('transfer', 'Internal cash transfer'), ('unresolved', 'Unresolved'),
    ], string='Cash-flow category', readonly=True)
    classification_note = fields.Text(readonly=True, string='Classification detail')
