"""One BOQ/location identity from requisition through posted project cost."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


def reject_derived_dimensions(env, values_list, names):
    if any(set(values) & set(names) for values in values_list) or any(
            env.context.get('default_' + name) for name in names):
        raise AccessError(_('Derived construction dimensions are assigned by their approved source documents only.'))


def check_dimensions(record, project, wbs=False, contract_line=False, location=False, discipline=False):
    company = project.company_id
    project.check_access('read')
    if not company or company not in record.env.companies:
        raise AccessError(_('Select the matching project legal entity.'))
    for linked in (wbs, contract_line, location, discipline):
        if linked:
            linked.check_access('read')
            if linked.company_id != company:
                raise ValidationError(_('Construction dimensions must belong to the project legal entity.'))
    if wbs and wbs.project_id != project:
        raise ValidationError(_('The WBS must belong to the project.'))
    if contract_line and (contract_line.contract_id.project_id != project or (wbs and contract_line.wbs_id != wbs)):
        raise ValidationError(_('The BOQ item must match the project and WBS.'))
    if location and location.project_id != project:
        raise ValidationError(_('The execution location must belong to the project.'))


class ContractLine(models.Model):
    _inherit = 'jakh.project.contract.line'
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')

    @api.constrains('location_id', 'discipline_id', 'wbs_id', 'contract_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.contract_id.project_id, row.wbs_id, location=row.location_id, discipline=row.discipline_id)


class RequestLine(models.Model):
    _name = 'jakh.purchase.request.line'
    _inherit = ['jakh.purchase.request.line', 'jakh.audit.mixin']
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')
    _jakh_audit_fields = ('name', 'quantity', 'uom_id', 'product_id', 'contract_line_id', 'location_id', 'discipline_id')

    @api.constrains('contract_line_id', 'location_id', 'discipline_id', 'request_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.request_id.project_id, row.request_id.wbs_id,
                row.contract_line_id, row.location_id, row.discipline_id)


class RequestAudit(models.Model):
    _name = 'jakh.purchase.request'
    _inherit = ['jakh.purchase.request', 'jakh.audit.mixin']
    _jakh_audit_fields = ('name', 'required_date', 'material_plan_reference', 'supplier_id', 'attachment_ids')


class RequestCostAudit(models.Model):
    _name = 'jakh.purchase.request.cost'
    _inherit = ['jakh.purchase.request.cost', 'jakh.audit.mixin']
    _jakh_audit_fields = ('unit_price', 'tax_ids')


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'
    jakh_contract_line_id = fields.Many2one(related='jakh_request_line_id.contract_line_id', store=True, readonly=True)
    jakh_location_id = fields.Many2one(related='jakh_request_line_id.location_id', store=True, readonly=True)
    jakh_discipline_id = fields.Many2one(related='jakh_request_line_id.discipline_id', store=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        reject_derived_dimensions(self.env, values_list, ('jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'}:
            raise AccessError(_('Purchase dimensions come from the approved requisition and cannot be reassigned.'))
        return super().write(values)

    def _prepare_account_move_line(self, move=False):
        values = super()._prepare_account_move_line(move=move)
        if self.jakh_request_line_id:
            account = self.jakh_request_line_id.request_id.project_id.account_id
            if account:
                values['analytic_distribution'] = {str(account.id): 100}
            # Perpetual stocked material is an inventory asset at supplier
            # billing. Site consumption, not AP recognition, charges job cost.
            if self.product_id.is_storable and self.product_id.valuation == 'real_time':
                inventory = self.product_id.with_company(self.company_id)._get_product_accounts().get('stock_valuation')
                if not inventory:
                    raise ValidationError(_('Configure the inventory asset account before billing stocked project material.'))
                values['account_id'] = inventory.id
        return values


class ServiceEntry(models.Model):
    _name = 'jakh.subcontract.service.entry'
    _inherit = ['jakh.subcontract.service.entry', 'jakh.audit.mixin']
    contract_line_id = fields.Many2one(related='purchase_line_id.jakh_contract_line_id', store=True, readonly=True)
    location_id = fields.Many2one(related='purchase_line_id.jakh_location_id', store=True, readonly=True)
    discipline_id = fields.Many2one(related='purchase_line_id.jakh_discipline_id', store=True, readonly=True)
    _jakh_audit_fields = ('name', 'quantity', 'notes', 'service_date')

    @api.model_create_multi
    def create(self, values_list):
        reject_derived_dimensions(self.env, values_list, ('contract_line_id', 'location_id', 'discipline_id'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'contract_line_id', 'location_id', 'discipline_id'}:
            raise AccessError(_('Service-entry dimensions come from the approved subcontract order.'))
        return super().write(values)


class ActualFeed(models.Model):
    _inherit = 'jakh.project.actual.feed'
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')

    @api.constrains('contract_line_id', 'location_id', 'discipline_id', 'project_id', 'wbs_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)


class ScheduleProgress(models.Model):
    _inherit = 'jakh.project.schedule.progress'
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')

    @api.constrains('contract_line_id', 'location_id', 'discipline_id', 'project_id', 'wbs_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)


class ResourceAssignment(models.Model):
    _inherit = 'jakh.resource.assignment'
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')

    @api.constrains('contract_line_id', 'location_id', 'discipline_id', 'project_id', 'wbs_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)


class Variation(models.Model):
    _inherit = 'jakh.project.variation'
    contract_line_id = fields.Many2one('jakh.project.contract.line', check_company=True, ondelete='restrict')
    location_id = fields.Many2one('jakh.project.location', check_company=True, ondelete='restrict')
    discipline_id = fields.Many2one('jakh.discipline', check_company=True, ondelete='restrict')

    @api.constrains('contract_line_id', 'location_id', 'discipline_id', 'project_id', 'wbs_id')
    def _check_execution_scope(self):
        for row in self:
            check_dimensions(row, row.project_id, row.wbs_id, row.contract_line_id, row.location_id, row.discipline_id)


class LedgerDimensions(models.Model):
    _inherit = 'jakh.project.control.entry'
    location_id = fields.Many2one('jakh.project.location', readonly=True, check_company=True, ondelete='restrict', index=True)
    discipline_id = fields.Many2one('jakh.discipline', readonly=True, check_company=True, ondelete='restrict', index=True)


class JournalLine(models.Model):
    _inherit = 'account.move.line'
    jakh_contract_line_id = fields.Many2one(related='purchase_line_id.jakh_contract_line_id', store=True, readonly=True)
    jakh_location_id = fields.Many2one(related='purchase_line_id.jakh_location_id', store=True, readonly=True)
    jakh_discipline_id = fields.Many2one(related='purchase_line_id.jakh_discipline_id', store=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        reject_derived_dimensions(self.env, values_list, ('jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'}:
            raise AccessError(_('Journal dimensions come from their source documents and cannot be reassigned.'))
        return super().write(values)


class StockMove(models.Model):
    _inherit = 'stock.move'
    jakh_contract_line_id = fields.Many2one(related='purchase_line_id.jakh_contract_line_id', store=True, readonly=True)
    jakh_location_id = fields.Many2one(related='purchase_line_id.jakh_location_id', store=True, readonly=True)
    jakh_discipline_id = fields.Many2one(related='purchase_line_id.jakh_discipline_id', store=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        reject_derived_dimensions(self.env, values_list, ('jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & {'jakh_contract_line_id', 'jakh_location_id', 'jakh_discipline_id'}:
            raise AccessError(_('Stock dimensions come from their approved source documents.'))
        return super().write(values)
