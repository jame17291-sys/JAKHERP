"""Public RPC context defaults cannot replace approval/posting actions."""
from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestConstructionContextIntegrity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='context.integrity.manager', groups='jakh_core.group_manager')
        cls.pm = new_test_user(cls.env, login='context.integrity.pm', groups='jakh_core.group_project_manager')
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Synthetic context-integrity project', 'company_id': cls.env.company.id,
            'privacy_visibility': 'employees', 'user_id': cls.pm.id})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Controlled execution', 'code': 'CTX-01', 'project_id': cls.project.id,
            'budget_amount': 1000})
        cls.wbs.action_release()
        cls.resource = cls.env['jakh.site.resource'].with_user(cls.manager).create({
            'name': 'Synthetic plant unit', 'kind': 'plant', 'source_reference': 'CTX-PLANT'})

    def _data(self, model):
        scope = {'project_id': self.project.id, 'wbs_id': self.wbs.id}
        if model == 'jakh.project.schedule.progress':
            return {**scope, 'name': 'Reviewed P6 update', 'source_system': 'p6', 'external_id': 'CTX-P6',
                'source_reference': 'Approved scheduling master data', 'as_of_date': fields.Date.today(),
                'planned_value': 100, 'earned_value': 50, 'progress_percent': 50}
        if model == 'jakh.project.actual.feed':
            return {**scope, 'name': 'Reviewed labour cost', 'source_system': 'jisr', 'actual_type': 'labour',
                'external_id': 'CTX-JISR', 'source_reference': 'Approved payroll feed',
                'resource_reference': 'Crew A', 'work_date': fields.Date.today(), 'quantity': 8, 'amount': 100}
        if model == 'jakh.integration.binding':
            return {**scope, 'name': 'Draft reviewed mapping', 'kind': 'p6', 'company_id': self.env.company.id,
                'source_project_ref': 'CTX-EXTERNAL-PROJECT', 'source_wbs_ref': 'CTX-EXTERNAL-WBS'}
        if model == 'jakh.resource.assignment':
            return {**scope, 'name': 'Draft plant allocation', 'resource_id': self.resource.id,
                'start_at': '2026-01-01 08:00:00', 'end_at': '2026-01-01 16:00:00',
                'site_reference': 'Approved site demand'}
        if model == 'jakh.project.cash.forecast':
            return {**scope, 'name': 'Draft cash plan', 'cash_direction': 'in', 'amount': 100,
                'expected_date': fields.Date.today(), 'basis': 'Reviewed certificate plan', 'source_reference': 'CTX-CASH'}
        return {}

    def _assert_context_denied(self, model, overrides, user=None):
        records = self.env[model].with_user(user or self.pm)
        count = records.search_count([])
        for name, value in overrides.items():
            with self.subTest(model=model, default=name), self.assertRaises(AccessError), self.cr.savepoint():
                records.with_context(**{'default_' + name: value}).create(self._data(model))
        self.assertEqual(records.search_count([]), count)

    def test_pm_cannot_forge_accepted_p6_or_resource_cost_states_and_pointers(self):
        self._assert_context_denied('jakh.project.schedule.progress', {
            'state': 'accepted', 'accepted_by': self.pm.id, 'accepted_at': fields.Datetime.now()})
        self._assert_context_denied('jakh.project.actual.feed', {
            'state': 'accepted', 'accepted_by': self.pm.id, 'accepted_at': fields.Datetime.now(),
            'account_move_id': 999999})
        self._assert_context_denied('jakh.project.cash.forecast', {
            'state': 'approved', 'approved_by': self.pm.id, 'approved_at': fields.Datetime.now(),
            'replaced_by_id': 999999, 'cancelled_at': fields.Datetime.now()})

    def test_mapping_and_capacity_approval_defaults_require_controlled_actions(self):
        self._assert_context_denied('jakh.integration.binding', {'state': 'approved', 'approved_by': self.pm.id})
        self._assert_context_denied('jakh.resource.assignment', {
            'state': 'approved', 'approved_by': self.pm.id, 'actual_feed_id': 999999})

    def test_source_journal_and_production_acceptance_defaults_are_not_capabilities(self):
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move'].with_user(self.manager).with_context(default_jakh_actual_feed_id=999999).create({})
        self._assert_context_denied('jakh.production.readiness', {
            'state': 'accepted', 'accepted_by': self.manager.id, 'accepted_at': fields.Datetime.now()}, user=self.manager)
        # Even a False pointer is caller input, not permission to rewrite an
        # automatically assigned identity in an inherited default dictionary.
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move'].with_user(self.manager).with_context(default_jakh_actual_feed_id=False).create({})

    def test_commercial_and_final_account_default_approvals_and_figures_are_server_owned(self):
        for model, fields_to_forge in {
            'jakh.project.contract': {'state': 'active', 'revision': 7, 'finance_approved_by': self.manager.id},
            'jakh.payment.certificate': {'state': 'approved', 'approved_by': self.manager.id, 'invoice_id': 999999,
                'period_gross': 500, 'approval_sequence': 3},
            'jakh.contract.cash.document': {'state': 'invoiced', 'approved_by': self.manager.id, 'invoice_id': 999999},
            'jakh.project.final.account': {'state': 'closed', 'closed_by': self.manager.id,
                'closed_at': fields.Datetime.now(), 'certified_gross': 1000},
            'jakh.subcontract.finance.agreement': {'state': 'active', 'approved_by': self.manager.id, 'advance_balance': 500},
            'jakh.subcontract.payment.certificate': {'state': 'approved', 'approved_by': self.manager.id,
                'invoice_id': 999999, 'gross_amount': 1000},
            'jakh.subcontract.cash.document': {'state': 'billed', 'approved_by': self.manager.id, 'invoice_id': 999999},
        }.items():
            for name, value in fields_to_forge.items():
                with self.subTest(model=model, field=name), self.assertRaises(AccessError), self.cr.savepoint():
                    self.env[model].with_user(self.manager).with_context(**{'default_' + name: value}).create({})

    def test_invoice_and_control_line_default_source_links_are_not_capabilities(self):
        for name in ('jakh_certificate_id', 'jakh_cash_document_id', 'jakh_subcontract_certificate_id', 'jakh_subcontract_cash_document_id'):
            with self.subTest(field=name), self.assertRaises(AccessError), self.cr.savepoint():
                self.env['account.move'].with_user(self.manager).with_context(**{'default_' + name: 999999}).create({})
        for name, value in (('jakh_subcontract_agreement_id', 999999), ('jakh_subcontract_control', 'retention')):
            with self.subTest(field=name), self.assertRaises(AccessError), self.cr.savepoint():
                self.env['account.move.line'].with_user(self.manager).with_context(**{'default_' + name: value}).create({})

    def test_legitimate_draft_source_data_remains_usable_and_audited_acceptance_is_not_skipped(self):
        progress = self.env['jakh.project.schedule.progress'].with_user(self.pm).create(self._data('jakh.project.schedule.progress'))
        self.assertEqual(progress.state, 'draft')
        self.assertFalse(progress.accepted_by)
        progress.action_submit(); progress.action_accept()
        self.assertEqual(progress.state, 'accepted'); self.assertEqual(progress.accepted_by, self.pm)
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('source_model', '=', progress._name), ('source_id', '=', progress.id)])
        self.assertEqual(len(facts), 2)
        feed = self.env['jakh.project.actual.feed'].with_user(self.pm).create(self._data('jakh.project.actual.feed'))
        feed.action_submit()
        self.assertEqual(feed.state, 'submitted'); self.assertFalse(feed.account_move_id)
        binding = self.env['jakh.integration.binding'].with_user(self.pm).create(self._data('jakh.integration.binding'))
        binding.action_approve()
        self.assertEqual(binding.state, 'approved'); self.assertEqual(binding.approved_by, self.pm)
        assignment = self.env['jakh.resource.assignment'].with_user(self.pm).create(self._data('jakh.resource.assignment'))
        assignment.action_approve()
        self.assertEqual(assignment.state, 'approved'); self.assertEqual(assignment.approved_by, self.pm)
