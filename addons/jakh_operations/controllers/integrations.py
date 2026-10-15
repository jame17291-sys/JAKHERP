from odoo import http
from odoo.http import request


class ConstructionIntegration(http.Controller):
    @http.route('/jakh/api/v1/feeds', auth='bearer', bearer_scope='rpc', type='json2', methods=['POST'], save_session=False)
    def intake(self, payload):
        return request.env['jakh.integration.event'].ingest(payload)

    @http.route('/jakh/api/v1/source-feeds', auth='bearer', bearer_scope='rpc', type='json2', methods=['POST'], save_session=False)
    def source_intake(self, payload):
        return request.env['jakh.integration.event'].ingest_source(payload)

    @http.route('/jakh/api/v1/feeds/<int:event_id>', auth='bearer', bearer_scope='rpc', type='json2', methods=['POST'], save_session=False, readonly=True)
    def status(self, event_id):
        event = request.env['jakh.integration.event'].browse(event_id).exists()
        if not event:
            raise request.not_found()
        return event._result()
