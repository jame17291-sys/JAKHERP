from decimal import Decimal, InvalidOperation

from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError


class Estimate(models.Model):
    _inherit = 'jakh.estimate'
    commercial_contract_id = fields.Many2one('jakh.project.contract', compute='_compute_commercial_contract', groups='jakh_core.group_financial')

    def _compute_commercial_contract(self):
        for row in self:
            row.commercial_contract_id = self.env['jakh.project.contract'].search([('jakh_estimate_id', '=', row.id)], limit=1)

    def action_handover(self):
        result = super().action_handover()
        self.ensure_one()
        # The parent's row lock and approval check cover this whole transaction.
        existing = self.env['jakh.project.contract'].search([('jakh_estimate_id', '=', self.id)], limit=1)
        if not existing:
            line_map = {line.line_uuid: line for line in self.line_ids}
            wbs_map = {row.jakh_estimate_line_id.id: row for row in self.project_id.jakh_wbs_ids}
            lines = []
            for item in self.approved_version_id.snapshot_json['lines']:
                if item.get('is_group'):
                    continue
                line = line_map[item['id']]
                lines.append(Command.create({
                    'code': item['code'], 'name': item['description'],
                    'quantity': float(item['total_quantity']), 'unit_price': float(item['selling_unit_rate']),
                    'uom_id': line.uom_id.id, 'wbs_id': wbs_map[line.id].id,
                    'source_line_uuid': item['id'], 'original_direct_unit_cost': float(item['unit_cost']),
                    'original_cost_total': float(item['cost_total']), 'original_budget_total': float(item.get('budget_total', item['cost_total'])),
                }))
            contract = self.env['jakh.project.contract']._create_estimate_handover({
                'name': self.title, 'project_id': self.project_id.id,
                'partner_id': self.partner_id.id, 'sale_order_id': self.sale_order_id.id,
                'jakh_estimate_id': self.id, 'line_ids': lines,
            })
            self.env['jakh.audit.event']._append(self.company_id, 'commercial.estimate_handover', contract, {'estimate_id': self.id, 'version_id': self.approved_version_id.id})
        return result

    def action_open_commercial_contract(self):
        self.ensure_one()
        self._check_manager()
        if not self.commercial_contract_id:
            raise ValidationError(self.env._('Hand over the approved estimate first.'))
        return {'type': 'ir.actions.act_window', 'res_model': 'jakh.project.contract', 'res_id': self.commercial_contract_id.id, 'view_mode': 'form', 'views': [(False, 'form')], 'target': 'current'}


class Contract(models.Model):
    _inherit = 'jakh.project.contract'
    jakh_estimate_id = fields.Many2one('jakh.estimate', readonly=True, copy=False, check_company=True)
    _estimate_unique = models.Constraint('UNIQUE(jakh_estimate_id)', 'An approved study creates only one commercial contract.')

    @api.model_create_multi
    def create(self, values):
        if any(row.get('jakh_estimate_id') for row in values):
            raise AccessError(self.env._('Create estimate-linked contracts through approved handover.'))
        return super().create(values)

    def _create_estimate_handover(self, values):
        if not self.env.user.has_group('jakh_core.group_manager'):
            raise AccessError(self.env._('A construction manager must hand over the approved study.'))
        return super().create(values)

    def write(self, values):
        if 'jakh_estimate_id' in values or (self.filtered('jakh_estimate_id') and set(values) & {'project_id', 'partner_id', 'sale_order_id'}):
            raise AccessError(self.env._('Approved estimate and commercial identity links are immutable.'))
        return super().write(values)

    def action_activate(self):
        for row in self.filtered('jakh_estimate_id'):
            approved = {line['code']: line for line in row.jakh_estimate_id.approved_version_id.snapshot_json['lines'] if not line.get('is_group')}
            if set(approved) != set(row.line_ids.mapped('code')) or len(approved) != len(row.line_ids):
                raise ValidationError(self.env._('Commercial BOQ must match the approved estimate.'))
            for line in row.line_ids:
                original = approved[line.code]
                if Decimal(str(line.quantity)) != Decimal(str(original['total_quantity'])) or Decimal(str(line.unit_price)) != Decimal(str(original['selling_unit_rate'])):
                    raise ValidationError(self.env._('Commercial quantities and rates must match the approved study; changes require a controlled revision.'))
                if line.source_line_uuid != original['id'] or any(Decimal(str(line[name])) != Decimal(str(value)) for name, value in {
                    'original_direct_unit_cost': original['unit_cost'], 'original_cost_total': original['cost_total'],
                    'original_budget_total': original.get('budget_total', original['cost_total']),
                }.items()):
                    raise ValidationError(self.env._('Award direct cost, cost budget and BOQ identity must match the frozen approved study.'))
        return super().action_activate()


class ContractLine(models.Model):
    _inherit = 'jakh.project.contract.line'

    _frozen_award_fields = {'source_line_uuid', 'original_direct_unit_cost', 'original_cost_total', 'original_budget_total'}

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            contract = self.env['jakh.project.contract'].browse(values.get('contract_id'))
            if not contract.jakh_estimate_id:
                continue
            approved = {item['id']: item for item in contract.jakh_estimate_id.approved_version_id.snapshot_json['lines'] if not item.get('is_group')}
            item = approved.get(values.get('source_line_uuid'))
            if not item or values.get('code') != item['code']:
                raise ValidationError(self.env._('Source-linked contract items must preserve an approved BOQ identity.'))
            for name, expected in {
                'original_direct_unit_cost': item['unit_cost'], 'original_cost_total': item['cost_total'],
                'original_budget_total': item.get('budget_total', item['cost_total']),
                'quantity': item['total_quantity'], 'unit_price': item['selling_unit_rate'],
            }.items():
                raw = values.get(name)
                try:
                    valid = not isinstance(raw, bool) and Decimal(str(raw)) == Decimal(str(expected))
                except (InvalidOperation, ValueError, TypeError):
                    valid = False
                if not valid:
                    raise ValidationError(self.env._('Source-linked contract prices and award costs must come from the frozen approved study.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & self._frozen_award_fields and self.filtered('contract_id.jakh_estimate_id'):
            raise AccessError(self.env._('Frozen award costs and source identities cannot be edited, including before activation.'))
        return super().write(values)
