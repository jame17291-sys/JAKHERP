import io
import json
from datetime import timedelta
from zipfile import ZipFile
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models import construction_reporting


@tagged('post_install', '-at_install', 'jakh')
class TestConstructionReporting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('ar_001')
        cls.manager = new_test_user(cls.env, login='construction.reporting.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='construction.reporting.technical', groups='jakh_core.group_technical')
        cls.project_manager = new_test_user(cls.env, login='construction.reporting.pm', groups='jakh_core.group_project_manager')
        cls.company = cls.env.company
        def account(code, kind, reconcile=False):
            return cls.env['account.account'].create({'code': code, 'name': 'Construction reporting ' + kind,
                'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(cls.company.ids)]})
        cls.ar = account('JCR9001', 'asset_receivable', True)
        cls.income = account('JCR9002', 'income')
        cls.sales = cls.env['account.journal'].create({'name': 'Construction reporting sales', 'code': 'JCRS',
            'type': 'sale', 'company_id': cls.company.id, 'default_account_id': cls.income.id})
        cls.customer = cls.env['res.partner'].create({'name': 'Construction reporting customer', 'property_account_receivable_id': cls.ar.id})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'Construction reporting project',
            'company_id': cls.company.id, 'user_id': cls.manager.id, 'privacy_visibility': 'employees'})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Structural work', 'code': 'JCR-01',
            'project_id': cls.project.id, 'budget_amount': 1000})
        cls.wbs.action_release()
        Location = cls.env['jakh.project.location'].with_user(cls.manager)
        cls.building = Location.create({'name': 'Building A', 'code': 'A', 'project_id': cls.project.id, 'kind': 'building'})
        cls.floor = Location.create({'name': 'Ground floor', 'code': 'A-G', 'project_id': cls.project.id, 'kind': 'floor', 'parent_id': cls.building.id})
        cls.building2 = Location.create({'name': 'Building B', 'code': 'B', 'project_id': cls.project.id, 'kind': 'building'})
        cls.discipline = cls.env['jakh.discipline'].with_user(cls.manager).create({'name': 'Civil', 'code': 'JCR-CIV', 'company_id': cls.company.id})
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({'name': 'Construction reporting contract',
            'project_id': cls.project.id, 'partner_id': cls.customer.id, 'journal_id': cls.sales.id, 'income_account_id': cls.income.id,
            'line_ids': [Command.create({'code': 'JCR-BOQ', 'name': 'Measured structure', 'wbs_id': cls.wbs.id,
                'quantity': 10, 'unit_price': 200, 'uom_id': cls.env.ref('uom.product_uom_unit').id,
                'source_line_uuid': 'construction-report-source-01', 'original_budget_total': 1000,
                'original_cost_total': 1000, 'original_direct_unit_cost': 100,
                'location_id': cls.floor.id, 'discipline_id': cls.discipline.id})]})
        cls.contract.action_activate()
        cls.control = cls.env['jakh.construction.control'].with_user(cls.manager)

    def _fact(self, kind, amount, identity, allocated=True, location=False):
        return self.env['jakh.project.control.entry'].with_user(self.manager)._record(
            name='Synthetic reporting fixture ' + identity, company=self.company, project=self.project,
            wbs=self.wbs, entry_type=kind, amount=amount, source_model=self.wbs._name, source_id=self.wbs.id,
            source_key='construction-report:' + identity, entry_date=fields.Date.today(),
            contract=self.contract if allocated else False, contract_line=self.contract.line_ids if allocated else False,
            location=(location or self.floor) if allocated else False,
            discipline=self.discipline if allocated else False)

    def _report(self, **values):
        return self.env['jakh.construction.report'].with_user(self.manager).create({'project_id': self.project.id, **values})

    def _progress(self, wbs=False, percentage=60, planned=500, earned=600):
        status = self.env['jakh.project.schedule.progress'].with_user(self.manager).create({
            'project_id': self.project.id, 'wbs_id': (wbs or self.wbs).id, 'source_system': 'verified',
            'external_id': 'JCR-progress-%s' % (wbs or self.wbs).id, 'source_reference': 'Approved site measurement',
            'planned_value': planned, 'earned_value': earned, 'progress_percent': percentage})
        status.action_submit(); status.action_accept()
        return status

    def _certificate(self, quantity=5):
        certificate = self.env['jakh.payment.certificate'].with_user(self.manager).create({'name': 'Construction reporting certificate', 'contract_id': self.contract.id})
        certificate.action_prepare_lines(); certificate.line_ids.write({'cumulative_quantity': quantity})
        certificate.action_submit()
        while 'review_stage' in certificate._fields and certificate.review_stage in ('technical', 'commercial'):
            certificate.action_review_approve()
        certificate.action_approve()
        return certificate

    def _policy(self, **values):
        policy = self.env['jakh.project.threshold.policy'].with_user(self.manager).create({
            'name': 'Review contract commitment exposure', 'project_id': self.project.id,
            'owner_id': self.manager.id, 'metric': 'commitment_percent', 'threshold': 70, **values})
        policy.action_approve()
        return policy

    def test_dimensions_reconcile_once_and_do_not_fabricate_historical_allocations(self):
        self._fact('commitment', 500, 'building-a-commitment')
        self._fact('actual_cost', 200, 'building-a-actual')
        self._fact('actual_cost', 75, 'building-b-actual', location=self.building2)
        self._fact('actual_cost', 25, 'historical-unallocated', allocated=False)
        rows = self.control._cost_rows(self.project, fields.Date.today())
        self.assertEqual(sum(row['original_budget'] for row in rows), 1000)
        self.assertEqual(sum(row['commitments'] for row in rows), 500)
        self.assertEqual(sum(row['actual_cost'] for row in rows), 300)
        self.assertEqual(sum(row['actual_cost'] for row in rows if row['building_id'] == self.building.id), 200)
        self.assertEqual(sum(row['actual_cost'] for row in rows if row['building_id'] == self.building2.id), 75)
        unknown = [row for row in rows if not row['location_id']]
        self.assertEqual(sum(row['actual_cost'] for row in unknown), 25)
        self.assertFalse(all(row['dimension_complete'] for row in rows))
        report = self._report(location_id=self.building.id)
        payload = report._payload()
        self.assertEqual(payload['totals']['actual_cost'], 200)
        self.assertEqual(payload['summary']['actual_cost'], 300)
        report.action_generate()
        self.assertEqual(sum(report.cost_line_ids.mapped('actual_cost')), 200)
        self.assertEqual(report.actual_cost, 300)

    def test_unsigned_wbs_budget_stays_unallocated_even_if_building_cost_exists(self):
        other = self.env['jakh.wbs'].with_user(self.manager).create({'name': 'Historical package', 'code': 'JCR-HIST',
            'project_id': self.project.id, 'budget_amount': 500})
        other.action_release()
        rows = self.control._cost_rows(self.project, fields.Date.today())
        original = [row for row in rows if row['wbs_id'] == other.id]
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0]['original_budget'], 500)
        self.assertFalse(original[0]['contract_line_id'])
        self.assertFalse(original[0]['building_id'])

    def test_progress_percentages_are_distinct_and_missing_physical_status_is_undefined(self):
        self._progress(percentage=60)
        self._certificate(quantity=5)
        summary = self.control._summary(self.project, fields.Date.today())
        self.assertEqual(summary['physical_percent'], 60)
        self.assertEqual(summary['financial_percent'], 50)
        self.assertEqual(summary['billing_percent'], 0)
        extra = self.env['jakh.wbs'].with_user(self.manager).create({'name': 'No approved status', 'code': 'JCR-MISSING',
            'project_id': self.project.id, 'budget_amount': 500})
        extra.action_release()
        summary = self.control._summary(self.project, fields.Date.today())
        self.assertIsNone(summary['physical_percent'])
        self.assertFalse(summary['physical_complete'])
        self.assertIn(extra.id, summary['missing_progress_wbs'])
        self.assertEqual(summary['financial_percent'], 50)

    def test_unbilled_distinguishes_approval_draft_posting_and_unacknowledged_credit(self):
        certificate = self._certificate()
        rows = self.control._unbilled_rows(self.project, fields.Date.today())
        self.assertEqual(rows[0]['unbilled_gross'], 1000)
        self.assertEqual(rows[0]['invoice_status'], 'not_created')
        certificate.action_create_invoice()
        rows = self.control._unbilled_rows(self.project, fields.Date.today())
        self.assertEqual(rows[0]['unbilled_gross'], 1000)
        self.assertEqual(rows[0]['invoice_status'], 'draft')
        certificate.invoice_id.action_post()
        self.assertFalse(self.control._unbilled_rows(self.project, fields.Date.today()))
        credit = certificate.invoice_id._reverse_moves([{'date': fields.Date.today()}], cancel=False)
        credit.action_post()
        rows = self.control._unbilled_rows(self.project, fields.Date.today())
        self.assertEqual(rows[0]['unbilled_gross'], 1000)
        self.assertEqual(rows[0]['posted_gross'], 0)
        self.assertEqual(rows[0]['invoice_status'], 'credited')
        receivables = (certificate.invoice_id | credit).line_ids.filtered(lambda row: row.account_id == self.ar and not row.reconciled)
        if receivables:
            receivables.reconcile()
        certificate.action_confirm_reversal()
        self.assertFalse(self.control._unbilled_rows(self.project, fields.Date.today()))

    def test_reports_outputs_and_scope_cannot_be_forged_or_read_by_technical_staff(self):
        report = self._report(); report.action_generate()
        with self.assertRaises(AccessError):
            report.write({'actual_cost': 9000})
        with self.assertRaises(AccessError):
            self.env['jakh.construction.report'].with_user(self.manager).with_context(default_actual_cost=9000).create({'project_id': self.project.id})
        with self.assertRaises(AccessError):
            self.env['jakh.construction.report'].with_user(self.manager).create({'project_id': self.project.id, 'company_id': self.company.id})
        with self.assertRaises(AccessError):
            report.cost_line_ids.write({'actual_cost': 9000})
        with self.assertRaises(AccessError):
            self.env['jakh.construction.report'].with_user(self.technical).create({'project_id': self.project.id})
        with self.assertRaises(AccessError):
            report.with_user(self.technical)._payload()
        other = self.env['project.project'].with_user(self.manager).create({'name': 'Other report scope', 'company_id': self.company.id})
        foreign_location = self.env['jakh.project.location'].with_user(self.manager).create({'name': 'Other building',
            'code': 'OTHER', 'project_id': other.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._report(location_id=foreign_location.id)

    def test_changing_report_scope_clears_stale_values_and_exported_scope_is_frozen(self):
        self._fact('actual_cost', 150, 'scope-frozen-actual')
        report = self._report(); report.action_generate()
        self.assertTrue(report.cost_line_ids)
        report.write({'location_id': self.building.id})
        self.assertFalse(report.cost_line_ids)
        self.assertFalse(report.generated_at)
        report.action_export_xlsx()
        with self.assertRaises(UserError):
            report.write({'location_id': self.building2.id})

    def test_pdf_and_xlsx_recalculate_same_values_under_current_access(self):
        self._fact('actual_cost', 275, 'exports-actual')
        report = self._report()
        report.action_export_xlsx()
        self.assertFalse(report.xlsx_attachment_id.public)
        self.assertEqual(report.xlsx_attachment_id.res_model, report._name)
        with ZipFile(io.BytesIO(report.xlsx_attachment_id.raw)) as archive:
            xml = archive.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('<v>275</v>', xml)
            self.assertTrue(any(name.startswith('xl/media/image') for name in archive.namelist()))
        values = self.env['report.jakh_operations.construction_control_document'].with_user(self.manager)._get_report_values(report.ids)
        self.assertEqual(values['payloads'][report.id]['totals']['actual_cost'], 275)
        html, _format = self.env['ir.actions.report'].with_user(self.manager)._render_qweb_html(
            'jakh_operations.report_construction_control', report.ids)
        self.assertIn(b'JAKH Construction Control', html)
        self.assertIn(b'275', html)
        with self.assertRaises(AccessError):
            self.env['report.jakh_operations.construction_control_document'].with_user(self.technical)._get_report_values(report.ids)

    def test_scope_generation_and_export_share_real_report_row_version_lock(self):
        self._fact('actual_cost', 275, 'serialized-report-actual')
        report = self._report()
        def row_version():
            report.flush_recordset()
            self.cr.execute('SELECT ctid::text FROM jakh_construction_report WHERE id=%s', [report.id])
            return self.cr.fetchone()[0]
        previous = row_version()
        with patch.object(construction_reporting, 'lock_records', wraps=construction_reporting.lock_records) as lock:
            report.action_generate()
            generated = row_version()
            self.assertNotEqual(previous, generated)
            report.write({'location_id': self.building.id})
            filtered = row_version()
            self.assertNotEqual(generated, filtered)
            report.action_export_xlsx()
            self.assertNotEqual(filtered, row_version())
            self.assertEqual(lock.call_count, 3)
            for invocation in lock.call_args_list:
                self.assertEqual(invocation.args[0], report)
                self.assertIs(invocation.kwargs['touch'], True)
        self.assertEqual(report._payload()['totals']['actual_cost'], 275)
        with self.assertRaises(UserError):
            report.write({'project_id': self.project.id})

    def test_arabic_exports_use_native_rtl_and_html_direction(self):
        report = self._report().with_context(lang='ar_001')
        report.action_export_xlsx()
        with ZipFile(io.BytesIO(report.xlsx_attachment_id.raw)) as archive:
            xml = archive.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('rightToLeft="1"', xml)
        html, _format = self.env['ir.actions.report'].with_user(self.manager).with_context(lang='ar_001')._render_qweb_html(
            'jakh_operations.report_construction_control', report.ids)
        self.assertIn(b'dir="rtl"', html)

    def test_cash_forecast_pdf_xlsx_and_financial_pdf_reuse_native_calculations(self):
        forecast = self.env['jakh.project.cash.forecast'].with_user(self.manager).create({
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'cash_direction': 'in', 'amount': 700,
            'source_reference': 'Construction reporting approved collection forecast', 'basis': 'Approved owner payment plan',
            'expected_date': fields.Date.today()})
        forecast.action_submit(); forecast.action_approve()
        report = self.env['jakh.project.cashflow.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'date_from': fields.Date.today(), 'date_to': fields.Date.today(), 'opening_cash': 100})
        report.action_generate()
        payload = report._presentation_payload()
        self.assertEqual(payload['closing_cash'], 800)
        self.assertEqual(payload['rows'][0]['cash_in'], report.line_ids.cash_in)
        action = report.action_export_xlsx()
        attachment_id = int(action['url'].split('/')[3].split('?')[0])
        attachment = self.env['ir.attachment'].with_user(self.manager).browse(attachment_id)
        with ZipFile(io.BytesIO(attachment.raw)) as archive:
            self.assertIn('<v>800</v>', archive.read('xl/worksheets/sheet1.xml').decode())
            self.assertTrue(any(name.startswith('xl/media/image') for name in archive.namelist()))
        html, _format = self.env['ir.actions.report'].with_user(self.manager)._render_qweb_html(
            'jakh_operations.report_project_cash_forecast', report.ids)
        self.assertIn(b'JAKH Project Cash Forecast', html)
        self.assertIn(b'800', html)
        financial = self.env['jakh.financial.report'].with_user(self.manager).create({'company_id': self.company.id})
        html, _format = self.env['ir.actions.report'].with_user(self.manager)._render_qweb_html(
            'jakh_operations.report_financial_control', financial.ids)
        self.assertIn(b'JAKH Posted Financial Report', html)
        self.assertEqual(sum(financial.line_ids.mapped('balance')), 0)
        with self.assertRaises(AccessError):
            self.env['report.jakh_operations.financial_control_document'].with_user(self.technical)._get_report_values(financial.ids)

    def test_cost_progress_available_to_project_manager_without_exposing_accounting_documents(self):
        control = self.env['jakh.construction.control'].with_user(self.project_manager)
        summary = control._summary(self.project, fields.Date.today())
        self.assertEqual(summary['revised_budget'], 1000)
        self.assertIsNone(summary['matched_receipts'])
        self.assertIsNone(summary['unbilled_amount'])
        report = self.env['jakh.construction.report'].with_user(self.project_manager).create({'project_id': self.project.id})
        report.action_generate()
        self.assertFalse(report.receipts_available)
        report.action_export_xlsx()
        with self.assertRaises(AccessError):
            control._unbilled_rows(self.project, fields.Date.today())

    def test_matched_receipts_use_native_posted_payment_allocation_not_credit_settlement(self):
        certificate = self._certificate(); certificate.action_create_invoice(); certificate.invoice_id.action_post()
        bank_account = self.env['account.account'].create({'code': 'JCR9003', 'name': 'Construction report cash',
            'account_type': 'asset_cash', 'company_ids': [Command.set(self.company.ids)]})
        bank = self.env['account.journal'].create({'name': 'Construction report bank', 'code': 'JCRB', 'type': 'bank',
            'company_id': self.company.id, 'default_account_id': bank_account.id})
        method = bank.inbound_payment_method_line_ids.filtered(lambda row: row.code == 'manual')[:1]
        method.payment_account_id = bank_account
        self.env['account.payment.register'].with_user(self.manager).with_context(
            active_model='account.move', active_ids=certificate.invoice_id.ids).create({
                'journal_id': bank.id, 'payment_method_line_id': method.id, 'payment_date': fields.Date.today(),
            })._create_payments()
        receipts = self.control._collections(self.project, fields.Date.today())
        self.assertEqual(receipts['amount'], 1000)
        self.assertTrue(receipts['payment_move_ids'])
        self.assertEqual(self.control._collections(self.project, fields.Date.today() - timedelta(days=1))['amount'], 0)

    def test_schedules_share_detail_summary_exports_and_date_idempotence(self):
        self._fact('actual_cost', 325, 'scheduled-actual')
        self._progress(percentage=65)
        self._certificate()
        for kind in ('construction_cost', 'unbilled'):
            schedule = self.env['jakh.report.schedule'].with_user(self.manager).create({
                'name': 'Governed construction snapshot', 'project_id': self.project.id, 'owner_id': self.manager.id,
                'report_type': kind, 'next_run_on': fields.Date.today()})
            snapshot = schedule._snapshot(as_of=fields.Date.today())
            payload = self._report(report_type='cost' if kind == 'construction_cost' else 'unbilled')._payload()
            self.assertEqual(snapshot['totals'], payload['totals'])
            self.assertEqual(snapshot['summary'], payload['summary'])
            self.assertEqual(snapshot['unbilled'], payload['unbilled'])
            delivery = schedule._run_once(scheduled=True, run_on=fields.Date.today())
            self.assertEqual(delivery.state, 'generated', delivery.error_message)
            self.assertEqual(schedule._run_once(scheduled=True, run_on=fields.Date.today()), delivery)
            self.assertEqual(delivery.attempt_count, 1)
            self.assertEqual(delivery.xlsx_attachment_id.res_model, 'jakh.report.delivery')
            self.assertFalse(delivery.xlsx_attachment_id.public)
            self.assertEqual(json.loads(delivery.snapshot)['summary']['actual_cost'], 325)
            self.assertTrue(bytes(delivery.csv_attachment_id.raw).startswith(b'\xef\xbb\xbf'))
            self.assertTrue(bytes(delivery.xlsx_attachment_id.raw).startswith(b'PK'))

    def test_threshold_deduplicates_escalates_resolves_and_reopens_with_history(self):
        Policy = self.env['jakh.project.threshold.policy'].with_user(self.project_manager)
        values = {'name': 'No approval bypass', 'project_id': self.project.id,
                  'owner_id': self.manager.id, 'metric': 'commitment_percent', 'threshold': 70}
        with self.assertRaises(AccessError):
            Policy.with_context(default_state='active').create(values)
        with self.assertRaises(AccessError):
            Policy.with_context(default_approved_by=self.manager.id).create(values)
        with self.assertRaises(AccessError):
            Policy.create({**values, 'company_id': self.company.id})
        self._fact('commitment', 800, 'threshold-commitment')
        policy = self._policy()
        policy.action_evaluate(); policy.action_evaluate()
        self.assertEqual(len(policy.alert_ids), 1)
        alert = policy.alert_ids
        self.assertEqual(alert.observed_value, 80)
        self.assertEqual(alert.state, 'open')
        alert._transition(opened_at=fields.Datetime.now() - timedelta(days=4))
        policy.action_evaluate()
        self.assertEqual(alert.state, 'escalated')
        self._fact('commitment', -200, 'threshold-reversal')
        alert.action_recheck()
        self.assertEqual(alert.state, 'resolved')
        self.assertTrue(alert.resolution_note)
        self._fact('commitment', 300, 'threshold-new-exposure')
        policy.action_evaluate()
        self.assertEqual(len(policy.alert_ids), 1)
        self.assertEqual(alert.state, 'open')
        self.assertEqual(alert.occurrence_count, 2)
        self.assertTrue(alert.activity_ids)
        with self.assertRaises(AccessError):
            policy.write({'threshold': 1000})
        with self.assertRaises(AccessError):
            alert.write({'state': 'resolved'})

    def test_threshold_owner_access_revocation_and_undefined_indices_fail_closed(self):
        policy = self._policy(metric='cpi', comparison='lt', threshold=1)
        self._fact('actual_cost', 1000, 'cpi-actual')
        self._progress(earned=500)
        policy.action_evaluate()
        self.assertEqual(policy.alert_ids.state, 'open')
        self._fact('actual_cost', -1000, 'cpi-zero-denominator')
        policy.action_evaluate()
        self.assertEqual(policy.alert_ids.state, 'open')
        self.assertIn('Undefined', policy.evaluation_note)
        self.manager.write({'group_ids': [Command.set(self.technical.group_ids.ids)]})
        with self.assertRaises(AccessError):
            policy.action_evaluate()

    def test_retirement_requires_reason_and_preserves_resolved_exception_history(self):
        self._fact('commitment', 800, 'retirement-commitment')
        policy = self._policy(); policy.action_evaluate()
        with self.assertRaises(UserError):
            policy.action_retire()
        policy.write({'retirement_reason': 'The approved replacement policy supersedes this rule.'})
        policy.action_retire()
        self.assertEqual(policy.state, 'retired')
        self.assertEqual(policy.alert_ids.state, 'resolved')
        self.assertEqual(policy.alert_ids.resolution_note, policy.retirement_reason)
        self.assertEqual(len(policy.alert_ids), 1)

    def test_dashboard_keeps_each_company_currency_and_drilldown_totals_separate(self):
        self._fact('actual_cost', 250, 'dashboard-actual')
        sections = self.control.get_portfolio()
        section = next(row for row in sections if row['company_id'] == self.company.id)
        self.assertEqual(section['currency'], self.company.currency_id.name)
        card = next(row for row in section['cards'] if row['label'] == self.manager.env._('Posted project costs'))
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search(card['domain'])
        self.assertEqual(card['amount'], sum(facts.mapped('amount')))
        self.assertIn(('company_id', '=', self.company.id), card['domain'])
        technical_data = self.env['jakh.dashboard'].with_user(self.technical).get_data()
        self.assertFalse(technical_data['portfolio'])
