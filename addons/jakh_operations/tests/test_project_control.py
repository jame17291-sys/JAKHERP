from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestProjectControl(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.control.manager', groups='jakh_core.group_manager')
        company = cls.env.company
        Account = cls.env['account.account']

        def account(code, name, account_type, reconcile=False):
            return Account.create({
                'code': code, 'name': name, 'account_type': account_type, 'reconcile': reconcile,
                'company_ids': [Command.set(company.ids)],
            })

        receivable = account('JPC9001', 'Project control receivables', 'asset_receivable', True)
        income = account('JPC9002', 'Project control revenue', 'income')
        journal = cls.env['account.journal'].create({
            'name': 'Project control sales', 'code': 'JPC', 'type': 'sale',
            'company_id': company.id, 'default_account_id': income.id,
        })
        cls.customer = cls.env['res.partner'].create({
            'name': 'Project control customer', 'property_account_receivable_id': receivable.id,
        })
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Project control project', 'company_id': company.id, 'user_id': cls.manager.id,
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Project control WBS', 'code': 'CTRL-01', 'project_id': cls.project.id,
            'budget_amount': 1000,
        })
        cls.wbs.action_release()
        cls.contract = cls.env['jakh.project.contract'].with_user(cls.manager).create({
            'name': 'Project control contract', 'project_id': cls.project.id,
            'partner_id': cls.customer.id, 'journal_id': journal.id, 'income_account_id': income.id,
            'line_ids': [Command.create({
                'code': 'CTRL-BOQ', 'name': 'Controlled work', 'wbs_id': cls.wbs.id,
                'quantity': 10, 'unit_price': 100, 'uom_id': cls.env.ref('uom.product_uom_unit').id,
            })],
        })
        cls.contract.action_activate()

    def test_approved_budget_forecast_variation_and_cvr_are_traceable(self):
        revision = self.env['jakh.project.budget.revision'].with_user(self.manager).create({
            'name': 'Approved additional scope budget', 'wbs_id': self.wbs.id,
            'proposed_budget': 1200, 'reason': 'Approved client scope extension',
        })
        revision.action_submit()
        revision.action_approve()
        self.assertEqual(revision.approved_delta, 200)

        forecast = self.env['jakh.project.forecast'].with_user(self.manager).create({
            'name': 'October forecast', 'wbs_id': self.wbs.id, 'forecast_final_cost': 1300,
            'basis': 'Committed procurement plus reviewed productivity risk.',
        })
        forecast.action_submit()
        forecast.action_approve()

        variation = self.env['jakh.project.variation'].with_user(self.manager).create({
            'name': 'Owner instructed additional work', 'variation_type': 'owner', 'effect': 'addition',
            'project_id': self.project.id, 'wbs_id': self.wbs.id, 'contract_id': self.contract.id,
            'description': 'Approved additional controlled work.', 'quantity': 2,
            'uom_id': self.env.ref('uom.product_uom_unit').id, 'unit_price': 50,
        })
        variation.action_submit()
        variation.action_price()
        variation.action_approve()
        self.assertEqual(variation.state, 'approved')
        self.assertEqual(variation.amount, 100)

        certificate = self.env['jakh.payment.certificate'].with_user(self.manager).create({
            'name': 'Project control certificate', 'contract_id': self.contract.id,
        })
        certificate.action_prepare_lines()
        certificate.line_ids.write({'cumulative_quantity': 5})
        self.assertEqual(certificate.variation_line_ids.variation_id, variation)
        certificate.variation_line_ids.write({'cumulative_quantity': 2})
        certificate.action_submit()
        certificate.action_approve()
        self.assertEqual(certificate.period_gross, 600)
        self.assertEqual(variation.state, 'certified')

        report = self.env['jakh.project.control.report'].with_user(self.manager).create({
            'project_id': self.project.id, 'as_of_date': fields.Date.today(),
        })
        report.action_generate()
        line = report.line_ids
        self.assertEqual(line.original_budget, 1000)
        self.assertEqual(line.revised_budget, 1200)
        self.assertEqual(line.forecast_final_cost, 1300)
        self.assertEqual(line.original_revenue, 1000)
        self.assertEqual(line.variation_revenue, 100)
        self.assertEqual(line.revised_revenue, 1100)
        self.assertEqual(line.certified_revenue, 600)
        self.assertEqual(line.forecast_margin, -200)
        entries = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('project_id', '=', self.project.id),
        ])
        self.assertEqual(set(entries.mapped('entry_type')),
                         {'budget_revision', 'forecast_final_cost', 'variation_revenue', 'certified_revenue'})
        with self.assertRaises(AccessError):
            self.env['jakh.project.control.entry'].with_user(self.manager).create({
                'name': 'Forbidden entry', 'company_id': self.env.company.id, 'project_id': self.project.id,
                'wbs_id': self.wbs.id, 'entry_type': 'actual_cost', 'amount': 1,
                'date': fields.Date.today(), 'source_model': 'x', 'source_id': 1, 'source_key': 'forbidden',
            })
