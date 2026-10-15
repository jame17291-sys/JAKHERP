from urllib.parse import parse_qs, urlsplit

from lxml import html

from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..controllers.routes import canonical_backend_url


@tagged('post_install', '-at_install', 'jakh')
class JakhRouteTests(TransactionCase):
    def test_only_native_backend_paths_are_aliased(self):
        cases = {
            '/odoo': '/jakh',
            '/odoo/sales': '/jakh/sales',
            '/odoo/settings?debug=1#tab': '/jakh/settings?debug=1#tab',
            '/odoo/action-604/11?view_type=form': '/jakh/action-604/11?view_type=form',
            '/odoology/sales': '/odoology/sales',
            '/web/dataset/call_kw': '/web/dataset/call_kw',
            '//other.example/odoo/sales': '//other.example/odoo/sales',
            'https://other.example/odoo/sales': 'https://other.example/odoo/sales',
            '': '',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(canonical_backend_url(source), expected)


@tagged('post_install', '-at_install', 'jakh')
class JakhRouteHttpTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.authenticate(None, None)

    def test_legacy_deep_links_redirect_to_same_origin_alias(self):
        for path in ('/odoo', '/odoo/sales', '/odoo/settings', '/odoo/action-604/11'):
            with self.subTest(path=path):
                response = self.url_open(path + '?view_type=list&debug=1', allow_redirects=False)
                self.assertEqual(response.status_code, 303)
                destination = urlsplit(response.headers['Location'])
                self.assertEqual(destination.netloc, '')
                self.assertEqual(destination.path, '/jakh' + path[5:])
                self.assertEqual(parse_qs(destination.query), {'view_type': ['list'], 'debug': ['1']})

    def test_canonical_deep_links_require_native_authentication(self):
        response = self.url_open('/jakh/sales?view_type=list', allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        destination = urlsplit(response.headers['Location'])
        self.assertEqual(destination.path, '/web/login')
        self.assertEqual(parse_qs(destination.query)['redirect'], ['/jakh/sales?view_type=list'])

    def test_internal_bootstrap_keeps_security_headers_and_session(self):
        user = new_test_user(self.env, 'jakh_route_internal', groups='base.group_user')
        self.authenticate(user.login, user.login)
        for path in ('/jakh/sales', '/jakh/settings', '/jakh/action-604/11?view_type=form'):
            with self.subTest(path=path):
                response = self.url_open(path, allow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn('browser_cache_secret', response.text)
                self.assertIn('JAKH', response.text)

    def test_first_reload_uses_native_assets_for_new_language_in_both_directions(self):
        self.env['res.lang']._activate_lang('ar_001')
        user = new_test_user(self.env, 'jakh_route_language', groups='base.group_user', lang='en_US')
        self.authenticate(user.login, user.login)

        def backend_stylesheet():
            response = self.url_open('/jakh', allow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            document = html.fromstring(response.content)
            stylesheets = document.xpath("//link[@rel='stylesheet' and @media='screen']/@href")
            backend = [href for href in stylesheets if 'web.assets_web' in href]
            self.assertEqual(len(backend), 1, stylesheets)
            return backend[0]

        self.assertNotIn('.rtl.', backend_stylesheet())
        # Reproduce changing the native preference while the HTTP session still
        # contains the prior language. Exactly one GET must select the new CSS.
        user.write({'lang': 'ar_001'})
        self.assertIn('.rtl.', backend_stylesheet())
        user.write({'lang': 'en_US'})
        self.assertNotIn('.rtl.', backend_stylesheet())

    def test_external_user_still_cannot_enter_internal_backend(self):
        user = new_test_user(self.env, 'jakh_route_portal', groups='base.group_portal')
        self.authenticate(user.login, user.login)
        response = self.url_open('/jakh/sales', allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        # The installed portal controller redirects external users to /my.
        # This is the native external-user landing page, not ERP bootstrap.
        self.assertEqual(urlsplit(response.headers['Location']).path, '/my')
        self.assertNotIn('browser_cache_secret', response.text)

    def test_manifest_and_worker_use_product_routes_without_losing_logout_guard(self):
        response = self.url_open('/web/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest['scope'], '/jakh')
        self.assertEqual(manifest['start_url'], '/jakh')
        self.assertTrue(all(item['url'].startswith('/jakh') for item in manifest.get('shortcuts', [])))
        if manifest.get('share_target'):
            self.assertEqual(manifest['share_target']['action'], '/jakh?share_target=trigger')
        worker = self.url_open('/jakh/service-worker.js')
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.headers['Service-Worker-Allowed'], '/jakh')
        self.assertIn('const homepageURL = "/jakh";', worker.text)
        self.assertIn('jakh-sw-cache-v3', worker.text)
        self.assertIn('user_logout', worker.text)
        self.assertIn('sessionInfo = null;', worker.text)
        self.assertIn('@@@session_info_secret@@@', worker.text)
        offline = self.url_open('/jakh/offline')
        self.assertEqual(offline.status_code, 200)
        self.assertIn('JAKH', offline.text)
