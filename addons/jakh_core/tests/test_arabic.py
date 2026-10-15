"""Verify catalogs through the native loader, not a custom translation table."""
import re

from odoo.tests import TransactionCase, tagged
from odoo.tools.translate import code_translations


@tagged('post_install', '-at_install', 'jakh')
class TestJakhArabic(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('ar_001')
        modules = cls.env['ir.module.module'].search([
            ('name', '=like', 'jakh_%'), ('state', '=', 'installed')
        ])
        modules._update_translations(['ar_001'], overwrite=True)

    def assertArabic(self, text):
        self.assertRegex(text, re.compile('[\u0600-\u06ff]'))

    def test_dashboard_labels_and_notice_follow_native_language_context(self):
        english = self.env['jakh.dashboard'].with_context(lang='en_US').get_data()
        arabic = self.env['jakh.dashboard'].with_context(lang='ar_001').get_data()
        self.assertEqual(len(english['cards']), len(arabic['cards']))
        self.assertTrue(arabic['cards'])
        for original, translated in zip(english['cards'], arabic['cards']):
            self.assertEqual(original['action'], translated['action'])
            self.assertEqual(original['domain'], translated['domain'])
            self.assertEqual(original['count'], translated['count'])
            self.assertNotEqual(original['label'], translated['label'])
            self.assertArabic(translated['label'])
        self.assertArabic(arabic['notice'])

    def test_native_and_custom_menu_overrides_load_from_arabic_catalog(self):
        expected = {
            'jakh_core.menu_dashboard': 'لوحة',
            'jakh_core.menu_assurance': 'الجودة',
            'stock.menu_stock_root': 'المخازن والاستلام',
            'purchase.menu_purchase_root': 'المشتريات',
            'sale.sale_menu_root': 'المبيعات وعروض الأسعار',
            'contacts.menu_contacts': 'جهات الاتصال والبيانات الأساسية',
            'spreadsheet_dashboard.spreadsheet_dashboard_menu_root': 'لوحات المعلومات المحفوظة',
        }
        for xmlid, fragment in expected.items():
            record = self.env.ref(xmlid, raise_if_not_found=False)
            if record:
                self.assertIn(fragment, record.with_context(lang='ar_001').name, xmlid)

    def test_native_frontend_catalog_contains_dashboard_and_sidebar(self):
        for module, messages in {
            'jakh_core': ('Business & project overview', 'Open contributing records',
                          'Dashboard could not be loaded.'),
            'jakh_branding': ('Contracting & construction', 'Close navigation', 'Expand navigation'),
        }.items():
            if module not in self.env['ir.module.module']._installed():
                continue
            translated = {row['id']: row['string'] for row in
                          code_translations.get_web_translations(module, 'ar_001')['messages']}
            for message in messages:
                self.assertIn(message, translated)
                self.assertArabic(translated[message])
