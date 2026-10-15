import io
import json

from openpyxl import Workbook

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes


@tagged('post_install', '-at_install', 'jakh')
class TestReviewedBoqIntake(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='jakh.boq.manager', groups='jakh_core.group_manager')
        cls.technical = new_test_user(cls.env, login='jakh.boq.technical', groups='jakh_core.group_technical')
        cls.customer = cls.env['res.partner'].create({'name': 'Synthetic BOQ customer'})
        cls.product = cls.env['product.product'].create({'name': 'Synthetic BOQ scope', 'type': 'service', 'default_code': 'BOQ-TEST', 'taxes_id': [Command.clear()]})
        cls.code = cls.env['jakh.cost.code'].with_user(cls.manager).create({'code': 'BOQ-TEST', 'name': 'Synthetic code', 'company_id': cls.env.company.id})
        cls.env['product.pricelist'].create({'name': 'Reviewed BOQ company currency', 'currency_id': cls.env.company.currency_id.id, 'company_id': cls.env.company.id})

    def _estimate(self):
        return self.env['jakh.estimate'].with_user(self.manager).create({'title': 'Reviewed multi-file BOQ', 'partner_id': self.customer.id})

    def _xlsx(self, rows):
        book = Workbook(); sheet = book.active; sheet.title = 'BOQ'
        sheet.append(['Code', 'Description', 'Quantity', 'Unit cost', 'Unit', 'Parent code', 'Parent %', 'Group'])
        for row in rows:
            sheet.append(row)
        output = io.BytesIO(); book.save(output); book.close()
        return BinaryBytes(output.getvalue())

    def _file(self, batch, rows, **changes):
        return self.env['jakh.boq.import.file'].with_user(self.manager).create({'batch_id': batch.id, 'filename': 'synthetic.xlsx',
            'upload': self._xlsx(rows), 'default_product_id': self.product.id, 'default_cost_code_id': self.code.id, **changes})

    def _batch(self, estimate=None):
        return self.env['jakh.boq.import.batch'].with_user(self.manager).create({'estimate_id': (estimate or self._estimate()).id})

    def _row(self, code='B1', quantity=2, cost=10, **unused):
        return [code, 'Synthetic scope ' + code, quantity, cost, self.product.uom_id.with_context(lang='en_US').name]

    def test_multifile_preview_review_atomic_commit_and_retry(self):
        batch = self._batch()
        self._file(batch, [self._row('B1')], expected_direct_total=20)
        self._file(batch, [self._row('B2', 3, 20)], filename='second.xlsx', expected_direct_total=60)
        batch.action_preview()
        self.assertEqual(batch.error_count, 0)
        self.assertEqual(len(batch.row_ids), 2)
        self.assertEqual(batch.preview_json['combined_direct_total'], '80.00')
        self.assertFalse(batch.estimate_id.line_ids)
        with self.assertRaises(UserError):
            batch.action_commit()
        batch.write({'confirmed': True}); batch.action_review(); batch.action_commit(); batch.action_commit()
        self.assertEqual(len(batch.estimate_id.line_ids), 2)
        self.assertEqual(batch.estimate_id.cost_amount, 80)
        self.assertEqual(set(batch.estimate_id.line_ids.import_row_id.ids), set(batch.row_ids.ids))
        self.assertEqual(set(batch.row_ids.mapped('source_row')), {2})
        with self.assertRaises(UserError):
            batch.action_reset()
        with self.assertRaises(AccessError):
            batch.row_ids.write({'code': 'tamper'})

    def test_invalid_rows_formula_duplicates_and_no_partial_import(self):
        for rows in [[self._row('BAD', -1)], [self._row('BAD', 2, '=1+1')], [self._row('DUP'), self._row('DUP')]]:
            batch = self._batch(); self._file(batch, rows); batch.action_preview()
            self.assertGreater(batch.error_count, 0)
            self.assertFalse(batch.estimate_id.line_ids)
            with self.assertRaises(UserError):
                batch.action_review()
            with self.assertRaises(UserError):
                batch.action_commit()

    def test_stale_estimate_and_file_evidence_locks(self):
        batch = self._batch(); source = self._file(batch, [self._row()]); batch.action_preview()
        with self.assertRaises(UserError):
            source.write({'quantity_column': 'changed'})
        batch.confirmed = True; batch.action_review()
        batch.estimate_id.write({'header_markup_percent': 1})
        with self.assertRaises(UserError):
            batch.action_commit()
        self.assertFalse(batch.estimate_id.line_ids)
        batch.action_reset(); batch.action_preview(); batch.confirmed = True; batch.action_review(); batch.action_commit()
        self.assertEqual(len(batch.estimate_id.line_ids), 1)

    def test_percentage_group_import_pricing_and_native_handover(self):
        batch = self._batch(); unit = self.product.uom_id.with_context(lang='en_US').name
        self._file(batch, [['CAP', 'Parent value', 1, 100, unit, None, None, 'yes'],
            ['LEAF', 'Allocated work', 2, 0, unit, 'CAP', 100, 'no']], expected_direct_total=100)
        batch.estimate_id.write({'indirect_amount': 10, 'overhead_percent': 10, 'profit_percent': 20})
        self.assertEqual(batch.estimate_id.unallocated_indirect_amount, 10)
        self.assertEqual(batch.estimate_id.budget_amount, 0)
        with self.assertRaises(UserError):
            batch.estimate_id.action_submit()
        batch.action_preview(); self.assertFalse(batch.error_count)
        batch.confirmed = True; batch.action_review(); batch.action_commit()
        estimate = batch.estimate_id
        self.assertEqual(estimate.cost_amount, 100)
        self.assertEqual(estimate.budget_amount, 110)
        self.assertEqual(estimate.unallocated_indirect_amount, 0)
        self.assertAlmostEqual(estimate.sell_amount, 145.2)
        parent = estimate.line_ids.filtered('is_group')
        leaf = estimate.line_ids - parent
        self.assertEqual(leaf.parent_line_id, parent)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            leaf.write({'allocation_percent': 101})
        estimate.action_submit(); estimate.action_approve(); estimate.action_handover()
        self.assertEqual(len(estimate.project_id.jakh_wbs_ids), 1)
        self.assertEqual(estimate.project_id.jakh_wbs_ids.budget_amount, 110)
        self.assertTrue(estimate.project_id.account_id)
        self.assertEqual(len(estimate.sale_order_id.order_line), 1)
        if 'commercial_contract_id' in estimate._fields:
            contract = estimate.commercial_contract_id
            self.assertEqual(contract.line_ids.original_cost_total, 100)
            self.assertEqual(contract.line_ids.original_budget_total, 110)
            self.assertEqual(contract.line_ids.source_line_uuid, leaf.line_uuid)
            for field, value in [('original_direct_unit_cost', 1), ('original_cost_total', 1),
                                 ('original_budget_total', 1), ('source_line_uuid', 'forged')]:
                with self.subTest(field=field), self.assertRaises(AccessError):
                    contract.line_ids.write({field: value})
            with self.assertRaises(ValidationError):
                self.env['jakh.project.contract.line'].with_user(self.manager).create({
                    'contract_id': contract.id, 'code': 'FORGED', 'name': 'Forged imported cost',
                    'source_line_uuid': 'forged', 'quantity': 2, 'unit_price': 72.6,
                    'wbs_id': estimate.project_id.jakh_wbs_ids.id, 'uom_id': leaf.uom_id.id,
                    'original_direct_unit_cost': 1, 'original_cost_total': 1, 'original_budget_total': 1})
        revision = self.env['jakh.estimate'].with_user(self.manager).browse(estimate.action_new_revision()['res_id'])
        revised_leaf = revision.line_ids.filtered(lambda row: not row.is_group)
        self.assertEqual(revised_leaf.parent_line_id.estimate_id, revision)
        revision.action_submit(); revision.action_approve()
        with self.assertRaises(UserError):
            revision.action_handover()

    def test_intake_role_and_forged_provenance_are_denied(self):
        batch = self._batch()
        with self.assertRaises(AccessError):
            batch.with_user(self.technical).read(['preview_text'])
        with self.assertRaises(AccessError):
            self.env['jakh.boq.import.row'].with_user(self.manager).create({'batch_id': batch.id})
        with self.assertRaises(AccessError):
            batch.write({'state': 'committed'})

    def test_named_mapping_and_total_reconciliation_errors(self):
        batch = self._batch()
        source = self._file(batch, [self._row()], quantity_column='Missing quantity')
        batch.action_preview(); self.assertGreater(batch.error_count, 0)
        batch.action_reset(); source.write({'quantity_column': 'Quantity', 'expected_direct_total': 999})
        batch.action_preview(); self.assertGreater(batch.error_count, 0)
        self.assertFalse(batch.estimate_id.line_ids)

    def test_finite_numeric_inputs_and_before_after_audit(self):
        estimate = self._estimate()
        for field in ['indirect_amount', 'overhead_percent', 'profit_percent']:
            for number in [float('nan'), float('inf'), -1, True]:
                with self.subTest(field=field, number=number), self.assertRaises(ValidationError):
                    estimate.write({field: number})
        estimate.write({'profit_percent': 15})
        event = self.env['jakh.audit.event'].search([('resource_model', '=', 'jakh.estimate'),
            ('resource_id', '=', estimate.id), ('operation', '=', 'document.edited')], order='id desc', limit=1)
        self.assertEqual(json.loads(event.details)['changes']['profit_percent'], {'before': 0, 'after': 15})

    def test_arabic_estimate_and_intake_use_native_catalogs(self):
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module'].search([('name', '=', 'jakh_estimate'), ('state', '=', 'installed')])._update_translations(['ar_001'], overwrite=True)
        estimate = self._estimate().with_context(lang='ar_001')
        self.assertRegex(estimate.pricing_basis, '[\u0600-\u06ff]')
        self.assertRegex(self.env.ref('jakh_estimate.menu_boq_intake').with_context(lang='ar_001').name, '[\u0600-\u06ff]')
        fields_info = self.env['jakh.estimate'].with_context(lang='ar_001').fields_get(['overhead_percent', 'profit_percent'])
        for info in fields_info.values():
            self.assertRegex(info['string'], '[\u0600-\u06ff]')
