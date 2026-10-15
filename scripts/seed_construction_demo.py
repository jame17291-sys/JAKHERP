"""Persist a bounded construction walkthrough in STOPPED synthetic staging.

Run only after offline enrollment and explicit SYNTHETIC company labelling:
  python scripts/seed_construction_demo.py --root work/<marked-staging> --customer a

The fixed scenario is created once, through normal users and business actions,
in ONE transaction. Failure rolls back its rows; an identical retry returns the
saved manifest. Existing scenario records are never repaired, reset or deleted.
No production databases, remote transports, invitations or usable actor logins.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import smtplib
import socket
import subprocess
from unittest.mock import patch

import native_staging
from native_recovery_drill import (RecoveryRejected, _connect, private_path,
                                  require_quiet_databases, require_quiet_http,
                                  validate_customer, verify_cluster)
from runtime_config import secret_file, write_private

SCENARIO = 'jakh-construction-demo-v1'
MANIFEST_KEY = 'jakh.demo.manifest'
ENABLED_KEY = 'jakh.demo.enabled'
PREFIX = 'JDM'
REQUIRED_MODULES = {'jakh_core', 'jakh_estimate', 'jakh_operations', 'jakh_assurance'}
CONTEXT = {'lang': 'en_US', 'tz': 'UTC', 'tracking_disable': True,
           'mail_create_nosubscribe': True, 'mail_create_nolog': True,
           'mail_notify_force_send': False, 'no_reset_password': True}


@contextmanager
def offline_only():
    """Permit the existing loopback database connection, never external IO."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def local(stream, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise RecoveryRejected('External connections are disabled during demo seeding.')
        return connect(stream, address)

    def local_ex(stream, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise RecoveryRejected('External connections are disabled during demo seeding.')
        return connect_ex(stream, address)

    def no_mail(*args, **kwargs):
        raise RecoveryRejected('Mail delivery is disabled during demo seeding.')

    with patch.object(socket.socket, 'connect', local), patch.object(socket.socket, 'connect_ex', local_ex), \
            patch.object(smtplib.SMTP, 'connect', no_mail):
        yield


def _validate_manifest_header(value, customer_id, database, company_id):
    if (not isinstance(value, dict) or value.get('schema') != 1
            or value.get('scenario') != SCENARIO or value.get('synthetic') is not True
            or value.get('status') != 'complete' or value.get('customer') != customer_id
            or value.get('database') != database or value.get('company_id') != company_id
            or not isinstance(value.get('records'), dict) or not value['records']):
        raise RecoveryRejected('The existing demo marker is incompatible; nothing was changed.')


def _private_manifest(cfg, customer_id, manifest):
    """A missing export is recoverable from the transactional database marker."""
    root = Path(cfg['root'])
    destination = root / 'secrets' / ('construction-demo-' + customer_id + '.json')
    private_path(root, destination.parent)
    if destination.exists() or destination.is_symlink():
        path = private_path(root, destination, kind='file')
        if path.stat().st_mode & 0o077:
            raise RecoveryRejected('The existing demo manifest must be private.')
        if json.loads(path.read_text()) != manifest:
            raise RecoveryRejected('A different private demo manifest exists; it was not overwritten.')
    else:
        private_path(root, destination, kind='file', exists=False)
        write_private(destination, json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    return destination


class Demo:
    """Small normal-ORM scenario builder; no elevated business environment."""
    def __init__(self, env, customer_id, database):
        self.env = env
        self.company = env.company
        self.customer_id, self.database = customer_id, database
        self.today = __import__('odoo').fields.Date.today()
        self.records, self.stages, self.actors, self.moves = {}, [], {}, set()
        self.setup_changes = {}

    def actor_env(self, role):
        return self.env(user=self.actors[role], context=dict(self.env.context))

    def save(self, key, record, label=None):
        record.ensure_one()
        record = record.with_user(self.env.user)
        record.check_access('read')
        state = record.state if 'state' in record._fields else None
        self.records[key] = {'model': record._name, 'id': record.id,
            'label': label or record.display_name, 'status': state,
            'url': '/jakh/%s/%s' % (record._name, record.id),
            'transient_snapshot': bool(record._transient),
            'create_uid': record.create_uid.id if 'create_uid' in record._fields else None}
        return record

    def stage(self, key, label, keys, completed, pending=()):
        self.stages.append({'key': key, 'label': label, 'record_keys': keys,
            'completed_steps': list(completed), 'pending_steps': list(pending)})

    def posted(self, move):
        move = move.with_user(self.env.user)
        if move.state != 'posted' or not self.company.currency_id.is_zero(sum(move.line_ids.mapped('balance'))):
            raise RecoveryRejected('A demo accounting document was not posted and balanced.')
        self.moves.add(move.id)

    def settle(self, invoice, key):
        from odoo import fields
        env = self.actor_env('finance')
        invoice = invoice.with_env(env)
        inbound = invoice.move_type in ('out_invoice', 'in_refund')
        methods = self.bank.inbound_payment_method_line_ids if inbound else self.bank.outbound_payment_method_line_ids
        method = methods.filtered(lambda row: row.code == 'manual')[:1]
        if not method:
            raise RecoveryRejected('The isolated demo journal has no native manual payment method.')
        payments = env['account.payment.register'].with_context(active_model='account.move', active_ids=invoice.ids).create({
            'journal_id': self.bank.id, 'payment_method_line_id': method.id,
            'payment_date': fields.Date.today(),
        })._create_payments()
        if len(payments) != 1 or not invoice.currency_id.is_zero(invoice.amount_residual):
            raise RecoveryRejected('A demo payment did not reconcile its single source document.')
        for move in payments.move_id:
            self.posted(move)
        self.save(key, payments)
        return payments

    def post_and_settle(self, invoice, key):
        invoice = invoice.with_env(self.actor_env('finance'))
        invoice.action_post()
        self.posted(invoice)
        self.save(key, invoice)
        return self.settle(invoice, key.replace('_invoice', '') + '_payment' if key.endswith('_invoice') else key + '_payment')

    def evidence(self, record, name, payload):
        return record.env['ir.attachment'].create({'name': name, 'raw': payload,
            'res_model': record._name, 'res_id': record.id, 'public': False})

    def setup(self):
        from odoo import Command
        env, company = self.env, self.company
        # The enrolled platform owner may provision demo identities. All later
        # scenario work executes as these users through their actual role ACLs.
        roles = {'director': 'group_manager', 'estimator': 'group_estimator',
            'technical': 'group_technical', 'project_manager': 'group_project_manager',
            'procurement': 'group_procurement', 'finance': 'group_finance', 'quality': 'group_quality'}
        for role, group in roles.items():
            login = '%s.%s.%s@invalid.test' % (SCENARIO, self.customer_id, role)
            if env['res.users'].with_context(active_test=False).search_count([('login', '=', login)]):
                raise RecoveryRejected('An unmarked demo actor identity exists; no record was overwritten.')
            user = env['res.users'].create({'name': 'DEMO ONLY — ' + role.replace('_', ' ').title(),
                'login': login, 'email': login, 'notification_type': 'inbox',
                'company_id': company.id, 'company_ids': [Command.set(company.ids)],
                'group_ids': [Command.set([env.ref('jakh_core.' + group).id, env.ref('project.group_project_user').id])]})
            self.actors[role] = user
        self._check_actor_credentials()

        def account(suffix, name, kind, reconcile=False):
            code = PREFIX + suffix
            if env['account.account'].with_context(active_test=False).search_count([('code', '=', code), ('company_ids', 'in', company.ids)]):
                raise RecoveryRejected('An unmarked demo account code exists; no account was overwritten.')
            return env['account.account'].create({'name': 'DEMO ONLY — ' + name, 'code': code,
                'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(company.ids)]})

        self.ar = account('901', 'Receivables', 'asset_receivable', True)
        self.ap = account('902', 'Payables', 'liability_payable', True)
        self.income = account('903', 'Measured work revenue', 'income')
        self.expense = account('904', 'Project execution costs', 'expense_direct_cost')
        self.clearing = account('905', 'Labour and plant accrual', 'liability_current', True)
        self.retention = account('906', 'Owner retention receivable', 'asset_current')
        self.advance = account('907', 'Owner advance liability', 'liability_current')
        self.bank_account = account('908', 'Manual demonstration cash', 'asset_cash')
        self.ap_retention = account('909', 'Subcontract retention liability', 'liability_current')
        self.ap_advance = account('910', 'Subcontract prepaid advance', 'asset_current')
        own_inventory = account('911', 'Material inventory', 'asset_current')

        def journal(code, name, kind, default=False):
            if env['account.journal'].search_count([('code', '=', code), ('company_id', '=', company.id)]):
                raise RecoveryRejected('An unmarked demo journal code exists; no journal was overwritten.')
            return env['account.journal'].create({'name': 'DEMO ONLY — ' + name, 'code': code,
                'type': kind, 'company_id': company.id, 'default_account_id': default.id if default else False})

        self.sales = journal('JDMS', 'Measured sales', 'sale', self.income)
        self.purchase = journal('JDMP', 'Purchases', 'purchase', self.expense)
        self.costs = journal('JDMC', 'Labour and plant costs', 'general')
        self.bank = journal('JDMB', 'Manual payment simulation', 'bank', self.bank_account)
        own_stock = journal('JDMV', 'Native site material valuation', 'general')
        (self.bank.inbound_payment_method_line_ids | self.bank.outbound_payment_method_line_ids).write({'payment_account_id': self.bank_account.id})
        # Explicitly authorized, narrowly bounded synthetic company setup: only
        # fill EMPTY values. Existing valid accounting choices are preserved.
        for field, proposed in [('account_stock_journal_id', own_stock), ('account_stock_valuation_id', own_inventory)]:
            current = company[field]
            if current:
                if field == 'account_stock_journal_id' and (current.company_id != company or current.type != 'general'):
                    raise RecoveryRejected('The existing synthetic stock journal is incompatible; it was not replaced.')
                if field == 'account_stock_valuation_id' and (company not in current.company_ids or current.account_type != 'asset_current'):
                    raise RecoveryRejected('The existing synthetic inventory account is incompatible; it was not replaced.')
                self.setup_changes[field] = {'before': current.id, 'after': current.id, 'filled_empty': False}
            else:
                company.write({field: proposed.id})
                self.setup_changes[field] = {'before': False, 'after': proposed.id, 'filled_empty': True}
        self.inventory = company.account_stock_valuation_id
        self.customer = env['res.partner'].create({'name': 'DEMO ONLY — 11 Villas Client',
            'email': 'demo.client@invalid.test', 'company_id': company.id,
            'property_account_receivable_id': self.ar.id})
        self.vendor = env['res.partner'].create({'name': 'DEMO ONLY — Materials and Subcontract Supplier',
            'email': 'demo.supplier@invalid.test', 'company_id': company.id,
            'property_account_payable_id': self.ap.id})
        env['product.pricelist'].create({'name': 'DEMO ONLY — Company currency study',
            'currency_id': company.currency_id.id, 'company_id': company.id})
        self.scope = env['product.product'].create({'name': 'DEMO ONLY — Measured construction work',
            'default_code': 'JDM-SCOPE', 'type': 'service', 'company_id': company.id,
            'taxes_id': [Command.clear()], 'supplier_taxes_id': [Command.clear()]})
        self.sub_product = env['product.product'].create({'name': 'DEMO ONLY — Installation subcontract',
            'default_code': 'JDM-SUB', 'type': 'service', 'purchase_method': 'receive', 'company_id': company.id,
            'property_account_expense_id': self.expense.id,
            'taxes_id': [Command.clear()], 'supplier_taxes_id': [Command.clear()]})
        category = env['product.category'].create({'name': 'DEMO ONLY — Perpetual site materials',
            'property_valuation': 'real_time', 'property_cost_method': 'standard',
            'property_stock_valuation_account_id': self.inventory.id,
            'property_account_expense_categ_id': self.expense.id})
        self.material = env['product.product'].create({'name': 'DEMO ONLY — Structural material unit',
            'default_code': 'JDM-MAT', 'type': 'consu', 'is_storable': True, 'company_id': company.id,
            'categ_id': category.id, 'standard_price': 40, 'purchase_method': 'receive',
            'taxes_id': [Command.clear()], 'supplier_taxes_id': [Command.clear()]})
        if env['stock.warehouse'].search_count([('code', '=', 'JDM'), ('company_id', '=', company.id)]):
            raise RecoveryRejected('An unmarked demo warehouse exists; no warehouse was overwritten.')
        self.warehouse = env['stock.warehouse'].create({'name': 'DEMO ONLY — Isolated site store',
            'code': 'JDM', 'company_id': company.id})
        self.warehouse.int_type_id.write({'active': True})
        self.consumption = env['stock.location'].create({'name': 'DEMO ONLY — Project consumption',
            'usage': 'inventory', 'company_id': company.id, 'valuation_account_id': self.expense.id})
        self.discipline = self.actor_env('director')['jakh.discipline'].create({'name': 'DEMO ONLY — Civil and structure',
            'code': 'JDM-CIV', 'company_id': company.id})
        self.codes = {}
        for key, label in [('structure', 'Structure'), ('finishes', 'Finishes'), ('mep', 'MEP')]:
            self.codes[key] = self.actor_env('director')['jakh.cost.code'].create({
                'name': 'DEMO ONLY — ' + label, 'code': 'JDM-' + key.upper(), 'company_id': company.id})

    def _check_actor_credentials(self):
        # Read-only SQL resolves authentication facts that normal user fields
        # intentionally do not expose. It never changes credentials or states.
        self.env.cr.execute('SELECT id FROM res_users WHERE id = ANY(%s) AND password IS NOT NULL',
                            (list(user.id for user in self.actors.values()),))
        if self.env.cr.fetchone():
            raise RecoveryRejected('Demo actors must never receive a stored password.')
        for user in self.actors.values():
            if user.partner_id.signup_type or not user.login.endswith('@invalid.test'):
                raise RecoveryRejected('Demo actors must have no invitation and use invalid.test identities.')
        if 'res.users.apikeys' in self.env:
            if self.env['res.users.apikeys'].search_count([('user_id', 'in', [u.id for u in self.actors.values()])]):
                raise RecoveryRejected('Demo actors must have no API credentials.')

    def award(self, closed=False):
        from odoo import Command
        prefix = 'closed_' if closed else ''
        director, estimator = self.actor_env('director'), self.actor_env('estimator')
        title = 'DEMO ONLY — Completed reference project' if closed else 'DEMO ONLY — 11 Villas, one variant, active site'
        lead = director['crm.lead'].create({'name': title + ' tender', 'type': 'opportunity',
            'partner_id': self.customer.id, 'company_id': self.company.id,
            'user_id': self.actors['estimator'].id})
        estimate = estimator['jakh.estimate'].browse(lead.with_env(estimator).action_jakh_new_estimate()['res_id'])
        rows = [('structure', 10, [('material', 1, 40), ('labour', 1, 20), ('equipment', 1, 10), ('subcontract', 1, 10)])]
        if not closed:
            rows += [('finishes', 20, [('material', 1, 20), ('labour', 1, 15), ('equipment', 1, 5)]),
                     ('mep', 1, [('material', 1, 250), ('labour', 1, 150), ('subcontract', 1, 100)])]
        estimate.write({'title': title, 'unit_count': 1 if closed else 11,
            'header_markup_percent': 25 if closed else 0,
            'indirect_amount': 0 if closed else 2352, 'overhead_percent': 0 if closed else 10,
            'profit_percent': 0 if closed else 20, 'project_manager_id': self.actors['project_manager'].id,
            'line_ids': [Command.create({'code': 'JDM-' + key.upper(), 'name': 'DEMO ONLY — ' + key.title(),
                'cost_code_id': self.codes[key].id, 'product_id': self.scope.id, 'uom_id': self.scope.uom_id.id,
                'quantity': qty, 'resource_ids': [Command.create({'name': 'DEMO ONLY — ' + kind,
                    'resource_type': kind, 'quantity': amount, 'unit_cost': rate}) for kind, amount, rate in resources]})
                for key, qty, resources in rows]})
        if not closed:
            estimator['jakh.estimate.building'].create({'estimate_id': estimate.id, 'number': 11,
                'name': 'DEMO ONLY — Villa 11 variant (+20% quantities)',
                'override_ids': [Command.create({'line_id': line.id, 'quantity': line.quantity * 1.2}) for line in estimate.line_ids]})
        estimate.action_save_version()
        estimate.action_submit()
        estimate = estimate.with_env(director)
        estimate.action_approve(); estimate.action_handover()
        if estimate.action_handover()['res_id'] != estimate.sale_order_id.id:
            raise RecoveryRejected('Native handover was not idempotent.')
        project, contract = estimate.project_id, estimate.commercial_contract_id
        project.write({'privacy_visibility': 'employees'})
        project.message_subscribe(partner_ids=[actor.partner_id.id for actor in self.actors.values()])
        project.jakh_wbs_ids.action_release()
        estimate.sale_order_id.action_confirm()
        contract.write({'journal_id': self.sales.id, 'income_account_id': self.income.id,
            'retention_percent': 10 if closed else 5, 'retention_cap': 100 if closed else 2000,
            'retention_account_id': self.retention.id, 'recovery_percent': 20,
            'advance_cap': 100 if closed else 1000, 'advance_account_id': self.advance.id,
            'certificate_review_policy': 'technical_commercial_finance'})
        contract.line_ids.write({'discipline_id': self.discipline.id})
        contract.action_activate()
        buildings = director['jakh.project.location'].create([{'name': 'DEMO ONLY — ' + ('Reference building' if closed else 'Villa %02d' % number),
            'code': 'REF' if closed else 'V%02d' % number, 'project_id': project.id, 'kind': 'building'}
            for number in range(1, 2 if closed else 12)])
        for line in contract.line_ids:
            # Original approved quantities, including the variant, are allocated
            # explicitly across all buildings. The last row absorbs cents only.
            source = estimate.line_ids.filtered(lambda row: row.line_uuid == line.source_line_uuid)
            if not source:
                raise RecoveryRejected('The contract lost its approved estimate line identity.')
            quantities = [line.quantity] if closed else [source.quantity] * 10 + [source.quantity * 1.2]
            budgets = [self.company.currency_id.round(line.original_budget_total * qty / line.quantity) for qty in quantities]
            budgets[-1] += line.original_budget_total - sum(budgets)
            allocation = director['jakh.boq.location.budget'].create({'contract_line_id': line.id,
                'reason': 'DEMO ONLY — Reviewed original building allocation from approved study, not an opening balance.',
                'line_ids': [Command.create({'location_id': building.id, 'discipline_id': self.discipline.id,
                    'quantity': qty, 'budget_amount': budget}) for building, qty, budget in zip(buildings, quantities, budgets)]})
            allocation.action_submit(); allocation.action_approve()
            self.save(prefix + 'building_budget' + ('' if line == contract.line_ids[0] else '_' + source.code.lower()), allocation)
        self.save(prefix + 'opportunity', lead)
        self.save(prefix + 'estimate', estimate)
        self.save(prefix + 'sale_order', estimate.sale_order_id)
        self.save(prefix + 'project', project)
        self.save(prefix + 'contract', contract)
        for wbs in project.jakh_wbs_ids:
            source = estimate.line_ids.filtered(lambda row: row == wbs.jakh_estimate_line_id)
            key = source.code.removeprefix('JDM-').lower()
            self.save(prefix + 'wbs_' + key, wbs)
        for number, building in enumerate(buildings, 1):
            self.save(prefix + ('building' if closed else 'villa_%02d' % number), building)
        return {'prefix': prefix, 'project': project, 'contract': contract,
                'wbs': project.jakh_wbs_ids.filtered(lambda row: row.code == 'JDM-STRUCTURE'),
                'boq': contract.line_ids.filtered(lambda row: row.code == 'JDM-STRUCTURE'),
                'building': buildings[0], 'estimate': estimate}

    def owner_advance(self, job):
        prefix, contract = job['prefix'], job['contract']
        env = self.actor_env('finance')
        document = env['jakh.contract.cash.document'].create({'name': 'DEMO ONLY — Owner mobilization advance',
            'contract_id': contract.id, 'kind': 'advance', 'amount': 100 if prefix else 1000,
            'reason': 'DEMO ONLY — Simulated approved mobilization funding; no bank transport.'})
        document.action_submit(); document.action_approve(); document.action_create_invoice()
        self.post_and_settle(document.invoice_id, prefix + 'owner_advance_invoice')
        self.save(prefix + 'owner_advance', document)

    def request(self, job, name, quantity, price, key, pending=False):
        from odoo import Command
        technical, procurement = self.actor_env('technical'), self.actor_env('procurement')
        request = technical['jakh.purchase.request'].create({'name': 'DEMO ONLY — ' + name,
            'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
            'material_plan_reference': 'DEMO ONLY — Released WBS plan and approved BOQ',
            'line_ids': [Command.create({'name': 'DEMO ONLY — Installation', 'product_id': self.sub_product.id,
                'uom_id': self.sub_product.uom_id.id, 'quantity': quantity,
                'contract_line_id': job['boq'].id, 'location_id': job['building'].id,
                'discipline_id': self.discipline.id})]})
        request = request.with_env(procurement)
        request.write({'supplier_id': self.vendor.id})
        procurement['jakh.purchase.request.cost'].create({'line_id': request.line_ids.id, 'unit_price': price})
        request.with_env(technical).action_submit()
        if not pending:
            request.with_env(self.actor_env('director')).action_approve()
            request.action_release()
            order = request.purchase_order_id
            order.button_confirm()
            self.save(key.replace('_pr', '_po'), order)
        self.save(key, request)
        return request

    def materials(self, job):
        from odoo import Command
        env, director = self.actor_env('procurement'), self.actor_env('director')
        demand = director['jakh.material.request'].create({'name': 'DEMO ONLY — Structure materials demand',
            'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
            'contract_line_id': job['boq'].id, 'location_id': job['building'].id,
            'discipline_id': self.discipline.id, 'product_id': self.material.id, 'quantity': 60,
            'source_location_id': self.warehouse.lot_stock_id.id,
            'consumption_location_id': self.consumption.id,
            'reason': 'DEMO ONLY — Released structural work plan; no stock opening balances.'})
        demand.action_submit(); demand.action_approve(); demand.action_prepare_shortage()
        request = demand.request_id.with_env(env)
        request.write({'name': 'DEMO ONLY — Material shortage procurement', 'supplier_id': self.vendor.id})
        env['jakh.purchase.request.cost'].create({'line_id': request.line_ids.id, 'unit_price': 40})
        request.action_submit(); request.with_env(director).action_approve(); request.action_release()
        order = request.purchase_order_id
        order.write({'picking_type_id': self.warehouse.in_type_id.id})
        order.button_confirm()
        incoming = order.picking_ids
        incoming.move_ids.write({'quantity': 60, 'picked': True}); incoming.move_ids._action_done()
        order.action_create_invoice()
        bill = order.invoice_ids.with_env(self.actor_env('finance'))
        bill.write({'invoice_date': self.today, 'journal_id': self.purchase.id})
        self.post_and_settle(bill, 'material_bill')
        issue = env['stock.picking'].browse(demand.with_env(env).action_prepare_issue()['res_id'])
        issue.move_ids.write({'quantity': 60, 'picked': True}); issue.move_ids._action_done()
        self.posted(issue.move_ids.account_move_id)
        document = env['jakh.material.return'].create({'name': 'DEMO ONLY — Ten unused material units returned',
            'issue_move_id': issue.move_ids.id, 'quantity': 10,
            'reason': 'DEMO ONLY — Verified unused material returned to the same demo store.'})
        evidence = self.evidence(document, 'DEMO-material-return.txt', b'DEMO ONLY: 10 unused units verified and returned.')
        document.write({'evidence_attachment_ids': [Command.set(evidence.ids)]})
        document.action_submit(); document.with_env(director).action_approve()
        returned = env['stock.picking'].browse(document.action_prepare_picking()['res_id'])
        returned.move_ids.write({'quantity': 10, 'picked': True}); returned.move_ids._action_done()
        self.posted(returned.move_ids.account_move_id)
        if demand.issued_quantity != 50 or demand.remaining_quantity != 10:
            raise RecoveryRejected('The demo material return did not preserve the remaining demand.')
        for key, record in [('material_demand', demand), ('material_pr', request), ('material_po', order),
                            ('material_receipt', incoming), ('material_issue', issue), ('material_return', document)]:
            self.save(key, record)
        self.stage('materials', 'Material planning, procurement and site control',
            ['material_demand', 'material_pr', 'material_po', 'material_receipt', 'material_issue', 'material_return', 'material_bill'],
            ['Demand approved; shortage became requisition and confirmed PO',
             '60 units received and supplier bill posted and paid',
             '60 issued; 10 returned through approved evidence; net site cost is posted once'],
            ['10 units remain on approved demand for a supervised new issue'])

    def subcontract(self, job):
        prefix = job['prefix']
        finance, director = self.actor_env('finance'), self.actor_env('director')
        request = self.request(job, 'Subcontract installation work package', 10, 20, prefix + 'subcontract_pr')
        order = request.purchase_order_id
        agreement = finance['jakh.subcontract.finance.agreement'].create({
            'name': 'DEMO ONLY — Subcontract advance and retention terms', 'purchase_order_id': order.id,
            'journal_id': self.purchase.id, 'retention_percent': 10, 'retention_cap': 20,
            'advance_percent': 20, 'advance_cap': 40, 'recovery_percent': 20,
            'retention_account_id': self.ap_retention.id, 'advance_account_id': self.ap_advance.id})
        agreement.action_activate()
        advance = finance['jakh.subcontract.cash.document'].create({'name': 'DEMO ONLY — Subcontract mobilization advance',
            'agreement_id': agreement.id, 'document_type': 'advance', 'amount': 40,
            'reason': 'DEMO ONLY — Approved simulated supplier advance.'})
        advance.action_submit(); advance.action_approve(); advance.action_create_bill()
        self.post_and_settle(advance.invoice_id, prefix + 'subcontract_advance_invoice')
        ses = self.actor_env('procurement')['jakh.subcontract.service.entry'].create({
            'name': 'DEMO ONLY — Accepted installation measurement', 'purchase_line_id': order.order_line.id,
            'quantity': 10, 'notes': 'DEMO ONLY — Installation measured and inspected against confirmed order.'})
        ses.action_submit(); ses.with_env(director).action_approve()
        certificate = finance['jakh.subcontract.payment.certificate'].browse(ses.with_env(finance).action_create_bill()['res_id'])
        certificate.action_submit(); certificate.action_approve(); certificate.action_create_bill()
        self.post_and_settle(ses.invoice_id, prefix + 'subcontract_bill')
        release = finance['jakh.subcontract.cash.document'].create({'name': 'DEMO ONLY — Subcontract retention release',
            'agreement_id': agreement.id, 'document_type': 'release', 'amount': 20,
            'reason': 'DEMO ONLY — Accepted completion and retention release evidence.'})
        release.action_submit(); release.action_approve(); release.action_create_bill()
        self.post_and_settle(release.invoice_id, prefix + 'subcontract_retention_invoice')
        agreement.action_close()
        for key, record in [('subcontract_agreement', agreement), ('service_entry', ses),
                            ('subcontract_certificate', certificate), ('subcontract_advance', advance),
                            ('subcontract_retention', release)]:
            self.save(prefix + key, record)
        if not prefix:
            pending = self.request(job, 'Next villa installation package', 5, 20, 'pending_subcontract_pr')
            service = self.actor_env('procurement')['jakh.subcontract.service.entry'].create({
                'name': 'DEMO ONLY — Pending next villa service entry',
                'purchase_line_id': pending.purchase_order_id.order_line.id, 'quantity': 2,
                'notes': 'DEMO ONLY — Supervisor measurement waiting for commercial acceptance.'})
            service.action_submit()
            self.save('pending_service_entry', service)
            self.request(job, 'Next villa requisition awaiting approval', 2, 20, 'pending_purchase_request', pending=True)
            self.stage('subcontract', 'Accepted work, supplier certificates and cash',
                ['subcontract_agreement', 'service_entry', 'subcontract_certificate', 'subcontract_bill',
                 'subcontract_advance', 'subcontract_retention', 'pending_service_entry', 'pending_purchase_request'],
                ['Confirmed subcontract; accepted SES prepared governed supplier certificate',
                 'Advance, bill, recovery and retention release posted, paid and reconciled',
                 'Completed subcontract commercial agreement closed'],
                ['Review the second submitted service entry', 'Review the submitted next-villa requisition'])

    def execution(self, job):
        prefix, director, finance = job['prefix'], self.actor_env('director'), self.actor_env('finance')
        for kind in ('jisr', 'p6'):
            mapping = director['jakh.integration.binding'].create({'name': 'DEMO ONLY — Reviewed ' + kind + ' mapping',
                'kind': kind, 'company_id': self.company.id,
                'source_project_ref': SCENARIO + '-' + (prefix or 'active'), 'source_wbs_ref': 'STRUCTURE',
                'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
                'contract_line_id': job['boq'].id, 'location_id': job['building'].id,
                'discipline_id': self.discipline.id})
            mapping.action_approve()
            self.save(prefix + kind + '_mapping', mapping)
        inbox = director['jakh.integration.event']
        source = {'version': 1, 'kind': 'jisr', 'external_id': SCENARIO + '-' + (prefix or 'active') + '-labour',
            'company_id': self.company.id, 'source_project_ref': SCENARIO + '-' + (prefix or 'active'),
            'source_wbs_ref': 'STRUCTURE', 'data': {'source_reference': 'DEMO ONLY — Offline approved Jisr-shaped allocation',
                'work_date': str(self.today), 'resource_reference': 'DEMO ONLY — Crew A', 'quantity': 8 if prefix else 80,
                'amount': 120 if prefix else 1200, 'journal_id': self.costs.id,
                'expense_account_id': self.expense.id, 'offset_account_id': self.clearing.id}}
        event = inbox.ingest_source(source)
        if inbox.ingest_source(source)['id'] != event['id']:
            raise RecoveryRejected('The native demo inbox did not deduplicate the same source message.')
        labour = finance[event['business_model']].browse(event['business_id'])
        labour.action_accept(); self.posted(labour.account_move_id)
        plant = director['jakh.site.resource'].create({'name': 'DEMO ONLY — Excavator ' + (prefix or 'active'),
            'kind': 'plant', 'company_id': self.company.id, 'source_reference': SCENARIO + '-' + (prefix or 'active') + '-plant',
            'cost_journal_id': self.costs.id, 'expense_account_id': self.expense.id, 'clearing_account_id': self.clearing.id})
        start = datetime.combine(self.today, datetime.min.time())
        assignment = director['jakh.resource.assignment'].create({'name': 'DEMO ONLY — Verified plant shift',
            'resource_id': plant.id, 'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
            'site_reference': 'DEMO ONLY — Reference site' if prefix else 'DEMO ONLY — Villa 01',
            'contract_line_id': job['boq'].id, 'location_id': job['building'].id, 'discipline_id': self.discipline.id,
            'start_at': start, 'end_at': start + timedelta(hours=8)})
        assignment.action_approve()
        assignment.write({'actual_hours': 8, 'actual_cost': 80 if prefix else 800,
            'completion_reference': 'DEMO ONLY — Verified plant shift return'})
        assignment.action_prepare_actual(); assignment.actual_feed_id.with_env(finance).action_accept()
        self.posted(assignment.actual_feed_id.account_move_id)
        progress_event = inbox.ingest_source({'version': 1, 'kind': 'p6',
            'external_id': SCENARIO + '-' + (prefix or 'active') + '-p6', 'company_id': self.company.id,
            'source_project_ref': SCENARIO + '-' + (prefix or 'active'), 'source_wbs_ref': 'STRUCTURE',
            'data': {'source_reference': 'DEMO ONLY — Offline scheduling-master data date', 'as_of_date': str(self.today),
                'planned_value': 800 if prefix else 6000, 'earned_value': 800 if prefix else 4500,
                'progress_percent': 100 if prefix else 40}})
        progress = director[progress_event['business_model']].browse(progress_event['business_id'])
        progress.action_accept()
        forecast = director['jakh.project.forecast'].create({'name': 'DEMO ONLY — Reviewed completion forecast',
            'wbs_id': job['wbs'].id, 'forecast_final_cost': 400 if prefix else 11000,
            'basis': 'DEMO ONLY — Reviewed actual costs, commitments and remaining work; no live vendor data.'})
        forecast.action_submit(); forecast.action_approve()
        for key, record in [('labour_actual', labour), ('plant_assignment', assignment),
                            ('p6_progress', progress), ('forecast', forecast)]:
            self.save(prefix + key, record)
        if not prefix:
            self.stage('execution', 'Labour, plant, schedule and forecast',
                ['jisr_mapping', 'p6_mapping', 'labour_actual', 'plant_assignment', 'p6_progress', 'forecast'],
                ['Reviewed offline Jisr-shaped message deduplicated and labour cost posted',
                 'Plant shift approved and accepted actual posted',
                 'Offline P6-shaped progress accepted; completion forecast approved'])

    def assurance(self, job):
        prefix, technical, quality = job['prefix'], self.actor_env('technical'), self.actor_env('quality')
        document = technical['jakh.document'].create({'name': 'DEMO ONLY — Structural drawing',
            'code': 'JDM-STRUCT-001', 'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
            'location_id': job['building'].id, 'discipline_id': self.discipline.id})
        revision = technical['jakh.document.revision'].create({'document_id': document.id, 'name': 'A',
            'summary': 'DEMO ONLY — Approved construction reference issue',
            'reviewer_id': self.actors['quality'].id, 'due_date': self.today})
        self.evidence(revision, 'DEMO-drawing-A.txt', b'DEMO ONLY: illustrative drawing evidence, not a construction drawing.')
        revision.action_submit(); revision.with_env(quality).action_approve()
        inspection = quality['jakh.assurance.case'].create({'name': 'DEMO ONLY — Accepted structural inspection',
            'kind': 'inspection', 'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
            'due_date': self.today, 'description': '<p>DEMO ONLY — Inspect measured site work.</p>'})
        inspection.action_submit(); inspection.action_start()
        inspection.write({'resolution': '<p>DEMO ONLY — Work accepted for this simulated inspection.</p>'})
        inspection.action_resolve()
        self.evidence(inspection, 'DEMO-inspection.txt', b'DEMO ONLY: simulated inspection acceptance evidence.')
        inspection.action_close()
        self.save(prefix + 'document', document); self.save(prefix + 'document_revision', revision)
        self.save(prefix + 'assurance', inspection)
        if not prefix:
            pending = technical['jakh.document.revision'].create({'document_id': document.id, 'name': 'B',
                'summary': 'DEMO ONLY — Variant villa coordination, waiting for independent review',
                'reviewer_id': self.actors['quality'].id, 'due_date': self.today + timedelta(days=3)})
            self.evidence(pending, 'DEMO-drawing-B.txt', b'DEMO ONLY: pending variant coordination reference.')
            pending.action_submit()
            case = technical['jakh.assurance.case'].create({'name': 'DEMO ONLY — Open variant coordination NCR',
                'kind': 'ncr', 'project_id': job['project'].id, 'wbs_id': job['wbs'].id,
                'responsible_id': self.actors['quality'].id, 'due_date': self.today + timedelta(days=2),
                'description': '<p>DEMO ONLY — Review the Villa 11 interface and attach corrective evidence.</p>'})
            case.action_submit(); case.with_env(quality).action_start()
            self.save('pending_document_revision', pending); self.save('pending_assurance', case)
            self.stage('assurance', 'Controlled documents and QHSE evidence',
                ['document', 'document_revision', 'assurance', 'pending_document_revision', 'pending_assurance'],
                ['Revision A independently approved with locked attachment', 'Inspection resolved and verified with evidence'],
                ['Independently review revision B', 'Resolve the open variant NCR and attach evidence before independent closure'])

    def certificate(self, job, fraction, key, pending=False):
        technical = self.actor_env('technical')
        certificate = technical['jakh.payment.certificate'].create({'name': 'DEMO ONLY — ' +
            ('Next owner measurement awaiting review' if pending else 'Measured owner certificate'),
            'contract_id': job['contract'].id, 'date': self.today})
        certificate.action_prepare_lines()
        for line in certificate.line_ids:
            line.write({'cumulative_quantity': line.contract_line_id.quantity * fraction})
        certificate.action_submit()
        if not pending:
            certificate.action_review_approve()
            certificate.with_env(self.actor_env('project_manager')).action_review_approve()
            certificate = certificate.with_env(self.actor_env('finance'))
            certificate.action_approve()
            certificate.action_create_invoice()
            self.post_and_settle(certificate.invoice_id, job['prefix'] + 'owner_invoice')
        return self.save(key, certificate)

    def reporting(self, job, closed=False):
        prefix, env = job['prefix'], self.env
        cvr = env['jakh.project.control.report'].create({'project_id': job['project'].id})
        cvr.action_generate()
        construction = env['jakh.construction.report'].create({'project_id': job['project'].id, 'report_type': 'cost'})
        construction.action_generate()
        cash = env['jakh.project.cash.forecast'].create({'name': 'DEMO ONLY — Remaining owner collection plan',
            'project_id': job['project'].id, 'wbs_id': job['wbs'].id, 'cash_direction': 'in',
            'expected_date': self.today + timedelta(days=30), 'amount': 6000,
            'source_reference': SCENARIO + '-active-owner-collection',
            'basis': 'DEMO ONLY — Reviewed planning assumption; forecast, not posted cash.'}) if not closed else None
        if cash:
            cash.action_submit(); cash.action_approve(); self.save('cash_forecast', cash)
        cash_report = env['jakh.project.cashflow.report'].create({'project_id': job['project'].id,
            'date_from': self.today, 'date_to': self.today + timedelta(days=90), 'opening_cash': 0})
        cash_report.action_generate()
        schedule = env['jakh.report.schedule'].create({'name': 'DEMO ONLY — Weekly construction control',
            'project_id': job['project'].id, 'owner_id': env.uid, 'report_type': 'construction_cost',
            'frequency': 'weekly', 'next_run_on': self.today + timedelta(days=7)})
        schedule.action_run_now()
        if len(schedule.delivery_ids) != 1 or schedule.delivery_ids.state != 'generated':
            raise RecoveryRejected('The native demo report schedule did not generate its protected delivery.')
        schedule.action_pause()
        for key, record in [('cvr', cvr), ('construction_report', construction), ('cash_report', cash_report), ('report_schedule', schedule)]:
            self.save(prefix + key, record)
        self.save(prefix + 'report_delivery', schedule.delivery_ids)
        if not closed:
            policy = env['jakh.project.threshold.policy'].create({'name': 'DEMO ONLY — Schedule index below 0.90',
                'project_id': job['project'].id, 'owner_id': env.uid, 'metric': 'spi',
                'comparison': 'lt', 'threshold': 0.9, 'escalation_days': 3})
            policy.action_approve(); policy.action_evaluate()
            if len(policy.alert_ids) != 1 or policy.alert_ids.state != 'open':
                raise RecoveryRejected('The demo schedule variance did not create its native open exception.')
            self.save('threshold_policy', policy); self.save('threshold_alert', policy.alert_ids)
            self.stage('reporting', 'Cost, cash, reports and governed exceptions',
                ['cvr', 'construction_report', 'cash_forecast', 'cash_report', 'report_schedule', 'threshold_policy', 'threshold_alert'],
                ['CVR and building/BOQ report generated from linked source documents and posted actuals',
                 'Manual report run generated protected workbook delivery; weekly schedule paused for supervised testing',
                 'Approved SPI rule generated an open project exception'],
                ['Review forecast assumptions and the schedule exception; refresh reports after supervised workflow changes'])

    def playground(self):
        from openpyxl import Workbook
        from odoo.tools.binary import BinaryBytes
        env = self.actor_env('estimator')
        estimate = env['jakh.estimate'].create({'title': 'DEMO ONLY — Editable BOQ intake practice',
            'partner_id': self.customer.id, 'company_id': self.company.id, 'unit_count': 11,
            'indirect_amount': 2310, 'overhead_percent': 10, 'profit_percent': 20})
        batch = env['jakh.boq.import.batch'].create({'name': 'DEMO ONLY — Review three practice BOQ rows',
            'estimate_id': estimate.id})
        book = Workbook(); sheet = book.active; sheet.title = 'BOQ'
        sheet.append(['Code', 'Description', 'Quantity', 'Unit cost', 'Unit'])
        unit = self.scope.uom_id.with_context(lang='en_US').name
        for key, qty, cost in [('STRUCTURE', 10, 80), ('FINISHES', 20, 40), ('MEP', 1, 500)]:
            sheet.append(['PRACTICE-' + key, 'DEMO ONLY — ' + key.title(), qty, cost, unit])
        output = io.BytesIO(); book.save(output); book.close()
        env['jakh.boq.import.file'].create({'batch_id': batch.id, 'filename': 'DEMO-ONLY-practice-BOQ.xlsx',
            'upload': BinaryBytes(output.getvalue()), 'default_product_id': self.scope.id,
            'default_cost_code_id': self.codes['structure'].id, 'expected_direct_total': 23100})
        batch.action_preview()
        if batch.error_count or len(batch.row_ids) != 3 or estimate.line_ids:
            raise RecoveryRejected('The editable practice workbook did not produce a clean uncommitted preview.')
        self.save('draft_estimate', estimate); self.save('draft_boq_intake', batch)
        self.stage('playground', 'Editable estimating and reviewed workbook practice', ['draft_estimate', 'draft_boq_intake'],
            ['Three synthetic workbook rows previewed and reconciled without importing or freezing the draft'],
            ['Review preview, confirm reconciliation and commit into the draft',
             'Edit draft study, resource costs, repeated villas and variant quantities before approval'])

    def run(self):
        self.setup()
        active = self.award()
        self.stage('award', 'Opportunity to an approved 11-villa construction baseline',
            ['opportunity', 'estimate', 'sale_order', 'project', 'contract', 'wbs_structure', 'villa_01', 'villa_11', 'building_budget'],
            ['Opportunity linked to resource-based estimate: material, labour, equipment and subcontract costs',
             '11 villas include Villa 11 at +20% quantities; indirect pool, overhead and profit remain separate',
             'Approved version handed over once into confirmed quotation, active contract, released WBS and reviewed building budgets'])
        self.owner_advance(active)
        self.materials(active); self.subcontract(active); self.execution(active); self.assurance(active)
        certificate = self.certificate(active, 0.25, 'owner_certificate')
        self.certificate(active, 0.40, 'pending_certificate', pending=True)
        self.stage('owner_cash', 'Owner measured work, billing and collection',
            ['owner_advance', 'owner_advance_invoice', 'owner_certificate', 'owner_invoice', 'owner_payment', 'pending_certificate'],
            ['Owner advance posted and collected', '25% cumulative measurement independently reviewed through technical, commercial and finance stages',
             'Measured invoice posted, collected and reconciled; retention remains visible on the active contract'],
            ['Review the separate 40% cumulative certificate at technical stage'])
        self.reporting(active)
        self.playground()

        closed = self.award(closed=True)
        self.owner_advance(closed); self.subcontract(closed); self.execution(closed); self.assurance(closed)
        final_certificate = self.certificate(closed, 1, 'closed_owner_certificate')
        finance = self.actor_env('finance')
        release = finance['jakh.contract.cash.document'].create({'name': 'DEMO ONLY — Completed owner retention release',
            'contract_id': closed['contract'].id, 'kind': 'retention', 'amount': final_certificate.retention_amount,
            'reason': 'DEMO ONLY — Verified reference project completion and release conditions.'})
        release.action_submit(); release.action_approve(); release.action_create_invoice()
        self.post_and_settle(release.invoice_id, 'closed_owner_retention_invoice')
        self.save('closed_owner_retention', release)
        self.reporting(closed, closed=True)
        final = self.actor_env('director')['jakh.project.final.account'].create({'name': 'DEMO ONLY — Completed reference final account',
            'contract_id': closed['contract'].id, 'reason': 'DEMO ONLY — Source documents, posted costs, settlement and field evidence reviewed.'})
        final.action_submit(); final.action_approve(); final.action_close()
        if closed['contract'].state != 'closed' or closed['project'].active or not closed['project'].jakh_workflow_closed:
            raise RecoveryRejected('The completed reference did not pass native closeout controls.')
        self.save('closed_final_account', final)
        # Record final, rather than pre-close, states in the completion manifest.
        self.save('closed_contract', closed['contract']); self.save('closed_project', closed['project'])
        self.stage('closeout', 'Completed reference: final account and sealed project',
            ['closed_project', 'closed_contract', 'closed_owner_certificate', 'closed_owner_invoice',
             'closed_owner_retention', 'closed_cvr', 'closed_final_account'],
            ['Full BOQ measured and invoiced; owner and subcontract advances recovered',
             'Posted supplier bills and owner invoices fully reconciled; retentions released',
             'Accepted actual costs, completed field evidence and final account passed native closeout; project archived'])
        report = self.env['jakh.financial.report'].create({'company_id': self.company.id,
            'report_type': 'trial_balance', 'date_from': self.today, 'date_to': self.today})
        report.action_generate()
        if not self.company.currency_id.is_zero(sum(report.line_ids.mapped('balance'))):
            raise RecoveryRejected('The posted demonstration trial balance does not balance.')
        self.save('financial_report', report)
        self._check_actor_credentials()
        facts = self.env['jakh.project.control.entry'].search([('project_id', 'in', [active['project'].id, closed['project'].id]),
                                                              ('entry_type', '=', 'actual_cost')])
        if not facts or any(not row.account_move_line_id for row in facts):
            raise RecoveryRejected('Every demonstration actual cost must link to a posted ledger line.')
        if any(row.account_move_line_id.move_id.state != 'posted' or
               not self.company.currency_id.is_zero(row.amount - row.account_move_line_id.balance) for row in facts):
            raise RecoveryRejected('A demonstration actual cost differs from its posted source ledger line.')
        # All stages saved their records after the stage's final state. Actor
        # credentials and company/currency identity are separately rechecked.
        return {'schema': 1, 'scenario': SCENARIO, 'synthetic': True, 'status': 'complete',
            'customer': self.customer_id, 'database': self.database, 'company_id': self.company.id,
            'company_name': self.company.name, 'currency': self.company.currency_id.name,
            'created_at': datetime.now(timezone.utc).isoformat(), 'records': self.records, 'stages': self.stages,
            'actors': {role: {'id': user.id, 'name': user.name, 'login': user.login, 'login_enabled': False}
                       for role, user in self.actors.items()},
            'demo_accounting_setup': self.setup_changes, 'posted_move_ids': sorted(self.moves),
            'actual_cost_entry_ids': facts.ids,
            'summary': {'active_project': active['project'].id, 'closed_project': closed['project'].id,
                'active_contract_value': active['contract'].current_contract_value,
                'active_first_certificate_gross': certificate.period_gross, 'actual_cost_total': sum(facts.mapped('amount'))},
            'limitations': ['Synthetic training data only; no customer opening balances or live vendor connections.',
                'P6 and Jisr examples are local shaped messages, not vendor acceptance.',
                'Payments are native manual reconciliation simulations; no banking transport.',
                'No statutory tax/localization, engineering, regulatory, payroll or production acceptance.',
                'Saved reports are snapshots; refresh after supervised business changes.',
                'Reruns validate saved identities and credential safety and never reset user work.']}


def validate_saved(env, manifest, customer_id, database):
    _validate_manifest_header(manifest, customer_id, database, env.company.id)
    if manifest.get('currency') != env.company.currency_id.name:
        raise RecoveryRejected('The demo currency changed; no scenario was replayed.')
    for record in manifest['records'].values():
        if not isinstance(record, dict) or record.get('model') not in env or type(record.get('id')) is not int:
            raise RecoveryRejected('A saved demo record identity is malformed; no scenario was replayed.')
        row = env[record['model']].with_context(active_test=False).browse(record['id']).exists()
        if not row and env[record['model']]._transient:
            # Native report wizards expire. Their protected report delivery
            # remains persistent; never recreate workflow rows on a rerun.
            continue
        if len(row) != 1:
            raise RecoveryRejected('A saved demo source is missing; no scenario was replayed.')
        row.check_access('read')
        if 'company_id' in row._fields and row.company_id and row.company_id != env.company:
            raise RecoveryRejected('A saved demo source moved outside its synthetic company.')
        if 'currency_id' in row._fields and row.currency_id and row.currency_id != env.company.currency_id:
            raise RecoveryRejected('A saved demo source changed currency; no scenario was replayed.')
        if 'create_uid' in row._fields and row.create_uid.id != record.get('create_uid'):
            raise RecoveryRejected('A saved demo source identity changed; no scenario was replayed.')
        # State labels describe the original seed. Subsequent legitimate work
        # is retained, never reset or replayed on an idempotent rerun.
    actors = manifest.get('actors', {})
    if set(actors) != {'director', 'estimator', 'technical', 'project_manager', 'procurement', 'finance', 'quality'}:
        raise RecoveryRejected('Saved demo actor identities are incomplete.')
    checker = Demo(env, customer_id, database)
    checker.actors = {role: env['res.users'].browse(value['id']).exists() for role, value in actors.items()}
    if any(len(user) != 1 or user.company_id != env.company or user.login != actors[role].get('login')
           for role, user in checker.actors.items()):
        raise RecoveryRejected('A saved demo actor identity changed.')
    checker._check_actor_credentials()
    for move in env['account.move'].browse(manifest.get('posted_move_ids', [])).exists():
        checker.posted(move)
    if len(checker.moves) != len(manifest.get('posted_move_ids', [])):
        raise RecoveryRejected('A saved demo posted document is missing.')


def worker(root, customer_id):
    cfg = native_staging.settings(root)
    require_quiet_http(cfg)
    customer, _parser = validate_customer(cfg, customer_id)
    password = secret_file(private_path(Path(cfg['root']), Path(cfg['root']) / 'secrets/cluster', kind='file'))
    connection = _connect(cfg, password)
    try:
        verify_cluster(connection, cfg); require_quiet_databases(connection, cfg)
    finally:
        connection.close()
    import odoo
    from odoo import api
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', customer['config'], '-d', customer['database'],
                                   '--no-http', '--max-cron-threads=0'], setup_logging=True)
    with offline_only():
        registry = Registry(customer['database'])
        cursor = registry.cursor()
        try:
            cursor.execute('SELECT current_user, current_database(), r.rolsuper, r.rolcreatedb, '
                'r.rolcreaterole, r.rolinherit, pg_get_userbyid(d.datdba) '
                'FROM pg_roles r JOIN pg_database d ON d.datname=current_database() WHERE r.rolname=current_user')
            if cursor.fetchone() != (customer['role'], customer['database'], False, False, False, False, customer['role']):
                raise RecoveryRejected('The connected database must retain its dedicated unprivileged synthetic customer owner role.')
            # Read-only identity resolution, not a privileged business env.
            cursor.execute('SELECT id,company_id FROM res_users WHERE login=%s AND active', (customer['ownerLogin'],))
            identity = cursor.fetchone()
            if not identity or identity[0] == odoo.SUPERUSER_ID:
                raise RecoveryRejected('An active enrolled non-superuser owner is required.')
            env = api.Environment(cursor, identity[0], {**CONTEXT, 'allowed_company_ids': [identity[1]]})
            expected_country, expected_currency = ('SA', 'SAR') if customer_id == 'a' else ('AE', 'AED')
            if (env.su or not env.user.has_group('jakh_core.group_platform_owner')
                    or not env.user.has_group('jakh_core.group_manager')
                    or env.company.name != 'JAKH SYNTHETIC Customer %s — %s' % (customer_id.upper(), expected_country)
                    or env.company.country_id.code != expected_country or env.company.currency_id.name != expected_currency):
                raise RecoveryRejected('The enrolled owner must select the exact explicitly labelled synthetic company and country/currency.')
            installed = set(env['ir.module.module'].search([('name', 'in', list(REQUIRED_MODULES)), ('state', '=', 'installed')]).mapped('name'))
            if installed != REQUIRED_MODULES:
                raise RecoveryRejected('All construction walkthrough addons must already be installed offline.')
            # A transaction-scoped identity lock prevents another copy of this
            # script from creating the same fixed scenario concurrently.
            cursor.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', (104593, identity[1]))
            if cursor.fetchone() != (True,):
                raise RecoveryRejected('Another seed transaction owns this synthetic scenario.')
            parameters = env['ir.config_parameter']
            raw = parameters.get_str(MANIFEST_KEY, '')
            if raw:
                manifest = json.loads(raw)
                validate_saved(env, manifest, customer_id, customer['database'])
                cursor.rollback()
                result = 'existing'
            else:
                if parameters.get_bool(ENABLED_KEY, False):
                    raise RecoveryRejected('Demo enablement exists without a complete marker; nothing was overwritten.')
                # Refuse collisions before seeding rather than adopt unknown rows.
                if env['project.project'].with_context(active_test=False).search_count([('name', '=like', 'DEMO ONLY — %'), ('company_id', '=', env.company.id)]):
                    raise RecoveryRejected('Unmarked demo projects already exist; nothing was overwritten.')
                before_mail = set(env['mail.mail'].search([]).ids)
                def no_internal_commit(*args, **kwargs):
                    raise RecoveryRejected('Demo business actions must stay in the single seed transaction.')
                with patch.object(type(cursor), 'commit', no_internal_commit):
                    manifest = Demo(env, customer_id, customer['database']).run()
                    if set(env['mail.mail'].search([]).ids) != before_mail:
                        raise RecoveryRejected('Demo actions queued mail; the complete scenario was rolled back.')
                    parameters.set_str(MANIFEST_KEY, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
                    parameters.set_bool(ENABLED_KEY, True)
                # Pre-commit transport guard remains in force for callbacks.
                cursor.commit()
                result = 'created'
        except BaseException:
            cursor.rollback()
            raise
        finally:
            cursor.close()
    destination = _private_manifest(cfg, customer_id, manifest)
    print('Persistent synthetic construction demo %s; %s source records, no external delivery.' % (result, len(manifest['records'])))
    print('Private completion manifest: ' + str(destination))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--customer', choices=['a', 'b'], required=True)
    args = parser.parse_args()
    cfg = native_staging.settings(args.root)
    require_quiet_http(cfg)
    validate_customer(cfg, args.customer)
    if os.environ.get('JAKH_DEMO_SEED_CHILD') != '1':
        environment = native_staging.environment(cfg)
        environment['JAKH_DEMO_SEED_CHILD'] = '1'
        subprocess.run([cfg['python'], str(Path(__file__).resolve()), '--root', cfg['root'], '--customer', args.customer],
                       env=environment, cwd=cfg['source'], check=True)
        return
    # Reuse the staging metadata lock for this bounded offline operation. No
    # service is started or stopped, and no enrollment metadata is changed.
    lock_path = Path(cfg['root']) / '.staging.lock'
    if lock_path.is_symlink():
        raise RecoveryRejected('The synthetic staging lock cannot be linked.')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker(args.root, args.customer)


if __name__ == '__main__':
    main()
