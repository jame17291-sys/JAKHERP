import json
import math

from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


class CrmLead(models.Model):
    _inherit = 'crm.lead'
    _jakh_source_unique = models.Constraint(
        'UNIQUE(company_id, jakh_source_system, jakh_source_id)',
        'An original CRM source can be imported only once per legal entity.',
    )


class CrmImport(models.TransientModel):
    _name = 'jakh.crm.import'
    _description = 'CRM Data Import'

    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    upload = fields.Binary(required=True, attachment=False)
    filename = fields.Char()
    source_system = fields.Char(required=True, default='JAKH CRM')
    reviewed = fields.Boolean(string='I reviewed the source and confirmed it contains real customer CRM data, not illustrative financial balances')
    result = fields.Text(readonly=True)

    def _rows(self):
        self.ensure_one()
        if not self.env.user.has_group('jakh_core.group_manager') or self.company_id not in self.env.companies:
            raise AccessError(self.env._('An authorized construction manager must import into an allowed company.'))
        if not self.upload or self.upload.size > 20 * 1024 * 1024:
            raise ValidationError(self.env._('Select a JSON file no larger than 20 MiB.'))
        try:
            source = json.loads(self.upload.content.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise ValidationError(self.env._('The source must be valid UTF-8 JSON.')) from exc
        if isinstance(source, dict) and 'records' in source:
            if not isinstance(source['records'], dict):
                raise ValidationError(self.env._('The workspace records must be a JSON object.'))
            rows = source['records'].get('crm/pipeline', [])
        elif isinstance(source, dict):
            rows = source.get('opportunities', [])
        else:
            rows = source
        if not isinstance(rows, list) or not rows or len(rows) > 20000:
            raise ValidationError(self.env._('Provide between 1 and 20,000 CRM opportunity records.'))
        result, ids = [], set()
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValidationError(self.env._('Every opportunity must be a JSON object.'))
            source_id = str(raw.get('id', '')).strip()
            title = str(raw.get('title') or raw.get('name') or '').strip()
            if not source_id or len(source_id) > 160 or not title or len(title) > 300 or source_id in ids:
                raise ValidationError(self.env._('Each opportunity requires a unique source ID and a title.'))
            ids.add(source_id)
            amount = raw.get('value', raw.get('amount'))
            currency = None
            if amount is not None:
                if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
                    raise ValidationError(self.env._('Original amounts must be finite non-negative decimal numbers.'))
                try:
                    amount = float(amount)
                except ValueError as exc:
                    raise ValidationError(self.env._('Original amounts must be numeric.')) from exc
                if not math.isfinite(amount) or amount < 0:
                    raise ValidationError(self.env._('Original amounts cannot be negative or non-finite.'))
                code = raw.get('currency')
                if not isinstance(code, str) or len(code) != 3:
                    raise ValidationError(self.env._('An original amount requires an explicit three-letter currency.'))
                currency = self.env['res.currency'].with_context(active_test=False).search([('name', '=', code.upper())], limit=1)
                if not currency:
                    raise ValidationError(self.env._('The original currency is not available in the ERP.'))
            result.append((source_id, title, amount, currency, raw))
        return result

    def action_preview(self):
        rows = self._rows()
        self.result = self.env._('%s valid opportunities. Original fields and currencies will be retained. No contacts, balances, posted journals or user credentials are imported.', len(rows))
        return self._reopen()

    def action_import(self):
        rows = self._rows()
        if not self.reviewed:
            raise ValidationError(self.env._('Review and confirm the source before importing.'))
        created, skipped = 0, 0
        for source_id, title, amount, currency, raw in rows:
            existing = self.env['crm.lead'].search([
                ('company_id', '=', self.company_id.id), ('jakh_source_system', '=', self.source_system),
                ('jakh_source_id', '=', source_id),
            ], limit=1)
            if existing:
                skipped += 1
                continue
            self.env['crm.lead'].create({
                'name': title, 'type': 'opportunity', 'company_id': self.company_id.id,
                'jakh_source_system': self.source_system, 'jakh_source_id': source_id,
                'jakh_source_amount': amount or 0, 'jakh_source_currency_id': currency.id if currency else False,
                'jakh_source_payload': raw,
                'expected_revenue': amount if currency == self.company_id.currency_id and amount is not None else 0,
            })
            created += 1
        self.result = self.env._('%s opportunities imported; %s existing sources skipped. Foreign amounts were not relabelled as company currency.', created, skipped)
        self.env['jakh.audit.event']._append(self.company_id, 'crm.legacy_import', self, {'created': created, 'skipped': skipped, 'source': self.source_system})
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'new'}
