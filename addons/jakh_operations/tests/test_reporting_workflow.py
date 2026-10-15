import json
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestReportingWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='workflow.report.manager', groups='jakh_core.group_manager')
        company = cls.env.company

        def account(code, name, kind, reconcile=False):
            return cls.env['account.account'].create({
                'code': code, 'name': name, 'account_type': kind, 'reconcile': reconcile,
                'company_ids': [Command.set(company.ids)],
            })

        cls.expense = account('JRPT9001', 'Reporting labour cost', 'expense_direct_cost')
        cls.accrual = account('JRPT9002', 'Reporting labour accrual', 'liability_current')
        cls.general_journal = cls.env['account.journal'].create({
            'name': 'Reporting actual journal', 'code': 'JRPTG', 'type': 'general', 'company_id': company.id,
        })
        cls.bank = account('JRPT9003', 'Reporting cash bank', 'asset_cash')
        cls.receivable = account('JRPT9004', 'Reporting receivables', 'asset_receivable', True)
        cls.income = account('JRPT9005', 'Reporting revenue', 'income')
        cls.asset = account('JRPT9006', 'Reporting plant asset', 'asset_fixed')
        cls.loan = account('JRPT9007', 'Reporting loan', 'liability_non_current')
        cls.equity = account('JRPT9008', 'Reporting opening equity', 'equity')
        cls.depreciation = account('JRPT9009', 'Reporting depreciation', 'expense_depreciation')
        cls.accumulated_depreciation = account('JRPT9010', 'Reporting accumulated depreciation', 'asset_fixed')
        cls.cash_second = account('JRPT9011', 'Reporting petty cash', 'asset_cash')
        cls.unmapped = account('JRPT9012', 'Reporting unmapped expense', 'expense')
        cls.sales_journal = cls.env['account.journal'].create({
            'name': 'Reporting sales', 'code': 'JRPTS', 'type': 'sale', 'company_id': company.id,
            'default_account_id': cls.income.id,
        })
        cls.bank_journal = cls.env['account.journal'].create({
            'name': 'Reporting bank', 'code': 'JRPTB', 'type': 'bank', 'company_id': company.id,
            'default_account_id': cls.bank.id,
        })
        cls.payment_method = cls.bank_journal.inbound_payment_method_line_ids.filtered(lambda line: line.code == 'manual')[:1]
        cls.payment_method.payment_account_id = cls.bank
        cls.customer = cls.env['res.partner'].create({
            'name': 'Reporting cashflow customer', 'property_account_receivable_id': cls.receivable.id,
        })
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Reporting workflow project', 'company_id': cls.env.company.id,
            'user_id': cls.manager.id, 'privacy_visibility': 'employees',
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Reporting package', 'code': 'RPT-01', 'project_id': cls.project.id, 'budget_amount': 1000,
        })
        cls.wbs.action_release()

    def _actual(self, wbs, amount, date, reference):
        feed = self.env['jakh.project.actual.feed'].with_user(self.manager).create({
            'source_system': 'jisr', 'actual_type': 'labour', 'external_id': reference,
            'source_reference': reference, 'project_id': self.project.id, 'wbs_id': wbs.id,
            'resource_reference': 'Approved crew cost', 'quantity': 8, 'unit_label': 'hours',
            'amount': amount, 'work_date': date,
            'journal_id': self.general_journal.id, 'expense_account_id': self.expense.id, 'offset_account_id': self.accrual.id,
        })
        feed.action_submit()
        feed.action_accept()
        return feed

    def _progress(self, wbs, date, planned, earned, reference):
        progress = self.env['jakh.project.schedule.progress'].with_user(self.manager).create({
            'source_system': 'p6', 'external_id': reference, 'source_reference': reference,
            'project_id': self.project.id, 'wbs_id': wbs.id, 'as_of_date': date,
            'planned_value': planned, 'earned_value': earned, 'progress_percent': 50,
        })
        progress.action_submit()
        progress.action_accept()
        return progress

    def _forecast(self, wbs, date, amount):
        forecast = self.env['jakh.project.forecast'].with_user(self.manager).create({
            'wbs_id': wbs.id, 'as_of_date': date, 'forecast_final_cost': amount,
            'basis': 'Reviewed commitments, productivity and remaining work.',
        })
        forecast.action_submit()
        forecast.action_approve()
        return forecast

    def _cash(self, reference, date, amount, replaces=False, direction='out'):
        forecast = self.env['jakh.project.cash.forecast'].with_user(self.manager).create({
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'source_reference': reference,
            'cash_direction': direction, 'expected_date': date, 'amount': amount,
            'basis': 'Approved payment plan', 'replaces_id': replaces.id if replaces else False,
        })
        forecast.action_submit()
        forecast.action_approve()
        return forecast

    def _schedule(self, owner=False, **values):
        return self.env['jakh.report.schedule'].with_user(self.manager).create({
            'name': 'Workflow report schedule', 'project_id': self.project.id,
            'owner_id': (owner or self.manager).id, 'report_type': 'cvr', 'frequency': 'weekly',
            **values,
        })

    def test_cumulative_progress_latest_forecast_and_future_exclusion(self):
        wbs2 = self.env['jakh.wbs'].with_user(self.manager).create({
            'name': 'Second reporting package', 'code': 'RPT-02', 'project_id': self.project.id, 'budget_amount': 4000,
        })
        wbs2.action_release()
        self._actual(self.wbs, 100, '2026-01-10', 'report-actual-1')
        self._actual(wbs2, 900, '2026-01-10', 'report-actual-2')
        self._actual(self.wbs, 300, '2026-02-10', 'report-future-actual')
        self._progress(self.wbs, '2026-01-01', 100, 50, 'report-p6-old')
        self._progress(self.wbs, '2026-01-15', 300, 250, 'report-p6-current')
        self._progress(self.wbs, '2026-02-01', 500, 400, 'report-p6-future')
        self._progress(wbs2, '2026-01-15', 1000, 100, 'report-p6-second')
        self._forecast(self.wbs, '2026-01-05', 1100)
        self._forecast(self.wbs, '2026-01-18', 1400)
        self._forecast(self.wbs, '2026-02-01', 2000)
        report = self.env['jakh.project.control.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'as_of_date': '2026-01-20',
        })
        report.action_generate()
        first = report.line_ids.filtered(lambda line: line.wbs_id == self.wbs)
        self.assertEqual(first.planned_value, 300)
        self.assertEqual(first.earned_value, 250)
        self.assertEqual(first.actual_cost, 100)
        self.assertEqual(first.forecast_final_cost, 1400)
        self.assertAlmostEqual(report.total_cost_performance_index, 350 / 1000)
        self.assertAlmostEqual(report.total_schedule_performance_index, 350 / 1300, places=4)
        schedule = self._schedule(next_run_on='2026-01-20')
        snapshot = schedule._snapshot(as_of='2026-01-20')
        self.assertEqual(snapshot['actual_cost'], 1000)
        self.assertEqual(snapshot['forecast_final_cost'], 5400)
        self.assertEqual(snapshot['planned_value'], 1300)
        self.assertEqual(snapshot['earned_value'], 350)
        self.assertAlmostEqual(snapshot['cost_performance_index'], report.total_cost_performance_index)
        self.assertAlmostEqual(snapshot['schedule_performance_index'], report.total_schedule_performance_index, places=4)
        early = schedule._snapshot(as_of='2026-01-09')
        self.assertEqual(early['planned_value'], 100)
        self.assertEqual(early['earned_value'], 50)
        self.assertEqual(early['actual_cost'], 0)
        self.assertEqual(early['forecast_final_cost'], 5100)

    def test_same_date_schedule_correction_is_latest_status_not_extra_progress(self):
        self._progress(self.wbs, '2026-01-15', 300, 250, 'report-p6-status')
        self._progress(self.wbs, '2026-01-15', 310, 245, 'report-p6-correction')
        rows = self.env['jakh.project.control.report'].with_user(self.manager)._calculation_rows(self.project, '2026-01-20')
        self.assertEqual(rows[0]['planned_value'], 310)
        self.assertEqual(rows[0]['earned_value'], 245)

    def test_zero_denominator_indices_are_undefined_in_ui_snapshot_and_exports(self):
        self._progress(self.wbs, '2026-01-15', 0, 25, 'report-p6-no-denominator')
        report = self.env['jakh.project.control.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'as_of_date': '2026-01-20',
        })
        report.action_generate()
        undefined = report.env._('Undefined')
        self.assertEqual(report.total_cpi_display, undefined)
        self.assertEqual(report.total_spi_display, undefined)
        self.assertEqual(report.line_ids.cost_performance_display, undefined)
        self.assertEqual(report.line_ids.schedule_performance_display, undefined)
        schedule = self._schedule(next_run_on='2026-01-20')
        snapshot = schedule._snapshot(as_of='2026-01-20')
        self.assertIsNone(snapshot['cost_performance_index'])
        self.assertIsNone(snapshot['schedule_performance_index'])
        delivery = schedule._run_once(scheduled=True)
        self.assertIn(undefined, bytes(delivery.csv_attachment_id.raw).decode('utf-8'))

    def test_due_run_is_idempotent_and_exports_saved_under_owner(self):
        self._actual(self.wbs, 100, '2026-01-10', 'report-idempotent-actual')
        schedule = self._schedule(next_run_on='2026-01-20')
        delivery = schedule._run_once(scheduled=True, run_on='2026-01-20')
        self.assertEqual(delivery.state, 'generated')
        self.assertEqual(schedule.next_run_on, fields.Date.to_date('2026-01-27'))
        again = schedule._run_once(scheduled=True, run_on='2026-01-20')
        self.assertEqual(again, delivery)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(len(schedule.delivery_ids), 1)
        self.assertEqual(json.loads(delivery.snapshot)['generated_by'], self.manager.id)
        self.assertTrue(bytes(delivery.csv_attachment_id.raw).startswith(b'\xef\xbb\xbf'))
        self.assertTrue(bytes(delivery.xlsx_attachment_id.raw).startswith(b'PK'))
        self.assertEqual(delivery.csv_attachment_id.res_model, 'jakh.report.delivery')
        self.assertEqual(delivery.csv_attachment_id.res_id, delivery.id)

    def test_manual_run_preserves_future_schedule_and_immutable_scope(self):
        due = fields.Date.today() + timedelta(days=15)
        schedule = self._schedule(next_run_on=due)
        schedule.action_run_now()
        self.assertEqual(schedule.next_run_on, due)
        self.assertEqual(len(schedule.delivery_ids), 1)
        with self.assertRaises(UserError):
            schedule.write({'owner_id': self.env.user.id})

    def test_revoked_project_access_records_failure_without_financial_data_and_can_retry(self):
        owner = new_test_user(self.env, login='workflow.report.finance', groups='jakh_core.group_finance,project.group_project_user')
        self.project.write({'privacy_visibility': 'invited_users', 'allowed_internal_user_ids': [Command.link(owner.id)]})
        schedule = self._schedule(owner=owner, next_run_on=fields.Date.today())
        self.project.write({'allowed_internal_user_ids': [Command.unlink(owner.id)]})
        failure = schedule.sudo()._run_once(scheduled=True)
        self.assertEqual(failure.state, 'failed')
        self.assertEqual(json.loads(failure.snapshot), {})
        self.assertFalse(failure.csv_attachment_id)
        self.assertEqual(schedule.next_run_on, fields.Date.today())
        self.assertFalse(schedule.last_run_at)
        self.project.write({'allowed_internal_user_ids': [Command.link(owner.id)]})
        failure.with_user(self.manager).action_retry()
        self.assertEqual(failure.state, 'generated')
        self.assertEqual(failure.attempt_count, 2)
        self.assertEqual(json.loads(failure.snapshot)['generated_by'], owner.id)
        self.assertEqual(len(schedule.delivery_ids), 1)
        self.project.write({'allowed_internal_user_ids': [Command.unlink(owner.id)]})
        with self.assertRaises(AccessError):
            failure.with_user(owner).sudo(False).check_access('read')
        with self.assertRaises(AccessError):
            failure.csv_attachment_id.with_user(owner).sudo(False).check_access('read')

    def test_cron_inactive_owner_failure_does_not_block_other_due_report(self):
        owner = new_test_user(self.env, login='workflow.report.inactive', groups='jakh_core.group_manager')
        failed = self._schedule(owner=owner, next_run_on=fields.Date.today())
        passed = self._schedule(next_run_on=fields.Date.today())
        owner.active = False
        self.env['jakh.report.schedule']._cron_run_due()
        self.assertEqual(failed.delivery_ids.state, 'failed')
        self.assertEqual(passed.delivery_ids.state, 'generated')
        self.assertEqual(failed.next_run_on, fields.Date.today())
        self.assertGreater(passed.next_run_on, fields.Date.today())

    def test_cash_replacement_and_cancellation_keep_audit_without_double_forecasts(self):
        original = self._cash('invoice-plan-1', '2026-01-10', 100)
        replacement = self._cash('invoice-plan-1', '2026-02-15', 140, replaces=original)
        self._cash('collection-plan-2', '2026-02-20', 200, direction='in')
        cancelled = self._cash('invoice-plan-cancel', '2026-02-21', 70)
        cancelled.write({'cancellation_reason': 'Supplier payment deferred outside the approved plan.'})
        cancelled.action_cancel()
        cancelled.action_cancel()
        self.assertEqual(original.state, 'superseded')
        self.assertEqual(original.replaced_by_id, replacement)
        self.assertEqual(cancelled.state, 'cancelled')
        cashflow = self.env['jakh.project.cashflow.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'date_from': '2026-01-01', 'date_to': '2026-02-28', 'opening_cash': 10,
        })
        cashflow.action_generate()
        january, february = cashflow.line_ids.sorted('period')
        self.assertEqual(january.cash_out, 0)
        self.assertEqual(february.cash_out, 140)
        self.assertEqual(february.cash_in, 200)
        self.assertEqual(february.closing_cash, 70)
        entries = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id), ('entry_type', '=', 'cash_out_forecast'),
        ])
        self.assertEqual(sum(entries.mapped('amount')), 140)
        duplicate = self.env['jakh.project.cash.forecast'].with_user(self.manager).create({
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'cash_direction': 'out',
            'expected_date': '2026-02-15', 'amount': 140, 'basis': 'Duplicate plan', 'source_reference': 'invoice-plan-1',
        })
        duplicate.action_submit()
        with self.assertRaises(UserError):
            duplicate.action_approve()

    def _journal(self, date, lines, posted=True):
        move = self.env['account.move'].with_user(self.manager).create({
            'company_id': self.env.company.id, 'journal_id': self.general_journal.id, 'date': date,
            'move_type': 'entry', 'line_ids': [Command.create({
                'name': 'Verified reporting transaction', 'account_id': account.id,
                'debit': max(balance, 0), 'credit': max(-balance, 0),
            }) for account, balance in lines],
        })
        if posted:
            move.action_post()
        return move

    def _cashflow_policy(self, include_unmapped=False):
        mappings = [(self.receivable, 'operating'), (self.expense, 'operating'), (self.accrual, 'operating'),
                    (self.income, 'operating'), (self.asset, 'investing'), (self.loan, 'financing'),
                    (self.equity, 'financing'), (self.depreciation, 'noncash')]
        if include_unmapped:
            mappings.append((self.unmapped, 'operating'))
        policy = self.env['jakh.cashflow.policy'].with_user(self.manager).create({
            'name': 'Approved 2026 cashflow accounting policy', 'company_id': self.env.company.id,
            'effective_from': '2026-01-01', 'effective_to': '2026-12-31',
            'cash_account_ids': [Command.set((self.bank | self.cash_second).ids)],
            'basis': 'Finance has verified cash equivalents and journal counterpart account classifications.',
            'line_ids': [Command.create({'account_id': account.id, 'category': category,
                                         'rationale': 'Finance-approved economic classification.'}) for account, category in mappings],
        })
        policy.action_approve()
        return policy

    def _company_cash_report(self, policy):
        return self.env['jakh.financial.report'].with_user(self.manager).create({
            'company_id': self.env.company.id, 'report_type': 'cash_flow',
            'date_from': '2026-01-01', 'date_to': '2026-01-31', 'cashflow_policy_id': policy.id,
        })

    def test_posted_cashflow_reconciles_payment_asset_loan_and_excludes_non_cash_journals(self):
        policy = self._cashflow_policy()
        self._journal('2025-12-31', [(self.bank, 1000), (self.equity, -1000)])
        invoice = self.env['account.move'].with_user(self.manager).create({
            'move_type': 'out_invoice', 'company_id': self.env.company.id, 'journal_id': self.sales_journal.id,
            'partner_id': self.customer.id, 'invoice_date': '2026-01-05', 'date': '2026-01-05',
            'invoice_line_ids': [Command.create({'name': 'Measured work', 'quantity': 1, 'price_unit': 300,
                                                'account_id': self.income.id, 'tax_ids': [Command.clear()]})],
        })
        invoice.action_post()
        payment = self.env['account.payment.register'].with_user(self.manager).with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'journal_id': self.bank_journal.id, 'payment_method_line_id': self.payment_method.id,
                  'payment_date': '2026-01-10'})._create_payments()
        self.assertEqual(payment.move_id.state, 'posted')
        self._journal('2026-01-11', [(self.expense, 80), (self.accrual, -80)])
        self._journal('2026-01-11', [(self.depreciation, 20), (self.accumulated_depreciation, -20)])
        self._journal('2026-01-12', [(self.asset, 200), (self.bank, -200)])
        self._journal('2026-01-13', [(self.bank, 500), (self.loan, -500)])
        self._journal('2026-01-14', [(self.expense, 100), (self.bank, -100)])
        self._journal('2026-01-15', [(self.cash_second, 50), (self.bank, -50)])
        self._journal('2026-01-16', [(self.bank, 9000), (self.loan, -9000)], posted=False)
        self._journal('2026-02-01', [(self.bank, 100), (self.loan, -100)])
        report = self._company_cash_report(policy)
        report.action_generate()
        self.assertEqual(report.cash_opening_balance, 1000)
        self.assertEqual(report.cash_closing_balance, 1500)
        self.assertEqual(report.cash_operating, 200)
        self.assertEqual(report.cash_investing, -200)
        self.assertEqual(report.cash_financing, 500)
        self.assertEqual(report.cash_reconciliation_residual, 0)
        self.assertTrue(report.cash_report_complete)
        self.assertEqual(len(report.line_ids), 5)
        self.assertEqual(report.line_ids.filtered(lambda line: line.cashflow_category == 'transfer').balance, 0)
        self.assertNotIn(invoice, report.line_ids.move_id)
        csv_action = report.action_export_csv()
        xlsx_action = report.action_export_xlsx()
        csv_id = int(csv_action['url'].split('/')[3].split('?')[0])
        xlsx_id = int(xlsx_action['url'].split('/')[3].split('?')[0])
        self.assertTrue(bytes(self.env['ir.attachment'].browse(csv_id).raw).startswith(b'\xef\xbb\xbf'))
        self.assertTrue(bytes(self.env['ir.attachment'].browse(xlsx_id).raw).startswith(b'PK'))

    def test_unmapped_mixed_and_noncash_counterparts_block_complete_cashflow(self):
        policy = self._cashflow_policy()
        self._journal('2025-12-31', [(self.bank, 1000), (self.equity, -1000)])
        self._journal('2026-01-15', [(self.unmapped, 50), (self.bank, -50)])
        self._journal('2026-01-16', [(self.asset, 100), (self.expense, 50), (self.bank, -150)])
        self._journal('2026-01-17', [(self.depreciation, 20), (self.bank, -20)])
        report = self._company_cash_report(policy)
        report.action_generate()
        self.assertEqual(report.cash_closing_balance, 780)
        self.assertEqual(report.cash_unresolved, -220)
        self.assertEqual(report.cash_reconciliation_residual, -220)
        self.assertFalse(report.cash_report_complete)
        self.assertEqual(len(report.line_ids.filtered(lambda line: line.cashflow_category == 'unresolved')), 3)
        with self.assertRaises(UserError):
            policy.line_ids[:1].write({'category': 'financing'})
        with self.assertRaises(AccessError):
            report.write({'cash_report_complete': True})

    def test_financial_exports_cover_posted_statements_and_regenerate_current_scope(self):
        self._journal('2026-01-10', [(self.expense, 100), (self.accrual, -100)])
        for kind in ('trial_balance', 'general_ledger', 'profit_loss', 'balance_sheet', 'receivable_aging', 'payable_aging'):
            report = self.env['jakh.financial.report'].with_user(self.manager).create({
                'company_id': self.env.company.id, 'report_type': kind, 'date_from': '2026-01-01', 'date_to': '2026-01-31',
            })
            action = report.action_export_csv()
            self.assertEqual(action['type'], 'ir.actions.act_url')
            self.assertTrue(report.generated_at)
            report.write({'date_to': '2026-02-28'})
            self.assertFalse(report.generated_at)
