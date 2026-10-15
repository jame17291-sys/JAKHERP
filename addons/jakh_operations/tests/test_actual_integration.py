import copy
import hashlib
import json
from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.integration_workflow import _without_form_defaults


@tagged('post_install', '-at_install', 'jakh')
class TestActualIntegration(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='actual.integration.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='actual.integration.technical', groups='jakh_core.group_technical')
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'Integration finance project', 'company_id': cls.env.company.id, 'user_id': cls.manager.id,
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'Integration WBS', 'code': 'INT-COST', 'project_id': cls.project.id, 'budget_amount': 2000,
        })
        cls.wbs.action_release()
        cls.cost = cls.env['account.account'].create({'name': 'Integration labour cost', 'code': 'INT9001',
            'account_type': 'expense_direct_cost', 'company_ids': [Command.set(cls.env.company.ids)]})
        cls.offset = cls.env['account.account'].create({'name': 'Integration cost clearing', 'code': 'INT9002',
            'account_type': 'liability_current', 'company_ids': [Command.set(cls.env.company.ids)]})
        cls.journal = cls.env['account.journal'].create({'name': 'Integration verified costs', 'code': 'ICST',
            'type': 'general', 'company_id': cls.env.company.id})

    def _feed(self, **extra):
        return self.env['jakh.project.actual.feed'].with_user(self.manager).create({
            'name': 'Crew allocation actual', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'source_system': 'jisr', 'actual_type': 'labour', 'external_id': 'JISR-BATCH-1',
            'source_reference': 'Jisr approved batch', 'resource_reference': 'Crew A', 'quantity': 8, 'amount': 120,
            'journal_id': self.journal.id, 'expense_account_id': self.cost.id, 'offset_account_id': self.offset.id,
            **extra,
        })

    def test_posted_costs_and_corrections_reconcile_exactly(self):
        feed = self._feed(); feed.action_submit(); feed.action_accept(); feed.action_accept()
        move = feed.account_move_id
        self.assertEqual(move.state, 'posted')
        self.assertEqual(len(move.line_ids), 2)
        self.assertAlmostEqual(sum(move.line_ids.mapped('balance')), 0)
        entry = self.env['jakh.project.control.entry'].search([('source_model', '=', feed._name), ('source_id', '=', feed.id)])
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry.account_move_line_id.move_id, move)
        self.assertEqual(entry.amount, entry.account_move_line_id.balance)
        correction = self._feed(external_id='JISR-BATCH-1-CREDIT', effect='credit', correction_of_id=feed.id,
                                correction_reason='Two hours incorrectly allocated', amount=30, quantity=2)
        correction.action_submit(); correction.action_accept()
        actual = self.env['jakh.project.control.entry']._sum(self.wbs, ['actual_cost'])
        self.assertEqual(actual, 90)
        posted = self.env['account.move.line'].search([('account_id', '=', self.cost.id), ('parent_state', '=', 'posted')])
        self.assertEqual(sum(posted.mapped('balance')), float(actual))
        excess = self._feed(external_id='JISR-BATCH-1-EXCESS', effect='credit', correction_of_id=feed.id,
                            correction_reason='Invalid excess correction', amount=100)
        excess.action_submit()
        with self.assertRaises(UserError), self.cr.savepoint():
            excess.action_accept()
        with self.assertRaises(AccessError), self.cr.savepoint():
            move.line_ids[0].write({'debit': 999})
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['account.move.line'].with_user(self.manager).create([
                {'move_id': move.id, 'name': 'Forged extra cost', 'account_id': self.cost.id, 'debit': 50},
                {'move_id': move.id, 'name': 'Forged extra accrual', 'account_id': self.offset.id, 'credit': 50},
            ])
        with self.assertRaises(UserError), self.cr.savepoint():
            move.button_draft()
        with self.assertRaises(AccessError), self.cr.savepoint():
            move.with_user(self.manager).write({'state': 'draft'})

    def _payload(self):
        return {'version': 1, 'kind': 'p6', 'external_id': 'P6-SOURCE-1',
            'company_id': self.env.company.id, 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'data': {'source_reference': 'P6 approved status', 'as_of_date': str(fields.Date.today()),
                     'planned_value': 120, 'earned_value': 90, 'progress_percent': 75}}

    def test_posted_resource_cost_analytic_scope_cannot_be_reassigned(self):
        feed = self._feed(external_id='JISR-ANALYTIC-LOCK')
        feed.action_submit(); feed.action_accept()
        other_project = self.env['project.project'].with_user(self.manager).create({
            'name': 'Unrelated analytic project', 'company_id': self.env.company.id})
        other_account = other_project._jakh_prepare_cost_centre()
        line = feed.account_move_id.line_ids.filtered(lambda row: row.account_id == self.cost)
        original = dict(line.analytic_distribution or {})
        entry = self.env['jakh.project.control.entry'].search([
            ('source_model', '=', feed._name), ('source_id', '=', feed.id)])
        for source in (line.with_user(self.manager), line.sudo(),
                       line.with_user(self.manager).with_context(_jakh_actual_posting=True)):
            with self.assertRaises(AccessError), self.cr.savepoint():
                source.write({'analytic_distribution': {str(other_account.id): 100}})
        self.assertEqual(line.analytic_distribution or {}, original)
        self.assertEqual(entry.project_id, self.project)
        self.assertEqual(entry.wbs_id, self.wbs)
        self.assertEqual(entry.amount, line.balance)

    def test_nonfinite_costs_and_progress_are_rejected_before_posting(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self._feed(amount=value)
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self._feed(quantity=value)
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.env['jakh.project.schedule.progress'].with_user(self.manager).create({
                    'name': 'Non-finite status', 'source_system': 'p6', 'external_id': 'INVALID-NUMBER',
                    'source_reference': 'Invalid source', 'project_id': self.project.id, 'wbs_id': self.wbs.id,
                    'planned_value': value, 'earned_value': 0, 'progress_percent': 0})
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.env['jakh.project.control.entry'].with_user(self.manager)._record(
                    name='Invalid ledger fact', company=self.env.company, project=self.project, wbs=self.wbs,
                    entry_type='actual_cost', amount=value, source_model='project.project', source_id=self.project.id,
                    source_key='NONFINITE-FACT')

    def test_integration_retry_and_payload_identity(self):
        Inbox = self.env['jakh.integration.event'].with_user(self.manager)
        payload = self._payload()
        first = Inbox.ingest(payload); second = Inbox.ingest(payload)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['business_id'], second['business_id'])
        self.assertEqual(first['state'], 'prepared')
        progress = self.env[first['business_model']].browse(first['business_id'])
        self.assertEqual(progress.state, 'submitted', 'Intake must not approve business progress automatically.')
        changed = copy.deepcopy(payload); changed['data']['earned_value'] = 91
        with self.assertRaises(ValidationError), self.cr.savepoint():
            Inbox.ingest(changed)
        with self.assertRaises(AccessError), self.cr.savepoint():
            Inbox.with_user(self.technical).ingest(payload)

        draft_wbs = self.env['jakh.wbs'].with_user(self.manager).create({'name': 'Pending WBS', 'code': 'INT-PENDING',
            'project_id': self.project.id, 'budget_amount': 120})
        retry = copy.deepcopy(payload); retry.update(external_id='P6-PENDING', wbs_id=draft_wbs.id)
        failed = Inbox.ingest(retry)
        self.assertEqual(failed['state'], 'failed')
        draft_wbs.action_release()
        event = Inbox.browse(failed['id']); event.action_retry()
        self.assertEqual(event.state, 'prepared')
        self.assertEqual(event.attempt_count, 2)
        event.action_retry()
        self.assertEqual(event.attempt_count, 2)

    def test_intake_rejects_foreign_scope_and_sensitive_fields(self):
        payload = self._payload()
        payload['data']['api_key'] = 'should-never-be-stored'
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env['jakh.integration.event'].with_user(self.manager).ingest(payload)
        payload = self._payload(); payload['company_id'] = 999999
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env['jakh.integration.event'].with_user(self.manager).ingest(payload)

    def test_resource_capacity_and_actual_feed_handover(self):
        resource = self.env['jakh.site.resource'].with_user(self.manager).create({
            'name': 'Capacity-test crane', 'kind': 'plant', 'source_reference': 'CAPACITY-CRANE',
            'company_id': self.env.company.id, 'cost_journal_id': self.journal.id,
            'expense_account_id': self.cost.id, 'clearing_account_id': self.offset.id,
        })
        start = datetime.combine(fields.Date.today(), datetime.min.time())
        def allocation(name, begin, end, capacity):
            return self.env['jakh.resource.assignment'].with_user(self.manager).create({
                'name': name, 'resource_id': resource.id, 'project_id': self.project.id, 'wbs_id': self.wbs.id,
                'site_reference': 'Site A', 'start_at': start + timedelta(hours=begin),
                'end_at': start + timedelta(hours=end), 'capacity_percent': capacity,
            })
        early = allocation('Early shift', 0, 4, 50); early.action_approve()
        late = allocation('Late shift', 4, 8, 50); late.action_approve()
        shared = allocation('Shared shift', 0, 8, 50); shared.action_approve()
        excess = allocation('Impossible extra crane use', 3, 5, 1)
        with self.assertRaises(UserError), self.cr.savepoint():
            excess.action_approve()
        early.action_cancel()
        with self.assertRaises(UserError), self.cr.savepoint():
            excess.action_approve()  # Its second hour still overlaps the late shift.
        late.action_cancel()
        excess.action_approve(); excess.action_cancel()
        shared.write({'actual_hours': 4, 'actual_cost': 60, 'completion_reference': 'Verified plant return'})
        shared.action_prepare_actual(); shared.action_prepare_actual()
        self.assertEqual(self.env['jakh.project.actual.feed'].search_count([('external_id', '=', 'allocation:%s' % shared.id)]), 1)
        shared.actual_feed_id.action_accept()
        self.assertEqual(shared.actual_feed_id.actual_type, 'plant')
        self.assertEqual(shared.actual_feed_id.account_move_id.state, 'posted')

    def _integration_dimensions(self):
        Location = self.env['jakh.project.location'].with_user(self.manager)
        locations = Location.create([
            {'name': 'Context test building A', 'code': 'INT-CTX-A', 'project_id': self.project.id},
            {'name': 'Context test building B', 'code': 'INT-CTX-B', 'project_id': self.project.id}])
        discipline = self.env['jakh.discipline'].with_user(self.manager).create({'name': 'Context test civil',
            'code': 'INT-CTX-CIV', 'company_id': self.env.company.id})
        income = self.env['account.account'].create({'name': 'Context test income', 'code': 'INT9091',
            'account_type': 'income', 'company_ids': [Command.set(self.env.company.ids)]})
        sales = self.env['account.journal'].create({'name': 'Context test sales', 'code': 'ICTS',
            'type': 'sale', 'company_id': self.env.company.id, 'default_account_id': income.id})
        customer = self.env['res.partner'].create({'name': 'Context test customer'})
        contract = self.env['jakh.project.contract'].with_user(self.manager).create({'name': 'Context test active contract',
            'project_id': self.project.id, 'partner_id': customer.id, 'journal_id': sales.id,
            'income_account_id': income.id, 'line_ids': [Command.create({'code': 'INT-CTX-BOQ',
                'name': 'Same-WBS context test item', 'wbs_id': self.wbs.id, 'quantity': 10, 'unit_price': 100,
                'uom_id': self.env.ref('uom.product_uom_unit').id})]})
        contract.action_activate()
        return contract.line_ids, locations, discipline

    def test_mapped_absent_dimensions_ignore_caller_defaults_and_preserve_exact_replay(self):
        boq, locations, discipline = self._integration_dimensions()
        binding = self.env['jakh.integration.binding'].with_user(self.manager).create({
            'name': 'Explicitly WBS-only P6 mapping', 'kind': 'p6', 'company_id': self.env.company.id,
            'source_project_ref': 'CONTEXT-P6', 'source_wbs_ref': 'CONTEXT-WBS',
            'project_id': self.project.id, 'wbs_id': self.wbs.id})
        binding.action_approve()
        payload = {'version': 1, 'kind': 'p6', 'external_id': 'CONTEXT-P6-001', 'company_id': self.env.company.id,
            'source_project_ref': 'CONTEXT-P6', 'source_wbs_ref': 'CONTEXT-WBS', 'data': self._payload()['data']}
        original = copy.deepcopy(payload)
        Inbox = self.env['jakh.integration.event'].with_user(self.manager)
        injected = Inbox.with_context(default_contract_line_id=boq.id, default_location_id=locations[0].id,
            default_discipline_id=discipline.id, default_forecast_finish=str(fields.Date.today() + timedelta(days=30)),
            default_state='prepared', default_target_id=9999, default_attempt_count=99,
            default_accepted_by=self.manager.id)
        result = injected.ingest_source(payload)
        self.assertEqual(payload, original)
        self.assertEqual(result['state'], 'prepared')
        event = Inbox.browse(result['id'])
        progress = self.env[result['business_model']].with_user(self.manager).browse(result['business_id'])
        self.assertEqual(progress.state, 'submitted')
        self.assertFalse(progress.contract_line_id or progress.location_id or progress.discipline_id)
        self.assertFalse(progress.forecast_finish or progress.accepted_by)
        self.assertEqual(event.attempt_count, 1)
        self.assertFalse(set(json.loads(event.payload)['data']) & {'contract_line_id', 'location_id', 'discipline_id'})
        self.assertEqual(event.payload_hash, hashlib.sha256(event.payload.encode()).hexdigest())
        progress.action_accept()
        facts = self.env['jakh.project.control.entry'].with_user(self.manager).search([
            ('source_model', '=', progress._name), ('source_id', '=', progress.id)])
        self.assertTrue(facts)
        self.assertFalse(facts.contract_line_id or facts.location_id or facts.discipline_id)
        replay = injected.with_context(default_location_id=locations[1].id).ingest_source(payload)
        self.assertEqual(replay['id'], result['id'])
        self.assertEqual(replay['business_id'], progress.id)
        self.assertEqual(payload, original)
        self.assertEqual(self.env[progress._name].search_count([('external_id', '=', payload['external_id'])]), 1)

    def test_direct_labour_and_plant_intake_cannot_inject_correction_or_finance_defaults(self):
        boq, locations, discipline = self._integration_dimensions()
        Inbox = self.env['jakh.integration.event'].with_user(self.manager)
        for kind, actual_type in (('jisr', 'labour'), ('plant', 'plant')):
            with self.subTest(kind=kind):
                accepted = self._feed(external_id='CONTEXT-ORIGINAL-' + kind, source_system=kind, actual_type=actual_type,
                    contract_line_id=boq.id, location_id=locations[0].id, discipline_id=discipline.id)
                accepted.action_submit(); accepted.action_accept()
                before = self.env['jakh.project.control.entry']._sum(self.wbs, ['actual_cost'])
                payload = {'version': 1, 'kind': kind, 'external_id': 'CONTEXT-UNFINANCED-' + kind,
                    'company_id': self.env.company.id, 'project_id': self.project.id, 'wbs_id': self.wbs.id,
                    'data': {'source_reference': 'Verified source cost, not a correction', 'resource_reference': 'Crew or asset',
                        'work_date': str(fields.Date.today()), 'quantity': 2, 'amount': 30}}
                original = copy.deepcopy(payload)
                result = Inbox.with_context(default_contract_line_id=boq.id, default_location_id=locations[0].id,
                    default_discipline_id=discipline.id, default_effect='credit', default_correction_of_id=accepted.id,
                    default_correction_reason='Caller-controlled correction', default_unit_label='days',
                    default_journal_id=self.journal.id, default_expense_account_id=self.cost.id,
                    default_offset_account_id=self.offset.id).ingest(payload)
                self.assertEqual(result['state'], 'prepared')
                feed = self.env[result['business_model']].with_user(self.manager).browse(result['business_id'])
                self.assertEqual(feed.state, 'submitted')
                self.assertEqual(feed.effect, 'debit')
                self.assertEqual(feed.unit_label, 'hours')
                self.assertEqual(feed.actual_type, actual_type)
                self.assertFalse(feed.correction_of_id or feed.correction_reason)
                self.assertFalse(feed.contract_line_id or feed.location_id or feed.discipline_id)
                self.assertFalse(feed.journal_id or feed.expense_account_id or feed.offset_account_id or feed.account_move_id)
                with self.assertRaises(UserError), self.cr.savepoint():
                    feed.action_accept()
                self.assertEqual(self.env['jakh.project.control.entry']._sum(self.wbs, ['actual_cost']), before)
                self.assertFalse(self.env['account.move'].search([('jakh_actual_feed_id', '=', feed.id)]))
                self.assertEqual(Inbox.ingest(payload)['id'], result['id'])
                self.assertEqual(payload, original)

    def test_payload_fields_remain_authoritative_and_context_access_identity_is_preserved(self):
        boq, locations, discipline = self._integration_dimensions()
        Inbox = self.env['jakh.integration.event'].with_user(self.manager)
        injected = Inbox.with_context(lang='en_US', tz='Asia/Dubai', allowed_company_ids=self.env.company.ids,
            tracking_disable=True, default_location_id=locations[1].id, default_effect='credit',
            default_journal_id=False, default_unit_label='days', default_correction_reason='Not source evidence')
        clean = _without_form_defaults(injected)
        self.assertEqual(clean.env.uid, self.manager.id)
        self.assertFalse(clean.env.su)
        for key in ('lang', 'tz', 'allowed_company_ids', 'tracking_disable'):
            self.assertEqual(clean.env.context[key], injected.env.context[key])
        self.assertFalse(any(key.startswith('default_') for key in clean.env.context))
        payload = {'version': 1, 'kind': 'jisr', 'external_id': 'CONTEXT-EXPLICIT-PAYLOAD',
            'company_id': self.env.company.id, 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'data': {'source_reference': 'Explicitly configured source cost', 'resource_reference': 'Crew A',
                'work_date': str(fields.Date.today()), 'quantity': 2, 'amount': 30, 'unit_label': 'source hours',
                'effect': 'debit', 'journal_id': self.journal.id, 'expense_account_id': self.cost.id,
                'offset_account_id': self.offset.id, 'contract_line_id': boq.id,
                'location_id': locations[0].id, 'discipline_id': discipline.id}}
        result = injected.ingest(payload)
        self.assertEqual(result['state'], 'prepared')
        feed = self.env[result['business_model']].with_user(self.manager).browse(result['business_id'])
        self.assertEqual(feed.location_id, locations[0])
        self.assertEqual(feed.effect, 'debit')
        self.assertEqual(feed.unit_label, 'source hours')
        self.assertFalse(feed.correction_reason)
        self.assertEqual(feed.create_uid, self.manager)
        feed.action_accept()
        fact = self.env['jakh.project.control.entry'].search([('source_model', '=', feed._name), ('source_id', '=', feed.id)])
        self.assertEqual(len(fact), 1)
        self.assertEqual(fact.amount, 30)
        self.assertEqual(fact.location_id, locations[0])
        self.assertEqual(fact.contract_line_id, boq)

    def test_failed_retry_ignores_new_caller_defaults_without_changing_stored_payload(self):
        _, locations, _ = self._integration_dimensions()
        pending = self.env['jakh.wbs'].with_user(self.manager).create({'name': 'Context retry WBS', 'code': 'INT-CTX-PENDING',
            'project_id': self.project.id, 'budget_amount': 120})
        payload = self._payload(); payload.update(external_id='CONTEXT-RETRY-001', wbs_id=pending.id)
        Inbox = self.env['jakh.integration.event'].with_user(self.manager)
        failed = Inbox.ingest(payload)
        self.assertEqual(failed['state'], 'failed')
        event = Inbox.browse(failed['id'])
        before_payload, before_hash = event.payload, event.payload_hash
        pending.action_release()
        event.with_context(default_location_id=locations[0].id,
            default_forecast_finish=str(fields.Date.today() + timedelta(days=30))).action_retry()
        self.assertEqual(event.state, 'prepared')
        self.assertEqual(event.attempt_count, 2)
        self.assertEqual(event.payload, before_payload)
        self.assertEqual(event.payload_hash, before_hash)
        progress = self.env[event.target_model].with_user(self.manager).browse(event.target_id)
        self.assertFalse(progress.location_id or progress.forecast_finish)
        self.assertEqual(progress.state, 'submitted')
