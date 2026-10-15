"""Label the isolated synthetic companies; optionally prepare one private, unactivated owner invitation."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from native_staging import environment, settings
from runtime_config import write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--customer', choices=['a', 'b'], required=True)
    parser.add_argument('--pending-owner-email')
    parser.add_argument('--arabic', action='store_true', help='Activate native Arabic alongside English in synthetic staging')
    args = parser.parse_args()
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    if not customer['initialized']:
        raise ValueError('Offline enrollment must precede synthetic company configuration')
    # This script is always executed with the explicitly configured isolated venv/source.
    if os.environ.get('JAKH_SYNTHETIC_CONFIG_CHILD') != '1':
        env = environment(cfg)
        env['JAKH_SYNTHETIC_CONFIG_CHILD'] = '1'
        command = [cfg['python'], str(Path(__file__).resolve()), '--root', cfg['root'], '--customer', args.customer]
        if args.pending_owner_email:
            command += ['--pending-owner-email', args.pending_owner_email]
        if args.arabic:
            command += ['--arabic']
        subprocess.run(command, env=env, cwd=cfg['source'], check=True)
        return
    import odoo
    from odoo import api, Command
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', customer['config'], '-d', customer['database'], '--no-http'], setup_logging=True)
    with Registry(customer['database']).cursor() as cursor:
        env = api.Environment(cursor, odoo.SUPERUSER_ID, {})
        country_code, currency_name = ('SA', 'SAR') if args.customer == 'a' else ('AE', 'AED')
        country = env['res.country'].search([('code', '=', country_code)], limit=1)
        currency = env['res.currency'].with_context(active_test=False).search([('name', '=', currency_name)], limit=1)
        if not country or not currency:
            raise ValueError('Standard country/currency records were not found')
        currency.active = True
        env.company.write({'name': f'JAKH SYNTHETIC Customer {args.customer.upper()} — {country_code}',
                           'country_id': country.id, 'currency_id': currency.id})
        if args.arabic:
            env['res.lang']._activate_lang('ar_001')
            modules = env['ir.module.module'].search([('state', '=', 'installed')])
            modules._update_translations(filter_lang='ar_001', overwrite=True)
        parameters = env['ir.config_parameter'].sudo()
        parameters.set_str('web.base.url', f"http://127.0.0.1:{customer['httpPort']}")
        parameters.set_bool('web.base.url.freeze', True)
        parameters.set_str('auth_signup.invitation_scope', 'b2b')
        dashboard = env.ref('jakh_core.action_dashboard')
        platform_group = env.ref('jakh_core.group_platform_owner')
        env['res.users'].search([('group_ids', 'in', [platform_group.id])]).write({'action_id': dashboard.id})
        if args.pending_owner_email:
            email = args.pending_owner_email.strip()
            if args.customer != 'a' or '@' not in email or any(c in email for c in '\r\n\x00'):
                raise ValueError('Explicit customer A pending owner email is required')
            if not hasattr(env['res.partner'], '_generate_signup_token'):
                raise ValueError('Native auth_signup is not installed; enrollment remains pending')
            if env['res.users'].with_context(active_test=False).search_count([('login', '=', email)]):
                raise ValueError('Refusing to overwrite or re-enroll an existing identity')
            user = env['res.users'].with_context(no_reset_password=True, mail_create_nolog=True,
                    mail_create_nosubscribe=True).create({'name': 'JAKH Owner — pending verified enrollment',
                    'login': email, 'email': email, 'company_id': env.company.id,
                    'company_ids': [Command.set([env.company.id])],
                    'group_ids': [Command.set([env.ref('jakh_core.group_platform_owner').id])]})
            cursor.execute('SELECT password IS NULL FROM res_users WHERE id=%s', (user.id,))
            if cursor.fetchone() != (True,):
                raise ValueError('Pending enrollment must not receive a stored password')
            user.partner_id.signup_prepare(signup_type='signup')
            user.action_id = dashboard
            token = user.partner_id._generate_signup_token(expiration=4)
            from urllib.parse import urlencode
            url = f"http://127.0.0.1:{customer['httpPort']}/web/signup?" + urlencode({'db': customer['database'], 'token': token})
            target = Path(cfg['root']) / 'secrets/pending-owner-invitation.json'
            write_private(target, json.dumps({'login': email, 'customer': args.customer, 'userId': user.id,
                'activationPending': True, 'expiresAfterHours': 4, 'privateInvitationUrl': url}, indent=2) + '\n')
        cursor.commit()
    print('Synthetic company labeled; country/currency selection is not statutory localization acceptance.')
    if args.pending_owner_email:
        print('Owner invitation prepared privately; no password was set, no email sent, activation remains pending.')


if __name__ == '__main__':
    main()
