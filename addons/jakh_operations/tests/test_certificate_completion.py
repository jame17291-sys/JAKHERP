from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


class CommercialCompletionCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='completion.director', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='completion.technical', groups='jakh_core.group_technical')
        cls.pm = new_test_user(cls.env, login='completion.commercial', groups='jakh_core.group_project_manager')
        cls.finance = new_test_user(cls.env, login='completion.finance', groups='jakh_core.group_finance')
        cls.company = cls.env.company
        cls.income = cls.env['account.account'].create({'name': 'Completion certified revenue', 'code': 'JCC9001',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.journal = cls.env['account.journal'].create({'name': 'Completion sales', 'code': 'JCC', 'type': 'sale',
            'company_id': cls.company.id, 'default_account_id': cls.income.id})
        cls.customer = cls.env['res.partner'].create({'name': 'Synthetic completion customer'})
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Completion controlled project', 'company_id': cls.company.id, 'privacy_visibility': 'employees',
            'user_id': cls.pm.id})
        cls.project.message_subscribe(partner_ids=(cls.technical.partner_id | cls.pm.partner_id | cls.finance.partner_id).ids)
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Completion work', 'code': 'CC-01',
            'project_id': cls.project.id, 'budget_amount': 800})
        cls.wbs.action_release()
        cls.unit = cls.env.ref('uom.product_uom_unit')

    def _contract(self, policy='finance_only'):
        contract = self.env['jakh.project.contract'].with_user(self.manager).create({
            'name': 'Controlled completion contract', 'project_id': self.project.id, 'partner_id': self.customer.id,
            'journal_id': self.journal.id, 'income_account_id': self.income.id,
            'certificate_review_policy': policy,
            'line_ids': [Command.create({'code': 'CC-BOQ', 'name': 'Original measured scope', 'wbs_id': self.wbs.id,
                'uom_id': self.unit.id, 'quantity': 10, 'unit_price': 100, 'original_direct_unit_cost': 80,
                'original_cost_total': 800, 'original_budget_total': 800, 'source_line_uuid': 'cc-source-line-1'})],
        })
        contract.action_activate()
        return contract

    def _certificate(self, contract, quantity, user=None):
        return self.env['jakh.payment.certificate'].with_user(user or self.manager).create({
            'name': 'Completion measurement', 'contract_id': contract.id,
            'line_ids': [Command.create({'contract_line_id': contract.line_ids.id, 'cumulative_quantity': quantity})],
        })


@tagged('post_install', '-at_install', 'jakh')
class TestCertificateCompletion(CommercialCompletionCase):
    def test_full_route_return_resubmit_and_no_finance_bypass(self):
        contract = self._contract('technical_commercial_finance')
        certificate = self._certificate(contract, 5, self.technical)
        certificate.action_submit()
        self.assertEqual(certificate.review_stage, 'technical')
        self.assertEqual(certificate.review_cycle, 1)
        with self.assertRaises(AccessError), self.cr.savepoint():
            certificate.with_user(self.finance).action_approve()
        with self.assertRaises(UserError), self.cr.savepoint():
            certificate.with_user(self.manager).action_approve()
        with self.assertRaises(UserError), self.cr.savepoint():
            certificate.line_ids.write({'cumulative_quantity': 4})
        with self.assertRaises(UserError), self.cr.savepoint():
            certificate.action_return()
        certificate.write({'review_note': 'Correct the measured quantity from site evidence.'})
        certificate.action_return()
        self.assertEqual(certificate.state, 'draft')
        self.assertEqual(certificate.review_stage, 'returned')
        self.assertTrue(certificate.return_reason)
        self.assertEqual(len(certificate.review_ids), 2)
        self.assertIn('5.0', certificate.review_ids.filtered(lambda row: row.decision == 'returned').measurement_snapshot)
        with self.assertRaises(UserError), self.cr.savepoint():
            certificate.with_user(self.manager).unlink()
        certificate.line_ids.write({'cumulative_quantity': 4})
        certificate.action_submit()
        self.assertEqual(certificate.review_cycle, 2)
        certificate.action_review_approve()
        self.assertEqual(certificate.review_stage, 'commercial')
        with self.assertRaises(AccessError), self.cr.savepoint():
            certificate.action_review_approve()
        certificate.with_user(self.pm).action_review_approve()
        self.assertEqual(certificate.review_stage, 'finance')
        certificate.with_user(self.finance).action_approve()
        self.assertEqual(certificate.state, 'approved')
        self.assertEqual(certificate.review_stage, 'completed')
        self.assertTrue(certificate.approved_at)
        self.assertEqual(certificate.line_ids.current_quantity, 4)
        self.assertEqual(certificate.line_ids.remaining_quantity, 6)
        self.assertTrue(certificate.line_ids.measurement_frozen)
        self.assertEqual(len(certificate.review_ids), 6)

    def test_quantity_snapshots_include_variations_and_preserve_prior_certificate(self):
        contract = self._contract()
        first = self._certificate(contract, 4)
        first.action_submit(); first.action_approve()
        variation = self.env['jakh.project.variation'].with_user(self.manager).create({
            'name': 'Additional measured scope', 'variation_type': 'owner', 'project_id': self.project.id,
            'wbs_id': self.wbs.id, 'contract_id': contract.id, 'quantity': 2, 'unit_price': 100,
            'uom_id': self.unit.id, 'description': 'Approved extension.'})
        variation.action_submit(); variation.action_price(); variation.action_approve()
        second = self.env['jakh.payment.certificate'].with_user(self.manager).create({'name': 'Second measurement', 'contract_id': contract.id})
        second.action_prepare_lines()
        second.line_ids.write({'cumulative_quantity': 7})
        second.variation_line_ids.write({'cumulative_quantity': 2})
        second.action_submit(); second.action_approve()
        self.assertEqual(second.line_ids.previous_quantity, 4)
        self.assertEqual(second.line_ids.current_quantity, 3)
        self.assertEqual(second.line_ids.remaining_quantity, 3)
        self.assertEqual(second.variation_line_ids.previous_quantity, 0)
        self.assertEqual(second.variation_line_ids.current_quantity, 2)
        self.assertEqual(second.variation_line_ids.remaining_quantity, 0)
        self.assertEqual(first.line_ids.remaining_quantity, 6)
        with self.assertRaises(AccessError), self.cr.savepoint():
            second.line_ids.write({'remaining_quantity': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            second.variation_line_ids.write({'previous_quantity': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            second.review_ids.write({'note': 'Rewrite accepted evidence'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            second.review_ids.unlink()
        with self.assertRaises(AccessError), self.cr.savepoint():
            second.with_user(self.technical).read(['period_gross'])

    def test_review_route_and_snapshot_fields_cannot_be_forged(self):
        contract = self._contract('technical_finance')
        certificate = self._certificate(contract, 1)
        with self.assertRaises(AccessError), self.cr.savepoint():
            certificate.write({'review_stage': 'finance'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.payment.certificate'].with_user(self.manager).create({
                'name': 'Forged approval time', 'contract_id': contract.id, 'approved_at': fields.Datetime.now()})
        with self.assertRaises(AccessError), self.cr.savepoint():
            certificate.line_ids.write({'measurement_frozen': True})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.certificate.review'].with_user(self.manager).create({'certificate_id': certificate.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            contract.write({'certificate_review_policy': 'finance_only'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            contract.line_ids.with_user(self.technical).read(['original_direct_unit_cost', 'original_cost_total', 'original_budget_total'])
