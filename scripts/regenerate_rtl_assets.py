"""Health-check the real native RTL compiler, then regenerate staging assets.

Never change business attachments, language preferences, or application source.
This command is limited to the marker-validated isolated native staging tree.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess

from native_staging import PACKAGE, environment, settings


GENERATED_ASSET_DOMAIN = [
    ('public', '=', True), ('url', '=like', '/web/assets/%'),
    ('res_model', '=', 'ir.ui.view'), ('res_id', '=', 0), ('create_uid', '=', 1),
]


def compiler_health(cfg):
    """Do not trust a filename or a successful version command without conversion."""
    if not cfg.get('rtlcssBin'):
        raise ValueError('No native RTL compiler is configured; activate with --rtlcss-bin first')
    env = environment(cfg)
    compiler = Path(cfg['rtlcssBin']) / 'rtlcss'
    native_config = Path(cfg['source']) / 'odoo/addons/base/data/rtlcss.json'
    if not native_config.is_file():
        raise ValueError('The pinned native RTL compiler configuration is missing')
    expected = json.loads((PACKAGE / 'deploy/rtlcss/package.json').read_text())['jakhRtlcssVersion']
    version = subprocess.run([str(compiler), '--version'], env=env, check=True,
                             capture_output=True, text=True, timeout=20)
    if version.stdout.strip() not in (expected, f'rtlcss version: {expected}'):
        raise ValueError('The native RTL compiler version does not match the reviewed dependency')
    probe = '.jakh_rtl_probe{direction:ltr;margin-left:7px;padding-right:11px;text-align:left;}'
    result = subprocess.run([str(compiler), '-c', str(native_config), '-'], input=probe,
                            env=env, check=True, capture_output=True, text=True, timeout=20)
    compact = re.sub(r'\s+', '', result.stdout)
    if not all(term in compact for term in ('direction:rtl', 'margin-right:7px',
                                            'padding-left:11px', 'text-align:right')):
        raise ValueError('Native RTL conversion probe failed; existing asset cache is untouched')
    return {'version': expected, 'nativeConversionProbe': 'passed'}


def regenerate(env):
    """Use the framework's generated-assets boundary, then explicitly build RTL."""
    if not env['res.lang'].search_count([('code', '=', 'ar_001'), ('active', '=', True)]):
        raise ValueError('Arabic must already be activated through the native language model')
    qweb = env['ir.qweb'].with_context(lang='ar_001')
    _, css_bundles, _ = qweb._get_bundles_to_pregenarate()
    if 'web.assets_web' not in css_bundles:
        raise ValueError('Native web-client CSS bundle is missing; existing asset cache is untouched')
    generated_before = env['ir.attachment'].search_count(GENERATED_ASSET_DOMAIN)
    # The pinned method deletes only public /web/assets/ attachments belonging to
    # ir.ui.view, res_id=0, created by the framework superuser, and clears assets cache.
    env['ir.attachment'].regenerate_assets_bundles()
    urls = []
    for name in sorted(css_bundles):
        bundle = qweb._get_asset_bundle(name, css=True, js=False, rtl=True)
        attachments = bundle.css()
        if bundle.css_errors:
            raise ValueError(f'Native RTL stylesheet generation failed for {name}; transaction must roll back')
        for attachment in attachments:
            if '.rtl.' not in attachment.url or not attachment.url.startswith('/web/assets/'):
                raise ValueError('Native RTL bundle emitted an unexpected attachment URL')
            urls.append(attachment.url)
    return {'generatedAssetsReplaced': generated_before, 'rtlCssBundles': len(css_bundles), 'urls': urls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--customer', choices=['a', 'b'], required=True)
    parser.add_argument('--check-only', action='store_true', help='Compiler-only health check; never opens a database')
    args = parser.parse_args()
    cfg = settings(args.root)
    health = compiler_health(cfg)  # Must pass before any cache mutation or database connection.
    if args.check_only:
        print(json.dumps(health))
        return
    customer = cfg['customers'][args.customer]
    try:
        with socket.create_connection(('127.0.0.1', customer['httpPort']), timeout=1):
            raise ValueError('Stop this customer web service before offline RTL asset regeneration')
    except ConnectionRefusedError:
        pass
    if os.environ.get('JAKH_RTL_ASSETS_CHILD') != '1':
        child_env = environment(cfg)
        child_env['JAKH_RTL_ASSETS_CHILD'] = '1'
        subprocess.run([cfg['python'], str(Path(__file__).resolve()), '--root', cfg['root'],
                        '--customer', args.customer], env=child_env, cwd=cfg['source'], check=True)
        return
    import odoo
    from odoo import api
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', customer['config'], '-d', customer['database'], '--no-http'], setup_logging=True)
    with Registry(customer['database']).cursor() as cursor:
        env = api.Environment(cursor, odoo.SUPERUSER_ID, {'lang': 'ar_001'})
        result = regenerate(env)
        cursor.commit()
    print(json.dumps({**health, **result}))


if __name__ == '__main__':
    main()
