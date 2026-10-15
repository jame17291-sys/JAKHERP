"""Dependency-free checks of the real guide's scope and link-filtering boundary.

These checks exercise the controller helpers with access-denying records. Native
ORM/HTTP acceptance remains a separate, synthetic-staging verification step.
"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class AccessError(Exception):
    pass


def load_controller():
    odoo = ModuleType('odoo')
    odoo.http = SimpleNamespace(Controller=object, route=lambda *args, **kwargs: lambda method: method)
    exceptions = ModuleType('odoo.exceptions')
    exceptions.AccessError = AccessError
    exceptions.MissingError = type('MissingError', (Exception,), {})
    http = ModuleType('odoo.http')
    http.request = None
    source = ROOT / 'addons/jakh_branding/controllers/demo.py'
    spec = importlib.util.spec_from_file_location('jakh_demo_boundary', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'odoo': odoo, 'odoo.exceptions': exceptions, 'odoo.http': http}):
        spec.loader.exec_module(module)
    return module


DEMO = load_controller()


class Record:
    def __init__(self, company_id=7, allowed=True, exists=True):
        self.company_id = SimpleNamespace(id=company_id)
        self._fields = {'company_id': object()}
        self.allowed = allowed
        self.present = exists
        self.read_checks = []

    def check_access(self, operation):
        self.read_checks.append(operation)
        if not self.allowed:
            raise AccessError('native record restriction')

    def exists(self):
        return self.present


class Environment:
    def __init__(self, manifest=None, enabled=True, financial=True, roles=True,
                 internal=True, companies=None, membership=None):
        self.manifest = manifest if manifest is not None else valid_manifest()
        self.enabled = enabled
        self.groups = {'base.group_user'} if internal else set()
        if roles:
            self.groups.add('jakh_core.group_technical')
        if financial:
            self.groups.add('jakh_core.group_financial')
        self.user = SimpleNamespace(has_group=lambda group: group in self.groups,
                                    company_ids=SimpleNamespace(ids=membership or [7]))
        self.companies = SimpleNamespace(ids=companies or [7])
        self.company = SimpleNamespace(name='JAKH SYNTHETIC Customer A — SA')
        self.context = {'lang': 'en_US'}
        self.cr = SimpleNamespace(dbname='synthetic_demo')
        self.records = {('project.project', 11): Record(), ('account.move', 22): Record()}
        self.native_access = {}
        self.native_actions = {}
        self.user.all_group_ids = set()
        self.lookups = []
        self.selected_context = None

    def __call__(self, *, context):
        self.selected_context = context
        return self

    def __contains__(self, model):
        return model in self.native_access or any(key[0] == model for key in self.records)

    def __getitem__(self, model):
        if model == 'ir.config_parameter':
            return SimpleNamespace(sudo=lambda: SimpleNamespace(
                get_bool=lambda key, default: self.enabled,
                get_str=lambda key, default: json.dumps(self.manifest) if not isinstance(self.manifest, str) else self.manifest))
        return SimpleNamespace(browse=lambda record_id: self.browse(model, record_id),
                               has_access=lambda operation: self.native_access.get(model, {}).get(operation, False))

    def ref(self, xmlid, raise_if_not_found=False):
        return self.native_actions.get(xmlid)

    def browse(self, model, record_id):
        self.lookups.append((model, record_id))
        return self.records[(model, record_id)]

    def _(self, text):
        return text


def valid_manifest():
    return {'schema': 1, 'scenario': DEMO.DEMO_SCENARIO if 'DEMO' in globals() else 'jakh-construction-demo-v1',
            'synthetic': True, 'status': 'complete', 'database': 'synthetic_demo', 'company_id': 7, 'customer': 'a',
            'records': {'project': {'model': 'project.project', 'id': 11,
                        'label': 'PRIVATE MANIFEST LABEL', 'url': 'https://untrusted.example/'},
                        'owner_invoice': {'model': 'account.move', 'id': 22}},
            'actors': {'owner': {'login': 'PRIVATE ACTOR LOGIN'}}}


class DemoScopeTests(unittest.TestCase):
    def test_production_and_unprepared_tenants_have_no_guide(self):
        self.assertIsNone(DEMO.demo_context(Environment(enabled=False)))
        for field, value in [('synthetic', False), ('status', 'preparing'), ('database', 'other_db'),
                             ('scenario', 'another-scenario'), ('schema', 99), ('schema', True),
                             ('customer', 'production'), ('company_id', 8), ('records', [])]:
            with self.subTest(field=field):
                manifest = valid_manifest()
                manifest[field] = value
                self.assertIsNone(DEMO.demo_context(Environment(manifest)))
        self.assertIsNone(DEMO.demo_context(Environment(manifest='{broken json')))
        env = Environment()
        env.company.name = 'Production company'
        self.assertIsNone(DEMO.demo_context(env))

    def test_internal_jakh_role_and_single_selected_company_are_required(self):
        for env in (Environment(internal=False), Environment(roles=False), Environment(companies=[8]),
                    Environment(companies=[7, 8]), Environment(membership=[8])):
            self.assertIsNone(DEMO.demo_context(env))
        for cookie in ('8', '7-8', '7-invalid', '7--8'):
            self.assertIsNone(DEMO.demo_context(Environment(), cookie))
        env = Environment()
        self.assertIsNotNone(DEMO.demo_context(env, '7'))
        self.assertEqual(env.selected_context['allowed_company_ids'], [7])

    def test_business_record_read_restriction_is_respected(self):
        env = Environment()
        env.records[('project.project', 11)].allowed = False
        self.assertIsNone(DEMO._record_link(env, env.manifest, 'project', 'Project'))
        self.assertEqual(env.records[('project.project', 11)].read_checks, ['read'])

    def test_cross_company_and_deleted_records_do_not_receive_links(self):
        for record in (Record(company_id=8), Record(exists=False)):
            env = Environment()
            env.records[('project.project', 11)] = record
            self.assertIsNone(DEMO._record_link(env, env.manifest, 'project', 'Project'))

    def test_financial_links_require_financial_group_before_browse(self):
        env = Environment(financial=False)
        self.assertIsNone(DEMO._record_link(env, env.manifest, 'owner_invoice', 'Invoice'))
        self.assertEqual(env.lookups, [])
        self.assertIsNotNone(DEMO._record_link(env, env.manifest, 'project', 'Project'))

    def test_manifest_is_not_a_model_url_or_label_authority(self):
        env = Environment()
        self.assertEqual(DEMO._record_link(env, env.manifest, 'project', 'Project'),
                         {'label': 'Project', 'url': '/jakh/project.project/11'})
        context = json.dumps(DEMO.demo_context(env))
        self.assertNotIn('PRIVATE', context)
        self.assertNotIn('untrusted.example', context)
        env.manifest['records']['project']['model'] = 'res.users'
        self.assertIsNone(DEMO._record_link(env, env.manifest, 'project', 'Project'))
        env.manifest['records']['project'] = {'model': 'project.project', 'id': True}
        self.assertIsNone(DEMO._record_link(env, env.manifest, 'project', 'Project'))

    def report_environment(self, key='cvr'):
        env = Environment()
        env.groups.add('jakh_core.group_project_manager')
        model = DEMO.RECORD_MODELS[key]
        env.native_access[model] = {'read': True, 'create': True}
        xmlid = DEMO.REPORT_ACTIONS[key][0]
        action = SimpleNamespace(_name='ir.actions.act_window', res_model=model, res_id=False, group_ids=set())
        action.sudo = lambda: action
        env.native_actions[xmlid] = action
        return env, action

    def test_expired_report_uses_trusted_native_action_without_creating_a_record(self):
        env, _action = self.report_environment()
        env.manifest['records']['cvr'] = {'model': 'jakh.project.control.report', 'id': 33,
                                          'url': 'https://untrusted.example/action'}
        env.records[('jakh.project.control.report', 33)] = Record(exists=False)
        reports = next(stage for stage in DEMO.demo_context(env)['demo_stages'] if stage['key'] == 'reports')
        self.assertIn({'label': 'New CVR snapshot',
                       'url': '/jakh/action-jakh_operations.action_project_control_report'}, reports['links'])
        self.assertNotIn('untrusted.example', json.dumps(reports))

    def test_report_fallback_respects_roles_native_acl_action_groups_and_project_access(self):
        env, action = self.report_environment()
        self.assertIsNotNone(DEMO._fresh_report_link(env, env.manifest, 'cvr'))
        env.native_access['jakh.project.control.report']['create'] = False
        self.assertIsNone(DEMO._fresh_report_link(env, env.manifest, 'cvr'))
        env.native_access['jakh.project.control.report']['create'] = True
        action.res_model = 'res.users'
        self.assertIsNone(DEMO._fresh_report_link(env, env.manifest, 'cvr'))
        action.res_model = 'jakh.project.control.report'
        action.group_ids = {99}
        self.assertIsNone(DEMO._fresh_report_link(env, env.manifest, 'cvr'))
        action.group_ids = set()
        env.records[('project.project', 11)].allowed = False
        self.assertIsNone(DEMO._fresh_report_link(env, env.manifest, 'cvr'))
        env, _action = self.report_environment('financial_report')
        self.assertIsNone(DEMO._fresh_report_link(env, env.manifest, 'financial_report'),
                          'Project manager role alone does not grant posted ledger report access')
        env.groups.add('jakh_core.group_finance')
        self.assertIsNotNone(DEMO._fresh_report_link(env, env.manifest, 'financial_report'))

    def test_fixed_report_actions_match_their_declared_native_models(self):
        actions = {}
        for file in (ROOT / 'addons/jakh_operations/views').glob('*.xml'):
            for action in ET.parse(file).getroot().findall("record[@model='ir.actions.act_window']"):
                model = action.find("field[@name='res_model']")
                if model is not None:
                    actions['jakh_operations.' + action.get('id')] = model.text
        for key, (xmlid, _roles) in DEMO.REPORT_ACTIONS.items():
            self.assertEqual(actions[xmlid], DEMO.RECORD_MODELS[key])

    def test_native_help_and_portable_guide_keep_retention_boundaries(self):
        templates = ET.parse(ROOT / 'addons/jakh_branding/views/branding_templates.xml').getroot()
        guide = templates.find("template[@id='demo_page']")
        text = ' '.join(' '.join(guide.itertext()).split())
        self.assertIn('There is no automatic deletion or reset.', text)
        self.assertIn('matched database and filestore backup', text)
        self.assertIn('All scenario data is synthetic.', text)
        links = guide.findall('.//a')
        self.assertTrue(any(node.get('rel') == 'noopener noreferrer' for node in links))
        portable = (ROOT / 'deploy/demo-walkthrough.html').read_text()
        self.assertIn('data-content="ar"', portable)
        self.assertIn('source alone does not undo database changes', portable)
        self.assertNotIn('PRIVATE MANIFEST LABEL', portable)


if __name__ == '__main__':
    unittest.main()
