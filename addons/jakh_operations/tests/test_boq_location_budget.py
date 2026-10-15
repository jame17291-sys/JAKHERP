from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestBoqLocationBudget(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='boq.allocation.manager', groups='jakh_core.group_manager')
        cls.pm = new_test_user(cls.env, login='boq.allocation.pm', groups='jakh_core.group_project_manager')
        cls.technical = new_test_user(cls.env, login='boq.allocation.technical', groups='jakh_core.group_technical')
        company = cls.env.company
        cls.project = cls.env['project.project'].with_user(cls.manager).create({'name': 'BOQ allocation project',
            'company_id': company.id, 'user_id': cls.manager.id, 'privacy_visibility': 'employees'})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({'name': 'Shared original budget', 'code': 'BQL-01',
            'project_id': cls.project.id, 'budget_amount': 1000})
        cls.wbs.action_release()
        cls.locations = cls.env['jakh.project.location'].with_user(cls.manager).create([
            {'name': 'Villa group A', 'code': 'V-A', 'project_id': cls.project.id},
            {'name': 'Villa group B', 'code': 'V-B', 'project_id': cls.project.id}])
        ar = cls.env['account.account'].create({'code': 'BQL9001', 'name': 'Allocation receivable', 'account_type': 'asset_receivable',
            'reconcile': True, 'company_ids': [Command.set(company.ids)]})
        income = cls.env['account.account'].create({'code': 'BQL9002', 'name': 'Allocation income', 'account_type': 'income',
            'company_ids': [Command.set(company.ids)]})
        journal = cls.env['account.journal'].create({'name': 'Allocation sales', 'code': 'BQLS', 'type': 'sale',
            'company_id': company.id, 'default_account_id': income.id})
        customer = cls.env['res.partner'].create({'name': 'Allocation customer', 'property_account_receivable_id': ar.id})
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({'name': 'Frozen two-item contract',
            'project_id': cls.project.id, 'partner_id': customer.id, 'journal_id': journal.id, 'income_account_id': income.id,
            'line_ids': [Command.create({'code': code, 'name': code, 'wbs_id': cls.wbs.id, 'quantity': quantity,
                'unit_price': 200, 'uom_id': cls.env.ref('uom.product_uom_unit').id}) for code, quantity in [('BQL-A', 10), ('BQL-B', 5)]]})
        cls.contract.action_activate()
        cls.boq = cls.contract.line_ids.sorted('id')[0]
        cls.boq2 = cls.contract.line_ids.sorted('id')[1]
        cls.Plans = cls.env['jakh.boq.location.budget'].with_user(cls.manager)
        cls.control = cls.env['jakh.construction.control'].with_user(cls.manager)

    def _plan(self, rows=((6, 600), (4, 400)), **values):
        return self.Plans.create({'contract_line_id': self.boq.id, 'reason': 'Reviewed measured building scope and original budget distribution.',
            'line_ids': [Command.create({'location_id': location.id, 'quantity': quantity, 'budget_amount': budget})
                         for location, (quantity, budget) in zip(self.locations, rows)], **values})

    def _approve(self, plan):
        plan.action_submit(); plan.action_approve()
        return plan

    def test_two_buildings_allocate_original_budget_and_revenue_without_changing_baseline(self):
        plan = self._approve(self._plan())
        plan.action_approve()
        self.assertEqual(self.wbs.budget_amount, 1000)
        self.assertEqual(self.boq.quantity, 10)
        self.assertEqual(self.boq.unit_price, 200)
        self.assertEqual(plan.original_quantity, 10)
        self.assertEqual(plan.original_wbs_budget, 1000)
        self.assertEqual(plan.unallocated_wbs_budget, 0)
        self.assertEqual(plan.approved_by, self.manager)
        rows = self.control._cost_rows(self.project, fields.Date.today())
        self.assertEqual(sum(row['original_budget'] for row in rows), 1000)
        self.assertEqual(sum(row['original_revenue'] for row in rows), 3000)
        first = [row for row in rows if row['building_id'] == self.locations[0].id]
        second = [row for row in rows if row['building_id'] == self.locations[1].id]
        self.assertEqual(sum(row['original_budget'] for row in first), 600)
        self.assertEqual(sum(row['original_revenue'] for row in first), 1200)
        self.assertEqual(sum(row['original_budget'] for row in second), 400)
        self.assertEqual(sum(row['original_revenue'] for row in second), 800)
        events = self.env['jakh.audit.event'].search([('resource_model', '=', plan._name), ('resource_id', '=', plan.id),
            ('operation', '=', 'boq_location_budget.approved')])
        self.assertEqual(len(events), 1)
        with self.assertRaises(UserError):
            plan.write({'reason': 'Tampered'})
        with self.assertRaises(UserError):
            plan.line_ids.write({'budget_amount': 999})

    def test_partial_quantity_and_budget_remainders_stay_unallocated_and_reconcile(self):
        plan = self._approve(self._plan(rows=((3, 250), (2, 200)), allocation_mode='partial'))
        self.assertEqual(plan.unallocated_quantity, 5)
        self.assertEqual(plan.unallocated_wbs_budget, 550)
        rows = self.control._cost_rows(self.project, fields.Date.today())
        self.assertEqual(sum(row['original_budget'] for row in rows), 1000)
        self.assertEqual(sum(row['original_revenue'] for row in rows), 3000)
        self.assertEqual(sum(row['original_budget'] for row in rows if not row['location_id']), 550)
        self.assertEqual(sum(row['original_revenue'] for row in rows if not row['location_id']), 2000)
        self.assertFalse(all(row['dimension_complete'] for row in rows))

    def test_replacements_preserve_dated_history_and_do_not_double_count(self):
        earlier = fields.Date.today() - timedelta(days=10)
        old = self._approve(self._plan(date=earlier))
        new = self._approve(self._plan(rows=((4, 300), (6, 700)), replaces_id=old.id))
        self.assertEqual(old.state, 'approved')
        self.assertEqual(new.state, 'approved')
        historic = self.control._cost_rows(self.project, earlier)
        current = self.control._cost_rows(self.project, fields.Date.today())
        self.assertEqual(sum(row['original_budget'] for row in historic if row['building_id'] == self.locations[0].id), 600)
        self.assertEqual(sum(row['original_budget'] for row in current if row['building_id'] == self.locations[0].id), 300)
        self.assertEqual(sum(row['original_budget'] for row in current), 1000)
        stale = self._plan(replaces_id=old.id)
        stale.action_submit()
        with self.assertRaises(UserError):
            stale.action_approve()

    def test_shared_wbs_budget_quantity_caps_and_future_dates_are_enforced(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._plan(date=fields.Date.today() + timedelta(days=1))
        excessive = self._plan(rows=((6, 600), (5, 400)))
        with self.assertRaises(UserError):
            excessive.action_submit()
        incomplete = self._plan(rows=((3, 300), (2, 200)))
        with self.assertRaises(UserError):
            incomplete.action_submit()
        self._approve(self._plan(rows=((6, 500), (4, 300))))
        other = self._plan(rows=((3, 150), (2, 150)), contract_line_id=self.boq2.id)
        other.action_submit()
        with self.assertRaises(UserError):
            other.action_approve()
        self.assertEqual(self.wbs.budget_amount, 1000)

    def test_backdated_other_boq_cannot_consume_capacity_released_by_a_later_plan(self):
        january = fields.Date.today() - timedelta(days=20)
        february = fields.Date.today() - timedelta(days=10)
        old = self._approve(self._plan(rows=((6, 500), (4, 0)), date=january))
        self._approve(self._plan(rows=((6, 0), (4, 0)), date=february, replaces_id=old.id))
        # Today's A allocation is zero, but at this historical boundary it was
        # still 500. B's 700 cannot make the historical shared baseline 1200.
        candidate = self._plan(rows=((3, 700), (2, 0)), contract_line_id=self.boq2.id,
                               date=january + timedelta(days=5))
        candidate.action_submit()
        with self.assertRaises(UserError):
            candidate.action_approve()
        self.assertEqual(candidate.state, 'submitted')
        self.assertEqual(old.allocated_budget, 500)
        historic = self.control._cost_rows(self.project, january + timedelta(days=5))
        self.assertEqual(sum(row['original_budget'] for row in historic), 1000)
        allowed = self._plan(rows=((3, 700), (2, 0)), contract_line_id=self.boq2.id, date=february)
        self._approve(allowed)
        self.assertEqual(allowed.unallocated_wbs_budget, 300)

    def test_same_date_reviewed_replacement_order_is_approval_not_draft_creation(self):
        replacement = self._plan(rows=((4, 300), (6, 700)))
        original = self._approve(self._plan())
        self.assertLess(replacement.id, original.id)
        replacement.write({'replaces_id': original.id})
        self._approve(replacement)
        self.assertEqual(original.approved_revision, 1)
        self.assertEqual(replacement.approved_revision, 2)
        self.assertEqual(self.Plans._latest(self.project, fields.Date.today())[self.boq.id], replacement)
        rows = self.control._cost_rows(self.project, fields.Date.today())
        self.assertEqual(sum(row['original_budget'] for row in rows if row['building_id'] == self.locations[0].id), 300)

    def test_fractional_currency_allocations_use_decimal_source_sums(self):
        wbs = self.env['jakh.wbs'].with_user(self.manager).create({'name': 'Decimal shared budget', 'code': 'BQL-DEC',
            'project_id': self.project.id, 'budget_amount': 0.3})
        wbs.action_release()
        contract = self.env['jakh.project.contract'].with_user(self.manager).create({'name': 'Decimal source contract',
            'project_id': self.project.id, 'partner_id': self.contract.partner_id.id,
            'journal_id': self.contract.journal_id.id, 'income_account_id': self.contract.income_account_id.id,
            'line_ids': [Command.create({'code': code, 'name': code, 'wbs_id': wbs.id, 'quantity': 10,
                'unit_price': 1, 'uom_id': self.boq.uom_id.id}) for code in ('DEC-A', 'DEC-B')]})
        contract.action_activate()
        a, b = contract.line_ids.sorted('id')
        first = self._approve(self._plan(rows=((6, 0.1), (4, 0.2)), contract_line_id=a.id))
        self.assertEqual(first.allocated_budget, 0.3)
        second = self._approve(self._plan(rows=((6, 0), (4, 0)), contract_line_id=b.id))
        self.assertEqual(second.unallocated_wbs_budget, 0)
        self.assertEqual(wbs.budget_amount, 0.3)

    def test_unauthorised_approvals_forged_snapshots_and_closed_work_are_blocked(self):
        plan = self._plan(); plan.action_submit()
        with self.assertRaises(AccessError):
            plan.with_user(self.pm).action_approve()
        with self.assertRaises(AccessError):
            self.Plans.with_user(self.technical).create({'contract_line_id': self.boq.id, 'reason': 'Denied'})
        with self.assertRaises(AccessError):
            self.Plans.with_context(default_state='approved').create({'contract_line_id': self.boq.id, 'reason': 'Forged'})
        with self.assertRaises(AccessError):
            self.Plans.create({'contract_line_id': self.boq.id, 'reason': 'Forged', 'original_wbs_budget': 99999})
        with self.assertRaises(AccessError):
            self.Plans.create({'contract_line_id': self.boq.id, 'reason': 'Forged', 'approved_revision': 99999})
        with self.assertRaises(AccessError):
            plan.line_ids.write({'company_id': self.env.company.id})
        self.project._jakh_set_closed(True)
        with self.assertRaises(UserError):
            plan.action_approve()
