import io
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools.pdf import PdfReader

from ..models.pdf_renderer import (ENGINE, _chromium_binary, _offline_documents,
                                   _paper_spec, _render_chromium_documents)


@tagged('post_install', '-at_install', 'jakh')
class TestJakhOfflinePdf(TransactionCase):
    def _paper(self):
        return _paper_spec(SimpleNamespace(format='A4', orientation='Landscape', page_width=0, page_height=0,
            margin_top=32, margin_bottom=20, margin_left=8, margin_right=8))

    def test_external_resources_and_scripts_are_blocked_with_private_inline_html(self):
        source = '''<html><head><link href="https://outside.invalid/style.css"/><script>alert(1)</script></head><body>
            <div class="header">JAKH company</div><div class="article" data-oe-id="42"><div dir="rtl" class="page">
            <h2 onclick="steal()">التقرير</h2><img src="https://outside.invalid/image"/><img src="file:///private/example"/>
            <iframe src="https://outside.invalid/"/><table><tr><td>275</td></tr></table></div></div><div class="footer">Approved company footer</div></body></html>'''
        documents, ids = _offline_documents(source, self._paper())
        self.assertEqual(ids, [42]); self.assertEqual(len(documents), 1)
        result = documents[0].decode()
        self.assertNotIn('outside.invalid', result)
        self.assertNotIn('file:///private', result)
        self.assertNotIn('<script', result); self.assertNotIn('onclick', result)
        self.assertIn('Content-Security-Policy', result)
        self.assertIn("connect-src 'none'", result)
        self.assertIn('dir="rtl"', result)
        self.assertIn('size:297mm 210mm', result)

    def test_only_jakh_reports_select_fallback_and_invoice_engine_is_unchanged(self):
        engine = self.env['ir.actions.report']
        report = self.env.ref('jakh_operations.report_construction_control')
        from odoo.addons.base_report_wkhtmltox.models.ir_actions_report import IrActionsReport as NativeEngine
        with patch.object(NativeEngine, 'get_pdf_engine_state', return_value='install'), patch('odoo.addons.jakh_operations.models.pdf_renderer._chromium_binary', return_value='/approved/chromium'):
            self.assertEqual(engine._get_pdf_engine(report), ENGINE)
            invoice = self.env.ref('account.account_invoices')
            self.assertNotEqual(engine._get_pdf_engine(invoice), ENGINE)
            with self.assertRaises(AccessError):
                engine._run_pdf_engine(ENGINE, '<html/>', 'account.account_invoices')

    def test_static_bilingual_real_browser_produces_valid_landscape_pdf(self):
        binary = _chromium_binary()
        if not binary:
            self.skipTest('Approved Chromium binary is not installed on this test host.')
        source = '''<html><body><div class="header"><b>JAKH ERP</b> - Synthetic company</div>
            <div class="article"><div class="page" dir="rtl"><h2>تقرير المشروع</h2>
            <p>العملة: SAR | Project cost report</p><table><thead><tr><th>البند</th><th>التكلفة</th></tr></thead>
            <tbody><tr><td>أعمال الخرسانة</td><td>275.00 SAR</td></tr></tbody></table></div></div>
            <div class="footer">Synthetic company footer | تقرير تجريبي</div></body></html>'''
        documents, _ids = _offline_documents(source, self._paper())
        data = _render_chromium_documents(documents, binary)
        self.assertTrue(data.startswith(b'%PDF-'))
        reader = PdfReader(io.BytesIO(data), strict=False)
        self.assertEqual(len(reader.pages), 1)
        self.assertGreater(float(reader.pages[0].mediabox.width), float(reader.pages[0].mediabox.height))
        self.assertIn('275', reader.pages[0].extract_text())
        self.assertIn('JAKH ERP', reader.pages[0].extract_text())
        self.assertIn('Synthetic company footer', reader.pages[0].extract_text())
        self.assertEqual(reader.metadata.get('/Creator'), 'JAKH ERP')

    def test_native_qweb_report_keeps_source_access_and_prints_actual_pdf(self):
        if not _chromium_binary():
            self.skipTest('Approved Chromium binary is not installed on this test host.')
        manager = new_test_user(self.env, login='jakh.pdf.manager', groups='jakh_core.group_manager')
        technical = new_test_user(self.env, login='jakh.pdf.technical', groups='jakh_core.group_technical')
        financial = self.env.ref('jakh_core.group_financial')
        self.assertNotIn(financial, manager.group_ids)
        self.assertIn(financial, manager.all_group_ids)
        project = self.env['project.project'].with_user(manager).create({'name': 'Synthetic PDF project',
            'company_id': self.env.company.id, 'user_id': manager.id, 'privacy_visibility': 'employees'})
        report = self.env['jakh.construction.report'].with_user(manager).create({'project_id': project.id})
        if self.env['ir.actions.report']._get_pdf_engine(self.env.ref('jakh_operations.report_construction_control')) == ENGINE:
            action = self.env.ref('jakh_operations.report_construction_control').with_user(manager).report_action(report)
            self.assertEqual(action['type'], 'ir.actions.act_url')
            self.assertIn('/report/pdf/jakh_operations.construction_control_document/', action['url'])
        printer = self.env['ir.actions.report'].with_user(manager).with_context(force_report_rendering=True)
        data, output_format = printer._render_qweb_pdf('jakh_operations.report_construction_control', report.ids)
        self.assertEqual(output_format, 'pdf'); self.assertTrue(data.startswith(b'%PDF-'))
        self.assertIn('Synthetic PDF project', PdfReader(io.BytesIO(data), strict=False).pages[0].extract_text())
        with self.assertRaises(AccessError):
            printer.with_user(technical)._render_qweb_pdf('jakh_operations.report_construction_control', report.ids)
