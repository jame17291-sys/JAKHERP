"""Exercise native API-key authentication and business scoping over HTTP."""
import json
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged, mute_logger
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install', 'jakh')
class TestConstructionIntegrationHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='http.integration.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='http.integration.technical', groups='jakh_core.group_technical')
        cls.project = cls.env['project.project'].with_user(cls.manager).create({
            'name': 'HTTP synthetic project', 'company_id': cls.env.company.id, 'user_id': cls.manager.id,
        })
        cls.wbs = cls.env['jakh.wbs'].with_user(cls.manager).create({
            'name': 'HTTP approved scope', 'code': 'HTTP-001', 'project_id': cls.project.id, 'budget_amount': 100,
        })
        cls.wbs.action_release()
        cls.keys = {}
        for user in cls.manager | cls.technical:
            cls.keys[user.id] = cls.env['res.users.apikeys'].with_user(user)._generate(
                scope='rpc', name='Disposable rollback-only construction test',
                expiration_date=datetime.now() + timedelta(hours=1))

    def _post(self, path, payload, user=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if user:
            headers['Authorization'] = 'Bearer ' + self.keys[user.id]
        elif token:
            headers['Authorization'] = 'Bearer ' + token
        return self.url_open(path, data=json.dumps(payload), headers=headers)

    def test_authenticated_idempotent_intake_and_denied_access(self):
        self.authenticate(None, None)
        payload = {'version': 1, 'kind': 'p6', 'external_id': 'HTTP-P6-1',
            'company_id': self.env.company.id, 'project_id': self.project.id, 'wbs_id': self.wbs.id,
            'data': {'source_reference': 'Approved P6 data date', 'as_of_date': str(fields.Date.today()),
                     'planned_value': 100, 'earned_value': 75, 'progress_percent': 75}}
        path = '/jakh/api/v1/feeds'
        with mute_logger('odoo.http'):
            self.assertEqual(self._post(path, {'payload': payload}).status_code, 401)
            self.assertEqual(self._post(path, {'payload': payload}, token='invalid-key').status_code, 401)
            self.assertEqual(self._post(path, {'payload': payload}, user=self.technical).status_code, 403)
        first = self._post(path, {'payload': payload}, user=self.manager)
        self.assertEqual(first.status_code, 200, first.text)
        event = first.json()
        repeated = self._post(path, {'payload': payload}, user=self.manager)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()['id'], event['id'])
        self.assertEqual(repeated.json()['business_id'], event['business_id'])
        status = self._post(path + '/' + str(event['id']), {}, user=self.manager)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()['state'], 'prepared')
        with mute_logger('odoo.http'):
            self.assertEqual(self._post(path + '/' + str(event['id']), {}, user=self.technical).status_code, 403)
        progress = self.env[event['business_model']].browse(event['business_id'])
        self.assertEqual(progress.state, 'submitted', 'API preparation cannot approve business progress.')

    def test_source_alias_intake_uses_reviewed_mapping_and_current_role(self):
        self.authenticate(None, None)
        binding = self.env['jakh.integration.binding'].with_user(self.manager).create({'name': 'HTTP P6 mapping',
            'kind': 'p6', 'company_id': self.env.company.id, 'source_project_ref': 'HTTP-P6',
            'source_wbs_ref': 'HTTP-WBS', 'project_id': self.project.id, 'wbs_id': self.wbs.id})
        binding.action_approve()
        payload = {'version': 1, 'kind': 'p6', 'external_id': 'HTTP-ALIAS-1', 'company_id': self.env.company.id,
            'source_project_ref': 'HTTP-P6', 'source_wbs_ref': 'HTTP-WBS',
            'data': {'source_reference': 'Approved export', 'as_of_date': str(fields.Date.today()),
                'planned_value': 100, 'earned_value': 50, 'progress_percent': 50}}
        with mute_logger('odoo.http'):
            self.assertEqual(self._post('/jakh/api/v1/source-feeds', {'payload': payload}).status_code, 401)
            self.assertEqual(self._post('/jakh/api/v1/source-feeds', {'payload': payload}, user=self.technical).status_code, 403)
        accepted = self._post('/jakh/api/v1/source-feeds', {'payload': payload}, user=self.manager)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self._post('/jakh/api/v1/source-feeds', {'payload': payload}, user=self.manager).json()['id'], accepted.json()['id'])
