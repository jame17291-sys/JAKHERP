"""Exercise the actual route helper without mocking framework authentication."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ROUTES = Path(__file__).resolve().parents[1] / 'addons/jakh_branding/controllers/routes.py'
tree = ast.parse(ROUTES.read_text())
helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == '_sync_bootstrap_language')
helper_module = ast.Module(body=[helper], type_ignores=[])


class TestBootstrapLanguage(unittest.TestCase):
    def sync(self, *, before='en_US', after='ar_001', session_uid=17, environment_uid=17,
             payload_uid=17, template='web.webclient_bootstrap'):
        original_context = {'lang': before, 'tz': 'Asia/Dubai', 'allowed_company_ids': [3, 7],
                            'host_id': 5}
        request = SimpleNamespace(session=SimpleNamespace(uid=session_uid),
                                  env=SimpleNamespace(uid=environment_uid, context=original_context.copy()))
        updates = []

        def update_context(**values):
            updates.append(values)
            request.env.context = dict(request.env.context, **values)

        request.update_context = update_context
        response = SimpleNamespace(template=template, qcontext={'session_info': {
            'uid': payload_uid, 'user_context': {'lang': after},
        }})
        namespace = {'request': request}
        exec(compile(helper_module, str(ROUTES), 'exec'), namespace)
        returned = namespace['_sync_bootstrap_language'](response)
        self.assertIs(returned, response)
        self.assertEqual({key: value for key, value in request.env.context.items() if key != 'lang'},
                         {key: value for key, value in original_context.items() if key != 'lang'})
        self.assertEqual(request.env.uid, environment_uid)
        self.assertEqual(request.session.uid, session_uid)
        return request.env.context['lang'], updates

    def test_first_arabic_render_uses_refreshed_native_language(self):
        self.assertEqual(self.sync(), ('ar_001', [{'lang': 'ar_001'}]))

    def test_first_english_render_returns_to_native_ltr_language(self):
        self.assertEqual(self.sync(before='ar_001', after='en_US'), ('en_US', [{'lang': 'en_US'}]))

    def test_current_language_needs_no_context_update(self):
        self.assertEqual(self.sync(before='ar_001'), ('ar_001', []))

    def test_login_redirects_and_public_pages_are_not_modified(self):
        for template in (None, 'web.login', 'portal.portal_layout'):
            with self.subTest(template=template):
                self.assertEqual(self.sync(template=template), ('en_US', []))

    def test_only_authenticated_matching_identity_can_refresh_context(self):
        for values in ({'session_uid': None}, {'environment_uid': 1}, {'payload_uid': 18}):
            with self.subTest(values=values):
                self.assertEqual(self.sync(**values), ('en_US', []))

    def test_missing_language_does_not_replace_existing_context(self):
        self.assertEqual(self.sync(after=None), ('en_US', []))


if __name__ == '__main__':
    unittest.main()
