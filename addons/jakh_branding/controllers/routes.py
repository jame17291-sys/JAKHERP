"""Product-facing web-client aliases; native authentication and APIs stay intact."""

from urllib.parse import urlsplit, urlunsplit

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.home import Home

from .branding import JakhWebManifest


def canonical_backend_url(value):
    """Alias only a local native backend path, never a host or API URL."""
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return value
    if parsed.path == '/odoo' or parsed.path.startswith('/odoo/'):
        return urlunsplit(parsed._replace(path='/jakh' + parsed.path[5:]))
    return value


def _sync_bootstrap_language(response):
    """Use the native refreshed user language for this lazy backend render.

    The HTTP environment starts with the incoming session context. Native
    session_info refreshes that session from the signed-in user's preferences,
    but the existing environment can still have the previous language. QWeb
    selects RTL assets from that environment, not from the JavaScript payload.
    Synchronize only the trusted bootstrap language after native authentication;
    never read a language/UID from URL parameters or replace company context.
    """
    if getattr(response, 'template', None) != 'web.webclient_bootstrap':
        return response
    session_info = (response.qcontext or {}).get('session_info') or {}
    uid = request.session.uid
    if not uid or request.env.uid != uid or session_info.get('uid') != uid:
        return response
    lang = (session_info.get('user_context') or {}).get('lang')
    # Native res.users.context_get() validates the language against installed
    # locales and provides its fallback; do not invent a second language policy.
    if lang and lang != request.env.context.get('lang'):
        request.update_context(lang=lang)
    return response


class JakhHome(Home):
    @http.route()
    def index(self, *args, **kwargs):
        response = super().index(*args, **kwargs)
        if response.headers.get('Location'):
            response.headers['Location'] = canonical_backend_url(response.headers['Location'])
        return response

    def _login_redirect(self, uid, redirect=None):
        # Keep the native helper: partially authenticated/MFA sessions must
        # still go to identity verification, with a canonical return path.
        return canonical_backend_url(super()._login_redirect(
            uid, redirect=canonical_backend_url(redirect)))

    @http.route([
        '/web', '/odoo', '/odoo/<path:subpath>', '/scoped_app/<path:subpath>',
        '/jakh', '/jakh/<path:subpath>',
    ])
    def web_client(self, s_action=None, **kwargs):
        path = request.httprequest.path
        if path == '/odoo' or path.startswith('/odoo/'):
            # A fixed same-origin path alias; preserve encoded query parameters.
            # The destination then uses the original ensure_db/session checks.
            return request.redirect(canonical_backend_url(request.httprequest.full_path), 303)
        response = super().web_client(s_action=s_action, **kwargs)
        return _sync_bootstrap_language(response)


class JakhRouteManifest(JakhWebManifest):
    def _get_webmanifest(self):
        manifest = super()._get_webmanifest()
        for key in ('scope', 'start_url'):
            manifest[key] = canonical_backend_url(manifest[key])
        for shortcut in manifest.get('shortcuts', []):
            shortcut['url'] = canonical_backend_url(shortcut['url'])
        if manifest.get('share_target'):
            manifest['share_target']['action'] = canonical_backend_url(manifest['share_target']['action'])
        return manifest

    def _get_jakh_service_worker_content(self):
        content = super()._get_service_worker_content()
        replacements = {
            'const cacheName = "jakh-sw-cache-v2";': 'const cacheName = "jakh-sw-cache-v3";',
            'const homepageURL = "/odoo";': 'const homepageURL = "/jakh";',
            'Response.redirect("/odoo?share_target=trigger")': 'Response.redirect("/jakh?share_target=trigger")',
        }
        for old, new in replacements.items():
            if content.count(old) != 1:
                raise ValueError('Backend route service-worker compatibility requires review')
            content = content.replace(old, new)
        # Session extraction and user_logout messages remain native. Renaming
        # their namespaces would break logout and cached-secret protections.
        return content

    @http.route('/jakh/service-worker.js', type='http', auth='public', methods=['GET'], readonly=True)
    def jakh_service_worker(self):
        return request.make_response(self._get_jakh_service_worker_content(), [
            ('Content-Type', 'text/javascript'),
            ('Service-Worker-Allowed', '/jakh'),
            ('Cache-Control', 'no-cache'),
        ])

    @http.route(['/odoo/offline', '/jakh/offline'])
    def offline(self):
        return super().offline()
