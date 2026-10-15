from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestConstructionSetup(TransactionCase):
    def test_wbs_state_defaults_cannot_bypass_release_action(self):
        manager = new_test_user(self.env, login='construction.state.manager', groups='jakh_core.group_manager')
        project = self.env['project.project'].with_user(manager).create({
            'name': 'Synthetic controlled WBS release', 'company_id': self.env.company.id})
        Wbs = self.env['jakh.wbs'].with_user(manager)
        values = {'name': 'Controlled draft', 'code': 'STATE-01', 'project_id': project.id, 'budget_amount': 100}
        for model in (Wbs, Wbs.sudo()):
            for supplied in (values, values | {'state': 'draft'}):
                with self.assertRaises(AccessError), self.cr.savepoint():
                    model.with_context(default_state='released').create(supplied)
        self.assertFalse(Wbs.search([('project_id', '=', project.id)]))
        row = Wbs.with_context(default_state='draft').create(values)
        self.assertEqual(row.state, 'draft')
        row.action_release()
        self.assertEqual(row.state, 'released')
        self.assertEqual(self.env['jakh.audit.event'].sudo().search_count([
            ('resource_model', '=', 'jakh.wbs'), ('resource_id', '=', row.id), ('operation', '=', 'wbs.released')]), 1)

    def test_currency_can_change_before_release_but_not_after_hidden_released_history(self):
        company = self.env['res.company'].create({'name': 'Synthetic construction currency policy'})
        currency = self.env['res.currency'].create({'name': 'QCC', 'symbol': 'QCC', 'rounding': 0.01, 'active': True})
        admin = new_test_user(self.env, login='construction.currency.admin', groups='base.group_system',
                             company_id=company.id, company_ids=[(6, 0, company.ids)])
        director = new_test_user(self.env, login='construction.currency.director', groups='jakh_core.group_manager',
                                company_id=company.id, company_ids=[(6, 0, company.ids)])
        technical = new_test_user(self.env, login='construction.currency.technical', groups='jakh_core.group_technical',
                                 company_id=company.id, company_ids=[(6, 0, company.ids)])
        Company = company.with_user(admin).with_context(allowed_company_ids=company.ids)
        project = self.env['project.project'].with_user(director).with_context(allowed_company_ids=company.ids).create({
            'name': 'Synthetic private construction history', 'company_id': company.id, 'privacy_visibility': 'followers',
            'user_id': director.id})
        wbs = self.env['jakh.wbs'].with_user(director).with_context(allowed_company_ids=company.ids).create({
            'name': 'Draft budget', 'code': 'CCY-01', 'project_id': project.id, 'budget_amount': 100})
        # A draft, unposted budget is not historical operational currency truth.
        Company.write({'currency_id': currency.id})
        self.assertEqual(company.currency_id, currency)
        with self.assertRaises(AccessError), self.cr.savepoint():
            company.with_user(technical).with_context(allowed_company_ids=company.ids).write({'currency_id': self.env.company.currency_id.id})
        wbs.action_release()
        alternative = self.env['res.currency'].create({'name': 'QCD', 'symbol': 'QCD', 'rounding': 0.01, 'active': True})
        with self.assertRaisesRegex(ValidationError, 'currency cannot change'), self.cr.savepoint():
            Company.write({'currency_id': alternative.id})
        self.assertEqual(company.currency_id, currency)

    def test_cost_centre_is_native_company_scoped_and_idempotent(self):
        manager = new_test_user(self.env, login='construction.setup.manager', groups='jakh_core.group_manager')
        technical = new_test_user(self.env, login='construction.setup.technical', groups='jakh_core.group_technical')
        project = self.env['project.project'].with_user(manager).create({'name': 'Synthetic setup', 'company_id': self.env.company.id})
        account = project._jakh_prepare_cost_centre()
        self.assertEqual(project._jakh_prepare_cost_centre(), account)
        self.assertEqual(account.company_id, project.company_id)
        with self.assertRaises(AccessError), self.cr.savepoint():
            project.with_user(technical).action_jakh_prepare_cost_centre()
        Location = self.env['jakh.project.location'].with_user(manager)
        building = Location.create({'name': 'Building A', 'code': 'A', 'project_id': project.id})
        floor = Location.create({'name': 'Floor 1', 'code': 'A-1', 'kind': 'floor', 'parent_id': building.id, 'project_id': project.id})
        self.assertEqual(floor.parent_id, building)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            floor.write({'parent_id': floor.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            Location.create({'name': 'Orphan floor', 'code': 'NO', 'kind': 'floor', 'project_id': project.id})
