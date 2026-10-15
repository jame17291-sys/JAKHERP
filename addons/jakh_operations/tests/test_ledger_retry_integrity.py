from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestLedgerRetryIntegrity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='ledger.retry.manager', groups='jakh_core.group_manager')
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Synthetic replay project', 'company_id': cls.env.company.id, 'privacy_visibility': 'employees'})
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Replay scope', 'code': 'RETRY-01', 'project_id': cls.project.id, 'budget_amount': 1000})
        cls.partner = cls.env['res.partner'].create({'name': 'Synthetic original counterparty'})
        cls.other_partner = cls.env['res.partner'].create({'name': 'Synthetic revised counterparty'})

    def _arguments(self, **changes):
        return dict(name='Original localized display', company=self.env.company, project=self.project, wbs=self.wbs,
                    entry_type='commitment', amount=10.004, quantity=1.0000004,
                    source_model=self.project._name, source_id=self.project.id,
                    source_key='controlled-replay:%s' % self.project.id,
                    entry_date=fields.Date.today(), partner=self.partner, notes='Display only') | changes

    def test_identical_normalized_retry_returns_original_and_ignores_display_translation(self):
        Entry = self.env['jakh.project.control.entry'].with_user(self.manager)
        values = self._arguments()
        original = Entry._record(**values)
        self.assertEqual(original.amount, self.env.company.currency_id.round(10.004))
        self.assertEqual(original.quantity, 1.0)
        replay = Entry.with_context(lang='ar_001')._record(**{**values, 'name': 'وصف مترجم', 'notes': 'وصف للعرض فقط',
            'amount': 10.0001, 'quantity': 1.00000049, 'entry_date': str(fields.Date.today())})
        self.assertEqual(replay.id, original.id)
        self.assertEqual(replay.name, 'Original localized display')
        self.assertEqual(Entry.search_count([('source_key', '=', values['source_key'])]), 1)
        audits = self.env['jakh.audit.event'].with_user(self.manager).search([
            ('resource_model', '=', original._name), ('resource_id', '=', original.id),
            ('operation', '=', 'project_control.recorded')])
        self.assertEqual(len(audits), 1)

    def test_changed_monetary_quantity_date_counterparty_or_provenance_replay_is_rejected(self):
        Entry = self.env['jakh.project.control.entry'].with_user(self.manager)
        values = self._arguments()
        original = Entry._record(**values)
        for change in ({'amount': 10.01}, {'quantity': 1.000001},
                       {'entry_date': fields.Date.today() - timedelta(days=1)},
                       {'partner': self.other_partner}, {'provenance': 'integration'}):
            with self.subTest(change=list(change)), self.assertRaises(ValidationError), self.cr.savepoint():
                Entry._record(**{**values, **change})
        self.assertEqual(original.amount, self.env.company.currency_id.round(10.004))
        self.assertEqual(original.quantity, 1.0)
        self.assertEqual(original.partner_id, self.partner)
        self.assertEqual(original.date, fields.Date.today())

    def test_contract_and_foreign_source_company_cannot_change_the_fact(self):
        Entry = self.env['jakh.project.control.entry'].with_user(self.manager)
        values = self._arguments()
        Entry._record(**values)
        contract = self.env['jakh.project.contract'].with_user(self.manager).create({
            'name': 'Synthetic draft replay contract', 'project_id': self.project.id, 'partner_id': self.partner.id})
        with self.assertRaisesRegex(ValidationError, 'counterparty, contract or provenance'), self.cr.savepoint():
            Entry._record(**{**values, 'contract': contract})
        other_company = self.env['res.company'].create({'name': 'Synthetic replay other entity'})
        self.manager.company_ids |= other_company
        other_project = self.env['project.project'].with_user(self.manager).with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]).create({
                'name': 'Synthetic foreign source', 'company_id': other_company.id, 'privacy_visibility': 'employees'})
        with self.assertRaisesRegex(ValidationError, 'same legal entity'), self.cr.savepoint():
            Entry.with_context(allowed_company_ids=[self.env.company.id, other_company.id])._record(**{
                **values, 'source_id': other_project.id, 'source_key': 'foreign-source:%s' % other_project.id})

    def test_operational_fact_locks_company_currency_even_without_a_released_wbs(self):
        other_company = self.env['res.company'].create({'name': 'Synthetic facts currency entity'})
        self.manager.company_ids |= other_company
        context = {'allowed_company_ids': [other_company.id]}
        project = self.env['project.project'].with_user(self.manager).with_context(**context).create({
            'name': 'Synthetic currency fact project', 'company_id': other_company.id, 'privacy_visibility': 'employees'})
        wbs = self.env['jakh.wbs'].with_user(self.manager).with_context(**context).create({
            'name': 'Draft fact fixture', 'code': 'FACT-CCY', 'project_id': project.id})
        self.env['jakh.project.control.entry'].with_user(self.manager).with_context(**context)._record(
            name='Synthetic controlled pre-release fact', company=other_company, project=project, wbs=wbs,
            entry_type='commitment', amount=10, source_model=project._name, source_id=project.id,
            source_key='currency-first-fact:%s' % project.id)
        currency = self.env['res.currency'].create({'name': 'QRY', 'symbol': 'QRY', 'rounding': 0.01, 'active': True})
        with self.assertRaisesRegex(ValidationError, 'currency cannot change'), self.cr.savepoint():
            other_company.with_context(**context).write({'currency_id': currency.id})
