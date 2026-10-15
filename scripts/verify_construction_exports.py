"""Native English/Arabic export QA in stopped, marked synthetic staging only.

Creates one explicitly synthetic scenario under the enrolled owner's normal
permissions. All fixture/report/accounting rows are ALWAYS rolled back. A
private copied filestore keeps company logos readable without modifying the
source filestore. No SMTP, HTTP service, migration or production acceptance.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import signal
import smtplib
import socket
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile

import native_staging
from native_recovery_drill import (Deadline, RecoveryRejected, _connect, copy_tree_new,
                                  inventory_tree, private_path, require_quiet_databases,
                                  require_quiet_http, validate_customer, verify_cluster)
from runtime_config import secret_file, write_private

QA_MARKER = 'jakh-native-construction-export-qa-v1'
MAX_EXPORT_BYTES = 50 * 1024 * 1024


def _private_binary(path, payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_EXPORT_BYTES:
        raise RecoveryRejected('The native export is empty or exceeds the bounded QA size.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(payload)
    return {'file': Path(path).name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}


@contextmanager
def _offline_python():
    """No SMTP or external Python connections; native Chromium CDP is local."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def loopback_only(stream, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise RecoveryRejected('External network connections are not permitted during native export QA.')
        return connect(stream, address)
    def loopback_only_ex(stream, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise RecoveryRejected('External network connections are not permitted during native export QA.')
        return connect_ex(stream, address)
    def no_smtp(*args, **kwargs):
        raise RecoveryRejected('SMTP is disabled during rollback-only native export QA.')
    with patch.object(socket.socket, 'connect', loopback_only), patch.object(socket.socket, 'connect_ex', loopback_only_ex), \
            patch.object(smtplib.SMTP, 'connect', no_smtp):
        yield


def _scenario(env, token):
    """Business actions, never direct ledger facts or forged posted states."""
    from odoo import Command, fields
    today = fields.Date.today()
    company = env.company
    prefix = 'QA' + token[:5].upper()
    def account(suffix, name, kind, reconcile=False):
        return env['account.account'].create({'code': prefix + suffix, 'name': name,
            'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(company.ids)]})
    receivable = account('01', 'SYNTHETIC QA receivable / ذمم تجريبية', 'asset_receivable', True)
    income = account('02', 'SYNTHETIC QA measured revenue / إيراد أعمال تجريبية', 'income')
    expense = account('03', 'SYNTHETIC QA labour cost / تكلفة عمالة تجريبية', 'expense_direct_cost')
    clearing = account('04', 'SYNTHETIC QA payroll accrual / استحقاق أجور تجريبي', 'liability_current')
    sales = env['account.journal'].create({'name': 'SYNTHETIC QA sales / مبيعات تجريبية',
        'code': 'QS' + token[:3].upper(), 'type': 'sale', 'company_id': company.id, 'default_account_id': income.id})
    costs = env['account.journal'].create({'name': 'SYNTHETIC QA costs / تكاليف تجريبية',
        'code': 'QG' + token[:3].upper(), 'type': 'general', 'company_id': company.id})
    customer = env['res.partner'].with_company(company).create({'name': 'SYNTHETIC QA client / عميل اختبار',
        'property_account_receivable_id': receivable.id})
    project = env['project.project'].create({'name': 'SYNTHETIC QA two-building project / مشروع مبنيين تجريبي',
        'company_id': company.id, 'user_id': env.uid, 'privacy_visibility': 'employees'})
    project._jakh_prepare_cost_centre()
    code = env['jakh.cost.code'].create({'name': 'SYNTHETIC QA structure / الهيكل التجريبي',
        'code': prefix, 'company_id': company.id, 'resource_type': 'labour'})
    wbs = env['jakh.wbs'].create({'name': 'SYNTHETIC QA structural works / الأعمال الإنشائية التجريبية',
        'code': prefix, 'project_id': project.id, 'cost_code_id': code.id, 'budget_amount': 10000})
    wbs.action_release()
    buildings = env['jakh.project.location'].create([
        {'name': 'SYNTHETIC QA Building A / المبنى أ', 'code': 'A', 'project_id': project.id, 'kind': 'building'},
        {'name': 'SYNTHETIC QA Building B / المبنى ب', 'code': 'B', 'project_id': project.id, 'kind': 'building'}])
    discipline = env['jakh.discipline'].create({'name': 'SYNTHETIC QA Civil / أعمال مدنية تجريبية',
        'code': prefix, 'company_id': company.id})
    contract = env['jakh.project.contract'].create({'name': 'SYNTHETIC QA contract / عقد اختبار',
        'project_id': project.id, 'partner_id': customer.id, 'journal_id': sales.id,
        'income_account_id': income.id, 'retention_percent': 5, 'retention_cap': 900,
        'retention_account_id': account('05', 'SYNTHETIC QA retention / ضمان محتجز تجريبي', 'asset_current').id,
        'certificate_review_policy': 'technical_commercial_finance',
        'line_ids': [Command.create({'code': prefix + '-BOQ', 'name': 'SYNTHETIC QA measured structure / هيكل مقاس تجريبي',
            'wbs_id': wbs.id, 'uom_id': env.ref('uom.product_uom_unit').id, 'quantity': 10, 'unit_price': 1800,
            'source_line_uuid': 'synthetic-export-qa-' + token, 'original_direct_unit_cost': 900,
            'original_cost_total': 9000, 'original_budget_total': 10000, 'discipline_id': discipline.id})]})
    contract.action_activate()
    boq = contract.line_ids
    allocation = env['jakh.boq.location.budget'].create({'contract_line_id': boq.id,
        'reason': 'SYNTHETIC QA approved measured building allocation; not a customer opening balance.',
        'line_ids': [Command.create({'location_id': building.id, 'discipline_id': discipline.id,
            'quantity': quantity, 'budget_amount': budget})
            for building, quantity, budget in zip(buildings, (6, 4), (6000, 4000))]})
    allocation.action_submit(); allocation.action_approve()
    feed = env['jakh.project.actual.feed'].create({'name': 'SYNTHETIC QA approved labour / عمالة معتمدة تجريبية',
        'project_id': project.id, 'wbs_id': wbs.id, 'contract_line_id': boq.id, 'location_id': buildings[0].id,
        'discipline_id': discipline.id, 'source_system': 'verified', 'actual_type': 'labour',
        'external_id': 'synthetic-qa-cost-' + token, 'source_reference': 'SYNTHETIC QA approved source',
        'resource_reference': 'SYNTHETIC QA crew', 'work_date': today, 'quantity': 80, 'amount': 2600,
        'journal_id': costs.id, 'expense_account_id': expense.id, 'offset_account_id': clearing.id})
    feed.action_submit(); feed.action_accept()
    if feed.account_move_id.state != 'posted' or not company.currency_id.is_zero(sum(feed.account_move_id.line_ids.mapped('balance'))):
        raise RecoveryRejected('The synthetic native actual-cost journal was not posted and balanced.')
    progress = env['jakh.project.schedule.progress'].create({'name': 'SYNTHETIC QA P6 status / حالة تقدم تجريبية',
        'project_id': project.id, 'wbs_id': wbs.id, 'source_system': 'p6', 'external_id': 'synthetic-qa-p6-' + token,
        'source_reference': 'SYNTHETIC QA accepted scheduling-master evidence', 'as_of_date': today,
        'planned_value': 5000, 'earned_value': 6000, 'progress_percent': 60, 'forecast_finish': today + timedelta(days=60)})
    progress.action_submit(); progress.action_accept()
    certificate = env['jakh.payment.certificate'].create({'name': 'SYNTHETIC QA unbilled certificate / مستخلص غير مفوتر تجريبي',
        'contract_id': contract.id, 'date': today})
    certificate.action_prepare_lines(); certificate.line_ids.write({'cumulative_quantity': 5})
    certificate.action_submit()
    for _stage in range(3):
        if certificate.review_stage == 'finance':
            break
        certificate.action_review_approve()
    certificate.action_approve()
    if certificate.state != 'approved' or certificate.invoice_id:
        raise RecoveryRejected('The synthetic certificate must be approved and remain unbilled.')
    cash = env['jakh.project.cash.forecast'].create({'name': 'SYNTHETIC QA certificate collection / تحصيل مستخلص تجريبي',
        'project_id': project.id, 'wbs_id': wbs.id, 'cash_direction': 'in', 'amount': certificate.net_before_tax,
        'expected_date': today + timedelta(days=14), 'basis': 'SYNTHETIC QA approved unbilled certificate planning assumption',
        'source_reference': 'synthetic-qa-cash-' + token})
    cash.action_submit(); cash.action_approve()
    return project, today


def _xlsx_data(env, report):
    action = report.action_export_xlsx()
    match = re.fullmatch(r'/web/content/([1-9][0-9]*)\?download=true', action.get('url', ''))
    if not match:
        raise RecoveryRejected('The native export did not return its protected attachment route.')
    attachment = env['ir.attachment'].browse(int(match.group(1)))
    attachment.check_access('read')
    if attachment.res_model != report._name or attachment.res_id != report.id or attachment.public:
        raise RecoveryRejected('The QA export attachment does not belong to its protected source report.')
    # Odoo 20 exposes BinaryValue rather than a Python bytes instance.
    return bytes(attachment.raw)


def _export(env, proof, project, today, deadline):
    from odoo.tools.pdf import PdfReader
    results = []
    for language in ('en_US', 'ar_001'):
        localized = env(context={**env.context, 'lang': language})
        reports = [
            ('construction-cost', localized['jakh.construction.report'].create({'project_id': project.id,
                'report_type': 'cost', 'as_of_date': today}), 'jakh_operations.report_construction_control'),
            ('approved-unbilled', localized['jakh.construction.report'].create({'project_id': project.id,
                'report_type': 'unbilled', 'as_of_date': today}), 'jakh_operations.report_construction_control'),
            ('project-cash', localized['jakh.project.cashflow.report'].create({'project_id': project.id,
                'date_from': today, 'date_to': today + timedelta(days=90), 'opening_cash': 0}), 'jakh_operations.report_project_cash_forecast'),
            ('posted-trial-balance', localized['jakh.financial.report'].create({'company_id': env.company.id,
                'report_type': 'trial_balance', 'date_from': today, 'date_to': today}), 'jakh_operations.report_financial_control'),
        ]
        for label, report, reference in reports:
            deadline.remaining(); report.action_generate()
            pdf, output_format = localized['ir.actions.report'].with_context(force_report_rendering=True)._render_qweb_pdf(reference, report.ids)
            if output_format != 'pdf' or not pdf.startswith(b'%PDF-'):
                raise RecoveryRejected('A native governed report did not produce a real PDF.')
            reader = PdfReader(io.BytesIO(pdf), strict=False)
            if not 1 <= len(reader.pages) <= 20 or reader.metadata.get('/Creator') != 'JAKH ERP':
                raise RecoveryRejected('Native PDF page count or JAKH metadata failed bounded QA.')
            record = _private_binary(proof / (label + '-' + language + '.pdf'), pdf)
            record.update(format='pdf', language=language, pages=len(reader.pages), nativeTemplate=reference)
            results.append(record)
            deadline.remaining()
            xlsx = _xlsx_data(localized, report)
            with ZipFile(io.BytesIO(xlsx)) as archive:
                sheets = [name for name in archive.namelist() if name.startswith('xl/worksheets/sheet') and name.endswith('.xml')]
                if not sheets or sum(item.file_size for item in archive.infolist()) > MAX_EXPORT_BYTES:
                    raise RecoveryRejected('The native workbook is empty or exceeds the QA expanded bound.')
                rtl = any(b'rightToLeft="1"' in archive.read(name) for name in sheets)
                if language == 'ar_001' and not rtl:
                    raise RecoveryRejected('The native Arabic workbook is not right-to-left.')
            record = _private_binary(proof / (label + '-' + language + '.xlsx'), xlsx)
            record.update(format='xlsx', language=language, sheets=len(sheets), rtl=rtl)
            results.append(record)
    return results


def worker(root, customer_id, proof):
    cfg = native_staging.settings(root)
    require_quiet_http(cfg)
    customer, _parser = validate_customer(cfg, customer_id)
    proof = private_path(Path(cfg['root']), proof)
    marker = private_path(Path(cfg['root']), proof / 'qa-marker.json', kind='file')
    if proof.stat().st_mode & 0o077 or marker.stat().st_mode & 0o077:
        raise RecoveryRejected('Synthetic export QA proof and marker must be private.')
    metadata = json.loads(marker.read_text())
    if metadata.get('marker') != QA_MARKER or metadata.get('customer') != customer_id:
        raise RecoveryRejected('A new private synthetic QA proof marker is required.')
    if not re.fullmatch('[0-9a-f]{16}', metadata.get('token', '')):
        raise RecoveryRejected('The synthetic export QA identity must be a newly generated bounded token.')
    runtime = private_path(Path(cfg['root']), proof / 'qa-runtime')
    deadline = Deadline(900)
    cluster_password = secret_file(private_path(Path(cfg['root']), Path(cfg['root']) / 'secrets/cluster', kind='file'))
    preflight = _connect(cfg, cluster_password)
    try:
        verify_cluster(preflight, cfg); require_quiet_databases(preflight, cfg)
    finally:
        preflight.close()
    import odoo
    from odoo import api
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', customer['config'], '-d', customer['database'], '--no-http',
                                   '--max-cron-threads=0'], setup_logging=True)
    odoo.tools.config['data_dir'] = str(runtime)
    registry = Registry(customer['database'])
    cursor = registry.cursor()
    fixture_project_id = None
    try:
        # SQL is only identity resolution; all business data uses this enrolled
        # user's native permissions, never a superuser business environment.
        cursor.execute('SELECT id,company_id FROM res_users WHERE login=%s AND active', (customer['ownerLogin'],))
        identity = cursor.fetchone()
        if not identity or identity[0] == odoo.SUPERUSER_ID:
            raise RecoveryRejected('An active enrolled non-superuser QA owner is required.')
        context = {'allowed_company_ids': [identity[1]], 'lang': 'en_US', 'tz': 'UTC',
            'tracking_disable': True, 'mail_create_nosubscribe': True, 'mail_create_nolog': True,
            'mail_notify_force_send': False, 'force_report_rendering': True}
        env = api.Environment(cursor, identity[0], context)
        if env.su or not env.user.has_group('jakh_core.group_manager') or 'SYNTHETIC' not in (env.company.name or '').upper():
            raise RecoveryRejected('The normal QA owner must belong to an explicitly SYNTHETIC construction company.')
        languages = env['res.lang'].search([('code', 'in', ['en_US', 'ar_001']), ('active', '=', True)])
        if set(languages.mapped('code')) != {'en_US', 'ar_001'}:
            raise RecoveryRejected('Both existing native English and Arabic languages must already be active.')
        def reject_commit(*args, **kwargs):
            raise RecoveryRejected('Committing a synthetic export QA transaction is forbidden.')
        with _offline_python(), patch.object(type(cursor), 'commit', reject_commit):
            project, today = _scenario(env, metadata['token'])
            fixture_project_id = project.id
            files = _export(env, proof, project, today, deadline)
            evidence = {'marker': QA_MARKER, 'scope': 'rollback-only explicitly synthetic native export QA',
                'status': 'generated-for-visual-review', 'normalOwnerPermissions': True,
                'businessRowsRolledBack': True, 'sourceFilestoreUnmodified': True,
                'openingBalancesPersisted': False, 'productionAcceptance': False,
                'visualAcceptance': 'pending complete page-by-page review by root',
                'templates': 3, 'reportVariants': 4, 'pdfCount': 8, 'xlsxCount': 8, 'files': files}
    finally:
        try:
            cursor.rollback()
            if fixture_project_id:
                # Verify existence only for the exact QA identity just created;
                # never read any other customer's business rows or amounts.
                cursor.execute('SELECT 1 FROM project_project WHERE id=%s', (fixture_project_id,))
                if cursor.fetchone():
                    raise RecoveryRejected('The synthetic QA project unexpectedly survived rollback; do not accept these artifacts.')
                cursor.rollback()
        finally:
            cursor.close()
    write_private(proof / 'export-evidence.json', json.dumps(evidence, indent=2) + '\n')
    print('Rollback-only native export QA generated 8 PDFs and 8 workbooks; visual acceptance is pending.')


def run(root, customer_id):
    cfg = native_staging.settings(root)
    require_quiet_http(cfg)
    customer, _parser = validate_customer(cfg, customer_id)
    password = secret_file(private_path(Path(cfg['root']), Path(cfg['root']) / 'secrets/cluster', kind='file'))
    connection = _connect(cfg, password)
    try:
        verify_cluster(connection, cfg); require_quiet_databases(connection, cfg)
    finally:
        connection.close()
    parent = Path(cfg['root']) / 'export-proofs'
    if parent.exists():
        private_path(Path(cfg['root']), parent)
        if parent.stat().st_mode & 0o077:
            raise RecoveryRejected('The native QA proof parent must remain private.')
    else:
        parent.mkdir(mode=0o700)
    token = secrets.token_hex(8)
    proof = private_path(Path(cfg['root']), parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + token), exists=False)
    proof.mkdir(mode=0o700)
    runtime = proof / 'qa-runtime'; runtime.mkdir(mode=0o700)
    stores = runtime / 'filestore'; stores.mkdir(mode=0o700)
    source = Path(customer['dataDir']) / 'filestore' / customer['database']
    if source.exists():
        private_path(Path(cfg['root']), source)
    else:
        private_path(Path(cfg['root']), source, exists=False)
    deadline = Deadline(900)
    original = inventory_tree(source, deadline)
    copy_tree_new(source, stores / customer['database'], original, deadline)
    write_private(proof / 'qa-marker.json', json.dumps({'marker': QA_MARKER, 'customer': customer_id, 'token': token}) + '\n')
    descriptor = os.open(proof / 'private-native-qa.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        process = subprocess.Popen([cfg['python'], '-B', str(Path(__file__).resolve()), '--root', cfg['root'],
            '--customer', customer_id, '--worker', '--proof', str(proof)], env=native_staging.environment(cfg),
            cwd=cfg['source'], stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return_code = process.wait(timeout=deadline.remaining())
        except subprocess.TimeoutExpired:
            # Only this newly created process group, including its isolated
            # renderer children, is terminated. Existing ERP/browser servers
            # are never targeted. Closing the connection rolls back fixtures.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            raise RecoveryRejected('The bounded native export QA timed out; partial private proof was retained at ' + str(proof)) from None
    if return_code or inventory_tree(source, deadline) != original:
        raise RecoveryRejected('Native export QA failed or source filestore changed; retained private proof: ' + str(proof))
    evidence = json.loads((proof / 'export-evidence.json').read_text())
    return proof, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--customer', choices=('a', 'b'), required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--proof', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            worker(args.root, args.customer, args.proof)
            return 0
        proof, evidence = run(args.root, args.customer)
    except Exception as error:
        print(str(error) if isinstance(error, RecoveryRejected) else
              'Native export QA refused or failed; no credentials printed. Inspect retained private proof logs.', file=sys.stderr)
        return 1
    print(json.dumps({'proofDirectory': str(proof), 'pdfCount': evidence['pdfCount'], 'xlsxCount': evidence['xlsxCount'],
                      'sanitizedExportBytes': sum(item['bytes'] for item in evidence['files']),
                      'scope': 'synthetic QA only; visual review pending; no production acceptance'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
