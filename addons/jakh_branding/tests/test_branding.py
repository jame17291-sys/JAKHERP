import json
from io import BytesIO

from lxml import html
from PIL import Image

from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import BinaryBytes

from ..brand_assets import company_logo_png, icon_png


@tagged('post_install', '-at_install', 'jakh')
class BrandingModelTests(TransactionCase):
    def test_unified_modules_retain_quality_finance_and_one_dashboard(self):
        owner = new_test_user(self.env, 'jakh_unified_menu_owner', groups='jakh_core.group_platform_owner')
        menus = self.env['ir.ui.menu'].with_user(owner).load_menus(False)
        root = self.env.ref('jakh_core.menu_construction')
        assurance = self.env.ref('jakh_core.menu_assurance')
        self.assertEqual(assurance.parent_id, root)
        self.assertFalse(assurance.action)
        for xmlid in ('jakh_core.menu_dashboard', 'jakh_core.menu_reports',
                      'account.menu_finance', 'jakh_assurance.menu_cases',
                      'jakh_assurance.menu_documents', 'jakh_assurance.menu_revisions'):
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            if menu:
                self.assertIn(menu.id, menus)
        self.assertEqual(self.env.ref('account.menu_finance').parent_id, root)
        documents = self.env.ref('jakh_assurance.menu_documents', raise_if_not_found=False)
        if documents:
            self.assertEqual(documents.parent_id, assurance)

    def test_apps_navigation_branch_removed_without_uninstall_or_permission_changes(self):
        owner = new_test_user(self.env, 'jakh_menu_test_owner', groups='jakh_core.group_platform_owner')
        owner_groups = set(owner._get_group_ids())
        menu_model = self.env['ir.ui.menu'].with_user(owner)
        apps_menu = self.env.ref('base.menu_management')
        branch = menu_model.with_context(active_test=False).search([('id', 'child_of', apps_menu.id)])
        before = branch.read(['active', 'parent_id', 'action'])
        installed_before = self.env['ir.module.module'].search([('state', '=', 'installed')]).ids
        for debug in (False, True):
            with self.subTest(debug=debug):
                payload = menu_model.load_menus(debug)
                self.assertFalse(set(branch.ids).intersection(payload))
                for xmlid in ('jakh_core.menu_administration', 'base.menu_administration',
                              'base_setup.menu_config', 'base.menu_action_res_users'):
                    self.assertIn(self.env.ref(xmlid).id, set(payload), xmlid)
        self.assertEqual(branch.read(['active', 'parent_id', 'action']), before)
        self.assertEqual(self.env.ref('base.open_module_tree').res_model, 'ir.module.module')
        self.assertTrue(self.env['ir.module.module'].with_user(owner).has_access('read'))
        self.assertEqual(set(owner._get_group_ids()), owner_groups)
        self.assertEqual(self.env['ir.module.module'].search([('state', '=', 'installed')]).ids, installed_before)

    def test_company_logo_defaults_do_not_overwrite_customer_identity(self):
        company = self.env['res.company'].create({'name': 'Synthetic brand default test'})
        self.assertEqual(company.logo.content, company_logo_png())
        custom = BinaryBytes(icon_png(32), filename='synthetic-customer-logo.png')
        company.logo = custom
        company.email_secondary_color = '#123456'
        self.env['res.users']._jakh_apply_brand_defaults()
        self.assertEqual(company.logo.content, custom.content)
        self.assertEqual(company.email_secondary_color, '#123456')

    def test_portal_brand_promotion_and_notification_labels(self):
        rendered = self.env['ir.qweb']._render('web.brand_promotion_message', {})
        self.assertIn('JAKH ERP', str(rendered))
        self.assertNotIn('odoo.com', str(rendered))
        self.assertNotIn('Odoo', str(rendered))
        choices = dict(self.env['res.users']._fields['notification_type']._description_selection(self.env))
        self.assertEqual(choices['inbox'], 'In JAKH ERP')
        invitation = self.env.ref('auth_signup.set_password_email')
        self.assertIn('JAKH ERP', invitation.subject)
        self.assertNotIn('odoo.com', invitation.body_html)
        self.assertNotIn('Enjoy Odoo', invitation.body_html)
        self.assertEqual(self.env.ref('digest.digest_digest_default').name, 'Your JAKH ERP Periodic Digest')
        digest_arch = self.env.ref('digest.digest_mail_main')._get_combined_arch()
        self.assertFalse(digest_arch.xpath("//a[contains(@href,'odoo.com')]"))
        self.assertFalse(digest_arch.xpath("//t[@t-call='digest.digest_section_mobile']"))
        self.assertFalse(digest_arch.xpath("//tr[@t-foreach='tips']"))
        self.assertTrue(digest_arch.xpath("//a[contains(@t-attf-href,'/unsubscribe')]"))

    def test_settings_cleanup_preserves_security_and_hidden_provider_fields(self):
        arch = self.env.ref('base.res_config_settings_view_form')._get_combined_arch()
        for field in ('auth_totp_enforce', 'restrict_template_rendering', 'lead_enrich_auto', 'module_crm_iap_enrich'):
            self.assertTrue(arch.xpath(f"//field[@name='{field}']"), field)
        for widget in arch.xpath("//widget[@name='iap_buy_more_credits']"):
            self.assertEqual(widget.get('invisible'), '1')
        self.assertEqual(arch.xpath("//div[@id='iap_portal']")[0].get('invisible'), '1')
        self.assertTrue(arch.xpath("//setting[@id='auth_totp_policy']"))
        self.assertTrue(arch.xpath("//setting[@id='email_servers_setting']"))

    def test_reports_and_purchase_portal_keep_business_links_without_vendor_promotion(self):
        for xmlid in ('account.report_invoice_document', 'account.report_invoice_qr_code_preview'):
            arch = self.env.ref(xmlid)._get_combined_arch()
            self.assertFalse(arch.xpath("//img[@id='qrcode_odoo_logo']"))
            self.assertTrue(arch.xpath("//div[@id='qrcode_image']//img"))
        arch = self.env.ref('purchase.portal_my_purchase_order')._get_combined_arch()
        modal = arch.xpath("//div[@id='portal_connect_software_modal']")[0]
        self.assertFalse(modal.xpath(".//a[contains(@href, 'odoo.com')]"))
        self.assertNotIn('Odoo', ''.join(modal.itertext()))
        self.assertTrue(modal.xpath(".//input[@t-att-value='doc_url']"))
        self.assertTrue(modal.xpath(".//button[@id='copy-button']"))

    def test_sales_views_never_show_fictitious_sample_totals(self):
        for xmlid, root_tag in (
            ('sale.view_quotation_tree', 'list'), ('sale.view_sale_order_kanban', 'kanban'),
            ('sale.view_sale_order_graph', 'graph'), ('sale.view_sale_order_pivot', 'pivot'),
        ):
            arch = self.env.ref(xmlid)._get_combined_arch()
            self.assertEqual(arch.tag, root_tag)
            self.assertEqual(arch.get('sample'), '0')

    def test_disable_automatic_enrichment_changes_only_explicit_service_configuration(self):
        before = self.env['crm.lead'].search_count([])
        self.env['res.users']._jakh_disable_automatic_enrichment()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_str('crm.iap.lead.enrich.setting'), 'manual')
        self.assertFalse(self.env.ref('crm_iap_enrich.ir_cron_lead_enrichment').active)
        self.assertEqual(self.env['crm.lead'].search_count([]), before)
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.get_values()['lead_enrich_auto'], 'manual')

    def test_platform_icon_sizes_are_real_pngs(self):
        for size in (32, 180, 192, 512):
            with self.subTest(size=size), Image.open(BytesIO(icon_png(size))) as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.format, 'PNG')
        with self.assertRaises(ValueError):
            icon_png(999)


@tagged('post_install', '-at_install', 'jakh')
class BrandingHttpTests(HttpCase):
    def test_public_auth_offline_and_manifest_branding(self):
        for path in ('/web/login', '/web/reset_password', '/odoo/offline'):
            with self.subTest(path=path):
                response = self.url_open(path)
                self.assertEqual(response.status_code, 200)
                document = html.fromstring(response.content)
                visible = ' '.join(document.xpath('//body//text()[not(ancestor::script) and not(ancestor::style)]'))
                self.assertNotIn('Odoo', visible)
                self.assertNotIn('odoo.com', visible)
                self.assertIn('JAKH', response.text)
                self.assertTrue(document.xpath("//link[@rel='shortcut icon' and @href='/jakh/brand/icon/32.png?v=2']"))
        manifest = self.url_open('/web/manifest.webmanifest')
        self.assertEqual(manifest.status_code, 200)
        payload = json.loads(manifest.content)
        self.assertEqual(payload['name'], 'JAKH ERP')
        self.assertTrue(all(item['src'].startswith('/jakh/brand/icon/') for item in payload['icons']))
        self.assertEqual(self.url_open('/jakh/brand/icon/32.png').headers['Content-Type'], 'image/png')
        worker = self.url_open('/web/service-worker.js')
        self.assertEqual(worker.status_code, 200)
        self.assertIn('jakh-sw-cache-v2', worker.text)
        self.assertIn('user_logout', worker.text)
