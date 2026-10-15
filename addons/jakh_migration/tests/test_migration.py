import json

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes


@tagged('post_install', '-at_install', 'jakh')
class TestJakhMigration(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.migration.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='jakh.migration.technical', groups='jakh_core.group_technical')

    def _wizard(self, rows):
        return self.env['jakh.crm.import'].with_user(self.manager).create({
            'upload': BinaryBytes(json.dumps(rows).encode()), 'filename': 'synthetic-qa.json',
            'company_id': self.env.company.id, 'reviewed': True,
        })

    def test_original_currency_and_idempotency(self):
        company_currency = self.env.company.currency_id
        foreign = self.env['res.currency'].with_context(active_test=False).search([('name', '!=', company_currency.name)], limit=1)
        wizard = self._wizard([{'id': 'QA-CRM-001', 'title': 'Synthetic foreign opportunity', 'currency': foreign.name, 'value': 12345.67}])
        wizard.action_preview()
        wizard.action_import()
        wizard.action_import()
        leads = self.env['crm.lead'].search([('jakh_source_id', '=', 'QA-CRM-001')])
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads.jakh_source_currency_id, foreign)
        self.assertAlmostEqual(leads.jakh_source_amount, 12345.67)
        self.assertEqual(leads.expected_revenue, 0)
        self.assertEqual(leads.jakh_source_payload['currency'], foreign.name)

    def test_validation_before_any_import(self):
        wizard = self._wizard([{'id': 'QA-BAD-001', 'title': 'Synthetic invalid amount', 'value': 100}])
        with self.assertRaises(ValidationError):
            wizard.action_import()
        self.assertFalse(self.env['crm.lead'].search([('jakh_source_id', '=', 'QA-BAD-001')]))
        wizard = self._wizard([{'id': 'QA-OK-001', 'title': 'Review required'}])
        wizard.reviewed = False
        with self.assertRaises(ValidationError):
            wizard.action_import()

    def test_technical_cannot_import_or_read_source_money(self):
        with self.assertRaises(AccessError):
            self.env['jakh.crm.import'].with_user(self.technical).create({'upload': BinaryBytes(b'[]'), 'company_id': self.env.company.id})

    def test_clear_import_labels_preserve_original_source_identity(self):
        action = self.env.ref('jakh_migration.action_crm_import').with_context(lang='en_US')
        menu = self.env.ref('jakh_migration.menu_crm_import').with_context(lang='en_US')
        self.assertEqual(action.name, 'Import CRM Data')
        self.assertEqual(menu.name, 'CRM Data Import')
        self.assertEqual(action.res_model, 'jakh.crm.import')
        self.assertEqual(self.env['jakh.crm.import']._description, 'CRM Data Import')
        wizard = self._wizard([{'id': 'QA-SOURCE-001', 'title': 'Reviewed source identity'}])
        self.assertEqual(wizard.source_system, 'JAKH CRM')
        wizard.action_import()
        lead = self.env['crm.lead'].search([('jakh_source_id', '=', 'QA-SOURCE-001')])
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.jakh_source_system, 'JAKH CRM')

    def test_unknown_currency_error_is_platform_neutral(self):
        self.assertFalse(self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'ZZZ')]))
        wizard = self._wizard([{'id': 'QA-UNKNOWN-001', 'title': 'Unknown original currency',
                                'value': 100, 'currency': 'ZZZ'}])
        with self.assertRaisesRegex(ValidationError, 'The original currency is not available in the ERP\\.'):
            wizard.with_context(lang='en_US').action_preview()
        self.assertFalse(self.env['crm.lead'].search([('jakh_source_id', '=', 'QA-UNKNOWN-001')]))
