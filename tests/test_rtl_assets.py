"""Synthetic checks only; never regenerate real attachments or open a database."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import regenerate_rtl_assets as rtl


class RtlAssetsTests(unittest.TestCase):
    def test_no_compiler_fails_before_any_process_or_database_work(self):
        with patch.object(rtl.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'No native RTL compiler'):
                rtl.compiler_health({})
            run.assert_not_called()

    def test_compiler_must_run_pinned_version_and_really_flip_native_css(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'odoo/addons/base/data/rtlcss.json'
            config.parent.mkdir(parents=True)
            config.write_text('{}')
            cfg = {'source': str(root), 'rtlcssBin': str(root / 'bin')}
            good = SimpleNamespace(stdout='.jakh_rtl_probe{direction:rtl;margin-right:7px;padding-left:11px;text-align:right;}')
            with patch.object(rtl, 'environment', return_value={'PATH': '/checked/bin'}), \
                    patch.object(rtl.subprocess, 'run', side_effect=[SimpleNamespace(stdout='rtlcss version: 4.3.0\n'), good]) as run:
                self.assertEqual(rtl.compiler_health(cfg)['nativeConversionProbe'], 'passed')
                self.assertEqual(run.call_args.args[0][-3:], ['-c', str(config), '-'])
                self.assertIn('direction:ltr', run.call_args.kwargs['input'])
            with patch.object(rtl, 'environment', return_value={}), \
                    patch.object(rtl.subprocess, 'run', return_value=SimpleNamespace(stdout='0.0.0\n')):
                with self.assertRaisesRegex(ValueError, 'version'):
                    rtl.compiler_health(cfg)
            with patch.object(rtl, 'environment', return_value={}), \
                    patch.object(rtl.subprocess, 'run', side_effect=[SimpleNamespace(stdout='4.3.0\n'), SimpleNamespace(stdout='direction:ltr')]):
                with self.assertRaisesRegex(ValueError, 'probe failed'):
                    rtl.compiler_health(cfg)

    def fake_env(self, *, active=True, names=None, errors=None):
        attachment, qweb, lang = MagicMock(), MagicMock(), MagicMock()
        lang.search_count.return_value = int(active)
        attachment.search_count.return_value = 8
        qweb.with_context.return_value = qweb
        qweb._get_bundles_to_pregenarate.return_value = (set(), names if names is not None else {'web.assets_web'}, set())
        bundle = SimpleNamespace(css_errors=errors or [], css=lambda: [SimpleNamespace(url='/web/assets/hash/web.assets_web.rtl.min.css')])
        qweb._get_asset_bundle.return_value = bundle
        return {'res.lang': lang, 'ir.attachment': attachment, 'ir.qweb': qweb}

    def test_regeneration_uses_native_asset_boundary_and_explicit_rtl_not_generic_pregenerator(self):
        env = self.fake_env()
        result = rtl.regenerate(env)
        env['ir.attachment'].search_count.assert_called_once_with(rtl.GENERATED_ASSET_DOMAIN)
        env['ir.attachment'].regenerate_assets_bundles.assert_called_once_with()
        env['ir.attachment'].unlink.assert_not_called()
        env['ir.qweb']._get_asset_bundle.assert_called_once_with('web.assets_web', css=True, js=False, rtl=True)
        env['ir.qweb']._pregenerate_assets_bundles.assert_not_called()
        self.assertEqual(result['rtlCssBundles'], 1)

    def test_missing_language_or_client_bundle_never_clears_assets(self):
        for env in (self.fake_env(active=False), self.fake_env(names={'another_bundle'})):
            with self.assertRaises(ValueError):
                rtl.regenerate(env)
            env['ir.attachment'].regenerate_assets_bundles.assert_not_called()

    def test_stylesheet_error_raises_for_enclosing_transaction_rollback(self):
        with self.assertRaisesRegex(ValueError, 'transaction must roll back'):
            rtl.regenerate(self.fake_env(errors=['synthetic compilation error']))


if __name__ == '__main__':
    unittest.main()
