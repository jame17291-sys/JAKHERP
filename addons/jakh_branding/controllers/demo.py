"""Read-only walkthrough for an explicitly enabled, retained synthetic dataset."""
import json

from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request


DEMO_SCENARIO = 'jakh-construction-demo-v1'
SYNTHETIC_COMPANIES = {'a': 'JAKH SYNTHETIC Customer A — SA', 'b': 'JAKH SYNTHETIC Customer B — AE'}
DEMO_ROLES = ('group_technical', 'group_quality', 'group_estimator',
              'group_project_manager', 'group_procurement', 'group_finance', 'group_manager')

# The manifest supplies identities, never executable URLs, labels or model names.
# A fixed allowlist also prevents a damaged manifest from becoming a record browser.
RECORD_MODELS = {
    'opportunity': 'crm.lead', 'estimate': 'jakh.estimate', 'sale_order': 'sale.order',
    'draft_estimate': 'jakh.estimate', 'draft_boq_intake': 'jakh.boq.import.batch',
    'project': 'project.project', 'contract': 'jakh.project.contract', 'wbs_structure': 'jakh.wbs',
    'building_budget': 'jakh.boq.location.budget', 'material_demand': 'jakh.material.request',
    'material_pr': 'jakh.purchase.request', 'material_po': 'purchase.order',
    'material_receipt': 'stock.picking', 'material_issue': 'stock.picking',
    'material_return': 'jakh.material.return', 'material_bill': 'account.move',
    'subcontract_pr': 'jakh.purchase.request', 'subcontract_po': 'purchase.order',
    'subcontract_agreement': 'jakh.subcontract.finance.agreement',
    'service_entry': 'jakh.subcontract.service.entry',
    'subcontract_certificate': 'jakh.subcontract.payment.certificate', 'subcontract_bill': 'account.move',
    'labour_actual': 'jakh.project.actual.feed', 'plant_assignment': 'jakh.resource.assignment',
    'p6_progress': 'jakh.project.schedule.progress', 'forecast': 'jakh.project.forecast',
    'owner_certificate': 'jakh.payment.certificate', 'owner_invoice': 'account.move',
    'owner_payment': 'account.payment', 'document': 'jakh.document',
    'document_revision': 'jakh.document.revision', 'assurance': 'jakh.assurance.case',
    'cvr': 'jakh.project.control.report', 'construction_report': 'jakh.construction.report',
    'cash_report': 'jakh.project.cashflow.report', 'financial_report': 'jakh.financial.report',
    'report_schedule': 'jakh.report.schedule', 'report_delivery': 'jakh.report.delivery',
    'threshold_policy': 'jakh.project.threshold.policy',
    'threshold_alert': 'jakh.project.threshold.alert',
    'pending_purchase_request': 'jakh.purchase.request', 'pending_certificate': 'jakh.payment.certificate',
    'pending_document_revision': 'jakh.document.revision', 'pending_assurance': 'jakh.assurance.case',
    'pending_service_entry': 'jakh.subcontract.service.entry',
    'closed_project': 'project.project', 'closed_contract': 'jakh.project.contract',
    'closed_final_account': 'jakh.project.final.account',
    **{f'villa_{number:02}': 'jakh.project.location' for number in range(1, 12)},
}
NON_FINANCIAL_KEYS = {'project', 'wbs_structure', 'document', 'document_revision', 'assurance',
                      'pending_document_revision', 'pending_assurance', 'closed_project'} | {
                          f'villa_{number:02}' for number in range(1, 12)}
REPORT_ACTIONS = {
    'cvr': ('jakh_operations.action_project_control_report', ('group_manager', 'group_project_manager', 'group_finance')),
    'construction_report': ('jakh_operations.action_construction_reports', ('group_manager', 'group_project_manager', 'group_finance')),
    'cash_report': ('jakh_operations.action_cashflow_report', ('group_manager', 'group_project_manager', 'group_finance')),
    'financial_report': ('jakh_operations.action_financial_report', ('group_manager', 'group_finance')),
}


def _demo_scope(env, company_cookie=None):
    """Return a validated manifest and a caller-owned, single-company environment.

    The native company cookie only selects scope. It never grants membership.
    The stored opt-in and database binding are deliberately absent in production.
    """
    if not env.user.has_group('base.group_user') or not any(
            env.user.has_group('jakh_core.' + role) for role in DEMO_ROLES):
        return None
    parameters = env['ir.config_parameter'].sudo()
    if not parameters.get_bool('jakh.demo.enabled', False):
        return None
    raw = parameters.get_str('jakh.demo.manifest', '')
    if not raw or len(raw) > 524288:
        return None
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(manifest, dict) or any((
            type(manifest.get('schema')) is not int, manifest.get('schema') != 1,
            manifest.get('scenario') != DEMO_SCENARIO,
            manifest.get('synthetic') is not True, manifest.get('status') != 'complete',
            manifest.get('database') != env.cr.dbname, not isinstance(manifest.get('records'), dict))):
        return None
    company_id = manifest.get('company_id')
    if type(company_id) is not int or company_id not in env.user.company_ids.ids:
        return None
    if company_cookie:
        try:
            selected = [int(value) for value in company_cookie.split('-')]
        except (AttributeError, TypeError, ValueError):
            return None
    else:
        selected = env.companies.ids
    # Avoid displaying a retained demo while another company is selected.
    if selected != [company_id]:
        return None
    scoped_env = env(context=dict(env.context, allowed_company_ids=[company_id]))
    expected_name = SYNTHETIC_COMPANIES.get(manifest.get('customer'))
    if not expected_name or scoped_env.company.name != expected_name:
        return None
    return manifest, scoped_env


def demo_available(env, company_cookie=None):
    return bool(_demo_scope(env, company_cookie))


def _record_link(env, manifest, key, label):
    model = RECORD_MODELS.get(key)
    reference = manifest['records'].get(key)
    if not model or not isinstance(reference, dict) or reference.get('model') != model:
        return None
    record_id = reference.get('id')
    if type(record_id) is not int or record_id <= 0 or model not in env:
        return None
    if key not in NON_FINANCIAL_KEYS and not env.user.has_group('jakh_core.group_financial'):
        return None
    record = env[model].browse(record_id)
    try:
        # Never sudo a business record, its company, name, state or link.
        record.check_access('read')
        if not record.exists() or 'company_id' not in record._fields or record.company_id.id != manifest['company_id']:
            return None
    except (AccessError, MissingError):
        return None
    return {'label': label, 'url': f'/jakh/{model}/{record_id}'}


def _fresh_report_link(env, manifest, key):
    """Open a fixed native criteria action when a transient snapshot is gone.

    No record is created and no manifest URL or action identifier is executed.
    Native Generate remains the user's explicit next step in the native form.
    """
    specification = REPORT_ACTIONS.get(key)
    if not specification or not env.user.has_group('jakh_core.group_financial'):
        return None
    xmlid, roles = specification
    if not any(env.user.has_group('jakh_core.' + role) for role in roles):
        return None
    if not _record_link(env, manifest, 'project', ''):
        return None
    model = RECORD_MODELS[key]
    if model not in env or not env[model].has_access('read') or not env[model].has_access('create'):
        return None
    action = env.ref(xmlid, raise_if_not_found=False)
    if not action or action._name != 'ir.actions.act_window':
        return None
    # Native action loading elevates metadata, never its business records.
    metadata = action.sudo()
    if metadata.res_model != model or metadata.res_id:
        return None
    if metadata.group_ids and not (metadata.group_ids & env.user.all_group_ids):
        return None
    labels = {
        'cvr': env._('New CVR snapshot'),
        'construction_report': env._('New BOQ and building snapshot'),
        'cash_report': env._('New project cash snapshot'),
        'financial_report': env._('New posted ledger snapshot'),
    }
    return {'label': labels[key], 'url': '/jakh/action-' + xmlid}


def _stage_catalog(env):
    _ = env._

    # Named constructors keep native gettext tokenization away from machine keys.
    def stage(key, title, instructions, expected, references):
        return key, title, instructions, expected, references

    def link(key, label):
        return key, label

    return [
        stage('bid', _('1. Opportunity and estimate'),
         _('Open the opportunity, then the estimate. Review the BOQ, resource build-up and approval history. Use the quotation link to follow the award.'),
         _('The retained estimate and quotation show how an approved bid becomes the awarded project; the history is already completed.'),
         [link('opportunity', _('Opportunity')), link('estimate', _('Estimate')), link('sale_order', _('Awarded quotation'))]),
        stage('baseline', _('2. Project, WBS and buildings'),
         _('Open the project, WBS and Villa 01. Review the eleven building locations. Financial users can open the contract and approved building allocation.'),
         _('The active project links to its contract, released WBS and building allocation. Each cost can be traced to the same project, WBS and BOQ.'),
         [link('project', _('Active project')), link('wbs_structure', _('Released WBS')), link('villa_01', _('Villa 01')), link('contract', _('Contract')), link('building_budget', _('Building allocation'))]),
        stage('materials', _('3. Material demand, purchasing and stock'),
         _('Open site demand, then the requisition, purchase order and receipt. Follow the issue and return records to review quantities and their source links.'),
         _('The retained chain connects site demand to purchasing, received stock, project consumption and a material return. The vendor bill is a separate accounting record.'),
         [link('material_demand', _('Site demand')), link('material_pr', _('Material requisition')), link('material_po', _('Material purchase order')), link('material_receipt', _('Receipt')), link('material_issue', _('Material issue')), link('material_return', _('Material return')), link('material_bill', _('Material vendor bill'))]),
        stage('subcontract', _('4. Subcontract work and billing'),
         _('Open the subcontract order and finance agreement, then the service entry, supplier certificate and vendor bill. Review measured work and the linked source documents.'),
         _('Approved subcontract work is connected to its purchasing and finance records. Review advance recovery, retention and the bill basis in the native forms.'),
         [link('subcontract_pr', _('Subcontract requisition')), link('subcontract_po', _('Subcontract order')), link('subcontract_agreement', _('Subcontract finance agreement')), link('service_entry', _('Service entry')), link('subcontract_certificate', _('Supplier certificate')), link('subcontract_bill', _('Subcontract vendor bill'))]),
        stage('execution', _('5. Labour, plant, progress and forecast'),
         _('Open the labour actual and plant allocation. Then inspect the schedule progress and approved forecast, including their source references and dates.'),
         _('The demo connects recorded labour and plant actuals to project controls. Schedule progress is a synthetic imported snapshot, not a live Primavera connection.'),
         [link('labour_actual', _('Labour actual')), link('plant_assignment', _('Plant allocation')), link('p6_progress', _('Schedule progress')), link('forecast', _('Approved forecast'))]),
        stage('client', _('6. Client certificate, invoice and payment'),
         _('Open the client certificate and follow its invoice. Inspect the linked payment in Finance and review the invoice payment status.'),
         _('The retained client billing chain shows certified work, posted invoicing and a recorded synthetic payment. It does not connect to a bank.'),
         [link('owner_certificate', _('Client certificate')), link('owner_invoice', _('Client invoice')), link('owner_payment', _('Client payment'))]),
        stage('assurance', _('7. Documents, Quality and HSE'),
         _('Open the controlled document and revision, then the Quality / HSE case. Inspect the revision history, responsibility and recorded evidence.'),
         _('Document control and assurance records retain their project links and review history. Visibility follows the same permissions as the native modules.'),
         [link('document', _('Controlled document')), link('document_revision', _('Document revision')), link('assurance', _('Quality / HSE case'))]),
        stage('reports', _('8. Reports and management review'),
         _('Open each available snapshot and review its company, currency, date and source basis. A New snapshot button opens native report criteria: select the active demo project, review the dates and explicitly Generate the report. For the posted ledger, select the demo company.'),
         _('CVR, BOQ / building control, cash forecast and posted ledger reports use different bases. A synthetic result is not production or statutory acceptance evidence.'),
         [link('cvr', _('CVR')), link('construction_report', _('BOQ and building report')), link('cash_report', _('Project cash report')), link('financial_report', _('Posted ledger report')), link('report_delivery', _('Saved report workbook')), link('threshold_policy', _('Threshold policy')), link('threshold_alert', _('Threshold alert')), link('report_schedule', _('Report schedule'))]),
        stage('practice', _('9. Try the pending work'),
         _('Open the draft estimate to practise editing without changing the awarded estimate. For approvals, open My Approvals in the Modules column or a pending record below; review it and use the native action for your assigned role.'),
         _('The draft study and pending requisition, service entry, client certificate, document revision and assurance work are practice items. Your changes persist and dashboard counts may change.'),
         [link('draft_estimate', _('Editable estimate study')), link('draft_boq_intake', _('Draft BOQ intake')), link('pending_purchase_request', _('Pending requisition')), link('pending_service_entry', _('Pending service entry')), link('pending_certificate', _('Pending client certificate')), link('pending_document_revision', _('Pending document revision')), link('pending_assurance', _('Open assurance action'))]),
        stage('closeout', _('10. Review a completed project'),
         _('Open the separate completed project and its contract or final account when available. Review the completed history; keep the active project open for practice.'),
         _('The completed reference illustrates closeout without closing the active demonstration project.'),
         [link('closed_project', _('Completed reference project')), link('closed_contract', _('Completed reference contract')), link('closed_final_account', _('Final account'))]),
    ]


def demo_context(env, company_cookie=None):
    scope = _demo_scope(env, company_cookie)
    if not scope:
        return None
    manifest, scoped_env = scope
    stages = []
    for key, title, instructions, expected, references in _stage_catalog(scoped_env):
        links = [link for reference, label in references
                 if (link := (_record_link(scoped_env, manifest, reference, label)
                              or _fresh_report_link(scoped_env, manifest, reference)))]
        if links:
            stages.append({'key': key, 'title': title, 'instructions': instructions,
                           'expected': expected, 'links': links})
    return {'demo_stages': stages}


class JakhDemo(http.Controller):
    @http.route('/jakh/demo', type='http', auth='user', methods=['GET'], readonly=True)
    def demo_page(self):
        context = demo_context(request.env, request.httprequest.cookies.get('cids'))
        if context is None:
            raise request.not_found()
        response = request.render('jakh_branding.demo_page', context)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
