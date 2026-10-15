"""Explicit, offline service-policy action on the marked synthetic staging only."""
import argparse
import os
from pathlib import Path
import socket
import subprocess

from native_staging import environment, settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--customer', choices=['a', 'b'], required=True)
    parser.add_argument('--requested-by', required=True)
    args = parser.parse_args()
    if '@' not in args.requested_by or any(c in args.requested_by for c in '\r\n\x00'):
        raise ValueError('An explicit requesting administrator identity is required')
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    try:
        with socket.create_connection(('127.0.0.1', customer['httpPort']), timeout=1):
            raise ValueError('Stop this customer HTTP service before offline configuration')
    except ConnectionRefusedError:
        pass
    if os.environ.get('JAKH_ENRICHMENT_POLICY_CHILD') != '1':
        env = environment(cfg)
        env['JAKH_ENRICHMENT_POLICY_CHILD'] = '1'
        subprocess.run([cfg['python'], str(Path(__file__).resolve()), '--root', cfg['root'],
                        '--customer', args.customer, '--requested-by', args.requested_by],
                       env=env, cwd=cfg['source'], check=True)
        return
    import odoo
    from odoo import api
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', customer['config'], '-d', customer['database'], '--no-http'], setup_logging=True)
    with Registry(customer['database']).cursor() as cursor:
        bootstrap = api.Environment(cursor, odoo.SUPERUSER_ID, {})
        actor = bootstrap['res.users'].search([('login', '=', args.requested_by)], limit=1)
        if actor and actor.has_group('base.group_system'):
            env = api.Environment(cursor, actor.id, {})
        else:
            # Customer B has only a synthetic administrator; do not create or
            # grant an account. Record the actual operator and requesting human.
            env = bootstrap
        before = env['crm.lead'].search_count([])
        env['res.users']._jakh_disable_automatic_enrichment(requested_by=args.requested_by)
        if env['crm.lead'].search_count([]) != before:
            raise ValueError('Lead record count changed unexpectedly; rolling back')
        assert env['ir.config_parameter'].sudo().get_str('crm.iap.lead.enrich.setting') == 'manual'
        assert not env.ref('crm_iap_enrich.ir_cron_lead_enrichment').active
        cursor.commit()
    print('Automatic lead enrichment disabled; cron inactive; existing lead records retained; immutable audit recorded.')


if __name__ == '__main__':
    main()
