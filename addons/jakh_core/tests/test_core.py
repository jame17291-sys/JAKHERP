from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestJakhCore(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.core.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='jakh.core.technical', groups='jakh_core.group_technical')
        cls.project = cls.env['project.project'].create({'name': 'QA Construction Project', 'company_id': cls.env.company.id, 'privacy_visibility': 'employees'})
        cls.cost_code = cls.env['jakh.cost.code'].create({'name': 'Concrete', 'code': 'CC-QA-01'})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Concrete foundations', 'code': 'WBS-QA-01',
            'project_id': cls.project.id, 'cost_code_id': cls.cost_code.id, 'budget_amount': 10000,
        })

    def test_release_guard_and_audit_immutable(self):
        with self.assertRaises(AccessError):
            self.wbs.with_user(self.manager).write({'state': 'released'})
        self.wbs.with_user(self.manager).action_release()
        self.assertEqual(self.wbs.state, 'released')
        self.wbs.with_user(self.manager).action_release()
        events = self.env['jakh.audit.event'].search([('resource_model', '=', 'jakh.wbs'), ('resource_id', '=', self.wbs.id)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events.actor_id, self.manager)
        with self.assertRaises(AccessError):
            events.write({'operation': 'tampered'})
        with self.assertRaises(AccessError):
            events.unlink()
        with self.assertRaises(ValidationError):
            self.wbs.write({'budget_amount': 20000})

    def test_technical_cannot_read_money(self):
        record = self.wbs.with_user(self.technical)
        self.assertEqual(record.read(['name'])[0]['name'], 'Concrete foundations')
        with self.assertRaises(AccessError):
            record.read(['budget_amount'])
        with self.assertRaises(AccessError):
            record.write({'budget_amount': 1})

    def test_hierarchy_and_company(self):
        other = self.env['project.project'].create({'name': 'Other QA Project', 'company_id': self.env.company.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env['jakh.wbs'].create({'name': 'Bad parent', 'code': 'BAD', 'project_id': other.id, 'parent_id': self.wbs.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.cost_code.write({'parent_id': self.cost_code.id})

    def test_dashboard_permission_safe(self):
        result = self.env['jakh.dashboard'].with_user(self.technical).get_data()
        self.assertTrue(result['generated_at'])
        actions = {item['action'] for item in result['cards']}
        self.assertNotIn('jakh_core.action_crm', actions)
        self.assertNotIn('account.action_move_out_invoice_type', actions)
        self.assertNotIn('account.action_move_in_invoice_type', actions)
        for card in result['cards']:
            action = self.env.ref(card['action'])
            model = self.env[action.res_model].with_user(self.technical)
            self.assertTrue(model.has_access('read'))
            self.assertEqual(card['count'], model.search_count(card['domain']))

    def test_dashboard_rejects_portal(self):
        portal = new_test_user(self.env, login='jakh.dashboard.portal', groups='base.group_portal')
        with self.assertRaises(AccessError):
            self.env['jakh.dashboard'].with_user(portal).get_data()

    def test_language_policy_keeps_native_english_arabic_only(self):
        languages = self.env['res.lang'].with_context(active_test=False)
        french = languages.search([('code', '=', 'fr_FR')], limit=1)
        self.assertTrue(french)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            french.write({'active': True})
        self.assertFalse(french.active)
        self.assertEqual(set(code for code, name in languages.get_installed()) - {'en_US', 'ar_001'}, set())
        # Keep the native locale catalog, and do not delete language metadata.
        self.assertTrue(french.exists())
        self.assertEqual(self.env['base.language.install']._fields['lang_ids'].domain,
                         [('code', 'in', ('en_US', 'ar_001'))])

    def test_dashboard_counts_and_drilldown_keep_selected_company_scope(self):
        other = self.env['res.company'].create({'name': 'QA Other Dashboard Company'})
        self.manager.write({'company_ids': [Command.link(other.id)]})
        hidden_project = self.env['project.project'].create({
            'name': 'QA Other Company Project', 'company_id': other.id, 'privacy_visibility': 'employees',
        })
        dashboard = self.env['jakh.dashboard'].with_user(self.manager).with_context(allowed_company_ids=self.env.company.ids)
        result = dashboard.get_data()
        self.assertEqual(result['companies'], [self.env.company.name])
        project_card = next(card for card in result['cards'] if card['action'] == 'jakh_core.action_projects')
        records = dashboard.env['project.project'].search(project_card['domain'])
        self.assertIn(self.project.id, records.ids)
        self.assertNotIn(hidden_project.id, records.ids)
        self.assertEqual(project_card['count'], len(records))
        combined = dashboard.with_context(allowed_company_ids=[self.env.company.id, other.id]).get_data()
        combined_card = next(card for card in combined['cards'] if card['action'] == 'jakh_core.action_projects')
        self.assertEqual(combined_card['count'], project_card['count'] + 1)

    def test_dashboard_overdue_finance_counts_only_posted_past_due_documents(self):
        company = self.env.company
        accounts = {}
        for code, kind in [('JD9101', 'asset_receivable'), ('JD9102', 'liability_payable'), ('JD9103', 'income'), ('JD9104', 'expense')]:
            accounts[kind] = self.env['account.account'].create({
                'code': code, 'name': 'QA Dashboard ' + kind, 'account_type': kind,
                'reconcile': kind in ('asset_receivable', 'liability_payable'),
                'company_ids': [Command.set(company.ids)],
            })
        journals = {
            kind: self.env['account.journal'].create({
                'name': 'QA Dashboard ' + kind, 'code': code, 'type': kind,
                'company_id': company.id, 'default_account_id': accounts[account_type].id,
            }) for kind, code, account_type in [('sale', 'JDS', 'income'), ('purchase', 'JDP', 'expense')]
        }
        partner = self.env['res.partner'].create({
            'name': 'QA Dashboard Customer and Supplier',
            'property_account_receivable_id': accounts['asset_receivable'].id,
            'property_account_payable_id': accounts['liability_payable'].id,
        })
        dashboard = self.env['jakh.dashboard'].with_user(self.manager).with_context(allowed_company_ids=company.ids)
        baseline = {card['action']: card['count'] for card in dashboard.get_data()['cards']}
        today = fields.Date.context_today(dashboard)

        def invoice(move_type, due, post=True):
            customer = move_type == 'out_invoice'
            record = self.env['account.move'].create({
                'move_type': move_type, 'partner_id': partner.id, 'company_id': company.id,
                'journal_id': journals['sale' if customer else 'purchase'].id,
                'invoice_date': today - timedelta(days=2), 'invoice_date_due': due,
                'invoice_payment_term_id': False,
                'invoice_line_ids': [Command.create({
                    'name': 'QA Dashboard Invoice', 'quantity': 1, 'price_unit': 10,
                    'account_id': accounts['income' if customer else 'expense'].id,
                    'tax_ids': [Command.clear()],
                })],
            })
            if post:
                record.action_post()
            return record

        overdue_invoice = invoice('out_invoice', today - timedelta(days=1))
        overdue_bill = invoice('in_invoice', today - timedelta(days=1))
        draft = invoice('out_invoice', today - timedelta(days=1), post=False)
        future = invoice('out_invoice', today + timedelta(days=1))
        cards = {card['action']: card for card in dashboard.get_data()['cards']}
        for action, record in [('account.action_move_out_invoice_type', overdue_invoice), ('account.action_move_in_invoice_type', overdue_bill)]:
            card = cards[action]
            self.assertEqual(card['count'], baseline[action] + 1)
            records = dashboard.env['account.move'].search(card['domain'])
            self.assertEqual(card['count'], len(records))
            self.assertIn(record.id, records.ids)
            self.assertNotIn(draft.id, records.ids)
            self.assertNotIn(future.id, records.ids)
