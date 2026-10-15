from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.webmanifest import WebManifest

from ..brand_assets import icon_png
from .demo import demo_available


class JakhBranding(http.Controller):
    @http.route('/jakh/brand/icon/<int:size>.png', type='http', auth='public', methods=['GET'], readonly=True)
    def brand_icon(self, size, **kwargs):
        if size not in (32, 180, 192, 512):
            raise request.not_found()
        return request.make_response(icon_png(size), headers=[
            ('Content-Type', 'image/png'), ('Cache-Control', 'public, max-age=86400'),
            ('X-Content-Type-Options', 'nosniff')])

    @http.route('/jakh/help', type='http', auth='user', methods=['GET'], readonly=True)
    def help_page(self):
        response = request.render('jakh_branding.help_page', {
            'demo_available': demo_available(request.env, request.httprequest.cookies.get('cids')),
        })
        response.headers['Cache-Control'] = 'no-store'
        return response


class JakhWebManifest(WebManifest):
    def _get_webmanifest(self):
        result = super()._get_webmanifest()
        result.update(name='JAKH ERP', short_name='JAKH', theme_color='#172e39', background_color='#f6f8f7')
        result['icons'] = [
            {'src': f'/jakh/brand/icon/{size}.png?v=2', 'sizes': f'{size}x{size}', 'type': 'image/png', 'purpose': 'any maskable'}
            for size in (192, 512)]
        return result

    def _get_service_worker_content(self):
        # Version the generated shell cache without modifying upstream source or
        # its session/logout protections. An older cached offline page must not
        # retain a previous product identity after an application update.
        body = super()._get_service_worker_content()
        marker = 'const cacheName = "odoo-sw-cache";'
        if body.count(marker) != 1:
            raise ValueError('Service-worker compatibility requires review for this build')
        return body.replace(marker, 'const cacheName = "jakh-sw-cache-v2";')

    @http.route()
    def offline(self):
        return request.render('web.webclient_offline', {})
