from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestSourceMapping(TransactionCase):
    def test_reviewed_source_mapping_preserves_business_approval_and_retry_identity(self):
        manager = new_test_user(self.env, login='source.mapping.manager', groups='jakh_core.group_manager')
        technical = new_test_user(self.env, login='source.mapping.technical', groups='jakh_core.group_technical')
        project = self.env['project.project'].with_user(manager).create({'name': 'Synthetic P6 mapping project', 'company_id': self.env.company.id})
        wbs = self.env['jakh.wbs'].with_user(manager).create({'name': 'Mapped scope', 'code': 'MAP-01', 'project_id': project.id, 'budget_amount': 100})
        wbs.action_release()
        location = self.env['jakh.project.location'].with_user(manager).create({'name': 'Mapped building', 'code': 'MAP-A', 'project_id': project.id})
        binding = self.env['jakh.integration.binding'].with_user(manager).create({'name': 'Reviewed P6 reference', 'kind': 'p6',
            'source_project_ref': 'P6-PROJECT', 'source_wbs_ref': 'P6-WBS', 'project_id': project.id,
            'wbs_id': wbs.id, 'location_id': location.id, 'company_id': self.env.company.id})
        payload = {'version': 1, 'kind': 'p6', 'company_id': self.env.company.id, 'external_id': 'P6-STATUS-001',
            'source_project_ref': 'P6-PROJECT', 'source_wbs_ref': 'P6-WBS',
            'data': {'source_reference': 'Approved P6 data date', 'as_of_date': str(fields.Date.today()),
                'planned_value': 100, 'earned_value': 60, 'progress_percent': 60}}
        inbox = self.env['jakh.integration.event'].with_user(manager)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            inbox.ingest_source(payload)
        binding.action_approve()
        first = inbox.ingest_source(payload)
        self.assertEqual(first['id'], inbox.ingest_source(payload)['id'])
        progress = self.env[first['business_model']].with_user(manager).browse(first['business_id'])
        self.assertEqual(progress.location_id, location)
        self.assertEqual(progress.state, 'submitted')
        progress.action_accept()
        facts = self.env['jakh.project.control.entry'].with_user(manager).search([('source_model', '=', progress._name), ('source_id', '=', progress.id)])
        self.assertEqual(facts.location_id, location)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            inbox.ingest_source({**payload, 'data': {**payload['data'], 'location_id': location.id}})
        with self.assertRaises(AccessError), self.cr.savepoint():
            inbox.with_user(technical).ingest_source(payload)
        with self.assertRaises(AccessError), self.cr.savepoint():
            binding.write({'location_id': False})
        binding.action_retire()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            inbox.ingest_source({**payload, 'external_id': 'P6-STATUS-002'})
        next_mapping = self.env[binding._name].with_user(manager).browse(binding.action_new_revision()['res_id'])
        self.assertEqual(next_mapping.revision, 2)
        self.assertEqual(next_mapping.state, 'draft')
        next_mapping.action_approve()
        second = inbox.ingest_source({**payload, 'external_id': 'P6-STATUS-002'})
        self.assertNotEqual(second['id'], first['id'])
        with self.assertRaises(AccessError), self.cr.savepoint():
            next_mapping.write({'revision': 99})
