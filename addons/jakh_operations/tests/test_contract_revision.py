from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .test_certificate_completion import CommercialCompletionCase


@tagged('post_install', '-at_install', 'jakh')
class TestContractRevision(CommercialCompletionCase):
    def _revision(self, contract, effect='addition', quantity=2, original=True):
        return self.env['jakh.contract.boq.revision'].with_user(self.manager).create({
            'name': 'Controlled same-contract revision', 'contract_id': contract.id, 'reason': 'Approved customer scope instruction.',
            'line_ids': [Command.create({'code': 'CC-BOQ' if original else 'CC-NEW', 'name': 'Changed measured work',
                'effect': effect, 'contract_line_id': contract.line_ids.id if original else False,
                'wbs_id': self.wbs.id, 'uom_id': self.unit.id, 'quantity': quantity,
                'unit_price': 100, 'direct_unit_cost': 80})],
        })

    def test_addition_preserves_baseline_and_publishes_once(self):
        contract = self._contract()
        revision = self._revision(contract)
        revision.action_submit(); revision.action_approve(); revision.action_approve()
        self.assertEqual(revision.state, 'approved')
        self.assertEqual(revision.approved_number, 1)
        self.assertEqual(contract.line_ids.quantity, 10)
        self.assertEqual(contract.line_ids.source_line_uuid, 'cc-source-line-1')
        self.assertEqual(contract.original_contract_value, 1000)
        self.assertEqual(contract.current_contract_value, 1200)
        self.assertEqual(len(revision.line_ids.variation_id), 1)
        self.assertEqual(revision.line_ids.variation_id.contract_id, contract)
        self.assertEqual(len(revision.budget_revision_ids), 1)
        self.assertEqual(revision.budget_revision_ids.approved_delta, 160)
        self.assertEqual(self.env['jakh.project.control.entry']._effective_budget(self.wbs), 960)
        report = self.env['jakh.contract.boq.report'].with_user(self.manager).create({'contract_id': contract.id})
        report.action_generate()
        self.assertEqual(report.original_value, 1000)
        self.assertEqual(report.revised_value, 1200)
        self.assertEqual(report.line_ids.current_quantity, 12)
        self.assertEqual(report.line_ids.original_quantity, 10)
        certificate = self.env['jakh.payment.certificate'].with_user(self.manager).create({'name': 'Revised scope certificate', 'contract_id': contract.id})
        certificate.action_prepare_lines()
        certificate.line_ids.write({'cumulative_quantity': 10})
        certificate.variation_line_ids.write({'cumulative_quantity': 2})
        certificate.action_submit(); certificate.action_approve()
        self.assertEqual(certificate.period_gross, 1200)

    def test_unperformed_omission_reduces_scope_without_double_invoice_deduction(self):
        contract = self._contract()
        first = self._certificate(contract, 4); first.action_submit(); first.action_approve()
        revision = self._revision(contract, effect='omission', quantity=3)
        revision.action_submit(); revision.action_approve()
        self.assertTrue(revision.line_ids.variation_id.boq_scope_omission)
        self.assertEqual(contract.line_ids.quantity, 10)
        self.assertEqual(contract.line_ids._jakh_measurement_limit(), 7)
        self.assertEqual(contract.original_contract_value, 1000)
        self.assertEqual(contract.current_contract_value, 700)
        second = self.env['jakh.payment.certificate'].with_user(self.manager).create({'name': 'Completion of revised scope', 'contract_id': contract.id})
        second.action_prepare_lines()
        self.assertFalse(second.variation_line_ids)
        second.line_ids.write({'cumulative_quantity': 7})
        second.action_submit(); second.action_approve()
        self.assertEqual(second.period_gross, 300)
        self.assertEqual(second.line_ids.current_quantity, 3)
        self.assertEqual(second.line_ids.remaining_quantity, 0)
        self.assertEqual(second.line_ids.approved_contract_quantity, 7)
        self.assertEqual(first.line_ids.remaining_quantity, 6, 'Historical quantity evidence remains frozen.')
        excess = self._revision(contract, effect='omission', quantity=1)
        excess.action_submit()
        with self.assertRaises(UserError), self.cr.savepoint():
            excess.action_approve()
        self.assertEqual(excess.state, 'submitted')

    def test_submitted_revision_and_generated_sources_are_immutable(self):
        contract = self._contract()
        revision = self._revision(contract, original=False)
        revision.action_submit()
        with self.assertRaises(UserError), self.cr.savepoint():
            revision.line_ids.write({'quantity': 9})
        with self.assertRaises(AccessError), self.cr.savepoint():
            revision.write({'approved_number': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            revision.line_ids.write({'variation_id': 1})
        revision.action_approve()
        with self.assertRaises(UserError), self.cr.savepoint():
            revision.write({'reason': 'Rewrite approved basis'})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.project.variation'].with_user(self.manager).create({
                'name': 'Forged omission', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
                'contract_id': contract.id, 'quantity': 1, 'unit_price': 1, 'uom_id': self.unit.id,
                'description': 'Forged source flag', 'boq_scope_omission': True})

    def test_stale_submission_and_invalid_omission_are_blocked(self):
        contract = self._contract()
        revision = self._revision(contract)
        revision.action_submit()
        certificate = self._certificate(contract, 1); certificate.action_submit(); certificate.action_approve()
        with self.assertRaises(UserError), self.cr.savepoint():
            revision.action_approve()
        self.assertFalse(revision.line_ids.variation_id)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._revision(contract, effect='omission', original=False)

    def test_current_boq_reports_new_scope_and_restricted_financials(self):
        contract = self._contract()
        revision = self._revision(contract, quantity=3, original=False)
        revision.action_submit(); revision.action_approve()
        report = self.env['jakh.contract.boq.report'].with_user(self.manager).create({'contract_id': contract.id})
        report.action_generate()
        self.assertEqual(len(report.line_ids), 2)
        self.assertEqual(sum(report.line_ids.mapped('selling_value')), 1300)
        with self.assertRaises(AccessError), self.cr.savepoint():
            report.line_ids.write({'current_quantity': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            report.with_user(self.technical).read(['revised_value'])
