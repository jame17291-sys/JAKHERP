"""Role-controlled certificate review and frozen quantity evidence.

Reviewing a measurement never grants access to prices or permission to post.
Only the final finance action invokes the existing certificate calculations.
Returned submissions retain immutable review evidence before draft correction.
"""
import json

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from .commercial import PaymentCertificate as OriginalCertificate
from .commercial import MeasurementLine as OriginalMeasurement
from .project_control import CertificateVariationLine as OriginalVariationMeasurement
from .procurement import lock_records, require_role


REVIEW_POLICIES = [
    ('finance_only', 'Finance approval'),
    ('technical_finance', 'Technical review → finance approval'),
    ('technical_commercial_finance', 'Technical review → commercial review → finance approval'),
]
REVIEW_ROUTES = {
    'finance_only': ['finance'],
    'technical_finance': ['technical', 'finance'],
    'technical_commercial_finance': ['technical', 'commercial', 'finance'],
}
REVIEW_STAGES = [('technical', 'Technical review'), ('commercial', 'Commercial review'),
                 ('finance', 'Finance approval'), ('returned', 'Returned for correction'),
                 ('completed', 'Approved')]
QUANTITY_SNAPSHOTS = {'previous_quantity', 'current_quantity', 'remaining_quantity',
                      'approved_contract_quantity', 'measurement_frozen'}


class ContractCertificateReview(models.Model):
    _inherit = 'jakh.project.contract'

    certificate_review_policy = fields.Selection(REVIEW_POLICIES, required=True, default='finance_only',
        string='Certificate approval route', help='Choose the review stages before activating the contract. The approved route cannot be changed silently.')

    def action_activate(self):
        if any(contract.certificate_review_policy not in REVIEW_ROUTES for contract in self):
            raise ValidationError(_('Choose a supported certificate approval route.'))
        return super().action_activate()


class CertificateReview(models.Model):
    _name = 'jakh.certificate.review'
    _description = 'Immutable certificate review evidence'
    _order = 'reviewed_at desc, id desc'
    _check_company_auto = True

    certificate_id = fields.Many2one('jakh.payment.certificate', required=True, readonly=True,
                                    ondelete='restrict', check_company=True)
    company_id = fields.Many2one(related='certificate_id.company_id', store=True)
    project_id = fields.Many2one(related='certificate_id.contract_id.project_id', store=True)
    cycle = fields.Integer(required=True, readonly=True)
    stage = fields.Selection(REVIEW_STAGES, required=True, readonly=True)
    decision = fields.Selection([('submitted', 'Submitted'), ('accepted', 'Review accepted'),
                                 ('returned', 'Returned for correction'), ('approved', 'Finance approved')],
                                required=True, readonly=True)
    reviewer_id = fields.Many2one('res.users', required=True, readonly=True, ondelete='restrict')
    reviewed_at = fields.Datetime(required=True, readonly=True)
    note = fields.Text(readonly=True, help='Operational review reason. Do not enter payroll details or confidential pricing here.')
    measurement_snapshot = fields.Text(required=True, readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_('Review evidence is recorded by the certificate workflow only.'))

    def write(self, values):
        raise AccessError(_('Certificate review history is immutable.'))

    def unlink(self):
        raise AccessError(_('Certificate review history cannot be deleted.'))

    @api.model
    def _append_review(self, certificate, stage, decision, note=''):
        certificate.check_access('write')
        values = {
            'certificate_id': certificate.id, 'cycle': certificate.review_cycle,
            'stage': stage, 'decision': decision, 'reviewer_id': self.env.uid,
            'reviewed_at': fields.Datetime.now(), 'note': note or '',
            'measurement_snapshot': json.dumps({
                'base': [{'item': line.contract_line_id.id, 'cumulative_quantity': line.cumulative_quantity}
                         for line in certificate.line_ids],
                'variations': [{'item': line.variation_id.id, 'cumulative_quantity': line.cumulative_quantity}
                               for line in certificate.variation_line_ids],
            }, sort_keys=True),
        }
        self.check_access('create')
        return super(CertificateReview, self).create(values)


class CertificateCompletion(models.Model):
    _inherit = 'jakh.payment.certificate'

    review_stage = fields.Selection(REVIEW_STAGES, readonly=True, copy=False, string='Next review stage')
    review_cycle = fields.Integer(readonly=True, copy=False, string='Submission cycle')
    review_route_snapshot = fields.Text(readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    review_note = fields.Text(copy=False, string='Review / return reason',
        help='Required when returning a certificate. Operational comments only; financial fields remain restricted.')
    return_reason = fields.Text(readonly=True, copy=False, string='Last return reason')
    review_ids = fields.One2many('jakh.certificate.review', 'certificate_id', readonly=True, string='Review history')

    _server_fields = OriginalCertificate._server_fields | {
        'review_stage', 'review_cycle', 'review_route_snapshot', 'submitted_at', 'approved_at',
        'return_reason', 'review_ids',
    }

    def write(self, values):
        if set(values) == {'review_note'} and self and all(row.state == 'submitted' for row in self):
            for row in self:
                lock_records(row)
                row._require_review_role()
            # A submitted quantity remains immutable. Only the current reviewer's
            # operational comment is editable before their controlled decision.
            return super(OriginalCertificate, self).write(values)
        return super().write(values)

    def unlink(self):
        if self.filtered('review_ids'):
            raise UserError(_('A submitted certificate retains its review history even after return. Create a replacement instead of deleting it.'))
        return super().unlink()

    def _require_review_role(self):
        self.ensure_one()
        self.check_access('write')
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select the certificate legal entity before reviewing.'))
        self.contract_id.project_id._jakh_assert_open()
        roles = {
            'technical': ['group_manager', 'group_technical'],
            'commercial': ['group_manager', 'group_project_manager'],
            'finance': ['group_manager', 'group_finance'],
        }
        if self.state != 'submitted' or self.review_stage not in roles:
            raise UserError(_('Only a submitted certificate at its current review stage can be reviewed.'))
        require_role(self.env, roles[self.review_stage])

    def _review_event(self, decision, note=''):
        self.ensure_one()
        self.env['jakh.certificate.review']._append_review(self, self.review_stage, decision, note)

    def action_submit(self):
        for certificate in self:
            lock_records(certificate)
            certificate.contract_id.project_id._jakh_assert_open()
            route = REVIEW_ROUTES.get(certificate.contract_id.certificate_review_policy)
            if not route:
                raise ValidationError(_('Choose a supported certificate approval route.'))
            if certificate.variation_line_ids.filtered('variation_id.boq_scope_omission'):
                raise UserError(_('Unperformed scope omissions reduce the BOQ quantity; they are not a second invoice deduction.'))
            super(CertificateCompletion, certificate).action_submit()
            certificate._transition(review_stage=route[0], review_cycle=certificate.review_cycle + 1,
                review_route_snapshot=json.dumps(route), submitted_at=fields.Datetime.now())
            certificate._review_event('submitted')
        return True

    def action_review_approve(self):
        for certificate in self:
            lock_records(certificate)
            certificate._require_review_role()
            if certificate.review_stage == 'finance':
                certificate.action_approve()
                continue
            route = json.loads(certificate.review_route_snapshot or '[]')
            if certificate.review_stage not in route or route[-1:] != ['finance']:
                raise UserError(_('The saved review route is invalid; return this measurement for correction.'))
            stage = certificate.review_stage
            next_stage = route[route.index(stage) + 1]
            certificate._review_event('accepted', certificate.review_note)
            certificate._transition(review_stage=next_stage)
            super(OriginalCertificate, certificate).write({'review_note': False})
        return True

    def action_return(self):
        for certificate in self:
            lock_records(certificate)
            certificate._require_review_role()
            reason = (certificate.review_note or '').strip()
            if not reason:
                raise UserError(_('Enter a reason before returning the certificate for correction.'))
            certificate._review_event('returned', reason)
            certificate._transition(state='draft', review_stage='returned', return_reason=reason)
            super(OriginalCertificate, certificate).write({'review_note': False})
        return True

    def action_reject(self):
        """A rejected submission returns to draft without erasing that submission."""
        return self.action_return()

    def action_approve(self):
        for certificate in self:
            lock_records(certificate)
            if certificate.state in ('approved', 'invoiced'):
                require_role(self.env, ['group_manager', 'group_finance'])
                continue
            # A legacy submitted measurement uses the original finance-only
            # route; new submissions always carry an explicit frozen route.
            if not certificate.review_stage and not certificate.review_route_snapshot:
                if certificate.contract_id.certificate_review_policy != 'finance_only':
                    raise UserError(_('Submit the configured certificate review route before finance approval.'))
                certificate._transition(review_stage='finance', review_cycle=max(1, certificate.review_cycle))
            certificate._require_review_role()
            if certificate.review_stage != 'finance':
                raise UserError(_('Complete technical and commercial reviews before finance approval.'))
            previous = self.search([('contract_id', '=', certificate.contract_id.id),
                ('state', 'in', ['approved', 'invoiced']), ('id', '!=', certificate.id)],
                order='approval_sequence desc', limit=1)
            prior_base = {line.contract_line_id.id: line.cumulative_quantity for line in previous.line_ids}
            prior_variations = {line.variation_id.id: line.cumulative_quantity for line in previous.variation_line_ids}
            super(CertificateCompletion, certificate).action_approve()
            for line in certificate.line_ids:
                quantity = line.contract_line_id._jakh_measurement_limit(certificate.date)
                old = prior_base.get(line.contract_line_id.id, 0)
                super(OriginalMeasurement, line).write({
                    'previous_quantity': old, 'current_quantity': line.cumulative_quantity - old,
                    'remaining_quantity': quantity - line.cumulative_quantity,
                    'approved_contract_quantity': quantity, 'measurement_frozen': True,
                })
            for line in certificate.variation_line_ids:
                old = prior_variations.get(line.variation_id.id, 0)
                super(OriginalVariationMeasurement, line).write({
                    'previous_quantity': old, 'current_quantity': line.cumulative_quantity - old,
                    'remaining_quantity': line.variation_id.quantity - line.cumulative_quantity,
                    'approved_contract_quantity': line.variation_id.quantity, 'measurement_frozen': True,
                })
            # The financial calculation is approved already, but the review log
            # records the final stage before its completion marker is assigned.
            certificate._review_event('approved', certificate.review_note)
            certificate._transition(approved_at=fields.Datetime.now(), review_stage='completed')
        return True


class CertificateQuantitySnapshot(models.Model):
    _inherit = 'jakh.payment.certificate.line'

    previous_quantity = fields.Float(readonly=True, digits=(16, 6), string='Previously certified quantity')
    current_quantity = fields.Float(readonly=True, digits=(16, 6), string='Current period quantity')
    remaining_quantity = fields.Float(readonly=True, digits=(16, 6), string='Remaining quantity')
    approved_contract_quantity = fields.Float(readonly=True, digits=(16, 6), string='Approved scope quantity')
    measurement_frozen = fields.Boolean(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if any(set(values) & QUANTITY_SNAPSHOTS for values in values_list):
            raise AccessError(_('Quantity evidence is frozen by certificate approval only.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & QUANTITY_SNAPSHOTS:
            raise AccessError(_('Approved quantity evidence cannot be changed.'))
        return super().write(values)


class CertificateVariationQuantitySnapshot(models.Model):
    _inherit = 'jakh.payment.certificate.variation.line'

    previous_quantity = fields.Float(readonly=True, digits=(16, 6), string='Previously certified quantity')
    current_quantity = fields.Float(readonly=True, digits=(16, 6), string='Current period quantity')
    remaining_quantity = fields.Float(readonly=True, digits=(16, 6), string='Remaining quantity')
    approved_contract_quantity = fields.Float(readonly=True, digits=(16, 6), string='Approved scope quantity')
    measurement_frozen = fields.Boolean(readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if any(set(values) & QUANTITY_SNAPSHOTS for values in values_list):
            raise AccessError(_('Quantity evidence is frozen by certificate approval only.'))
        return super().create(values_list)

    def write(self, values):
        if set(values) & QUANTITY_SNAPSHOTS:
            raise AccessError(_('Approved quantity evidence cannot be changed.'))
        return super().write(values)

    @api.constrains('variation_id')
    def _check_scope_omission(self):
        if self.filtered('variation_id.boq_scope_omission'):
            raise ValidationError(_('A BOQ scope omission cannot also be billed as a negative measurement.'))
