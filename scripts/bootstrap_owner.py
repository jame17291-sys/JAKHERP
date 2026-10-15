"""Offline initialization of an explicitly supplied owner on a fresh Odoo database."""
import argparse
from pathlib import Path

from runtime_config import identifier, secret_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--login', required=True)
    parser.add_argument('--password-file', type=Path, required=True)
    args = parser.parse_args()
    identifier(args.database)
    if '@' not in args.login or any(c in args.login for c in '\n\r\x00'):
        raise ValueError('Explicit owner email is required')
    password = secret_file(args.password_file)
    import odoo
    from odoo import api
    from odoo.modules.registry import Registry
    odoo.tools.config.parse_config(['-c', str(args.config), '-d', args.database, '--no-http'], setup_logging=True)
    registry = Registry(args.database)
    with registry.cursor() as cursor:
        env = api.Environment(cursor, odoo.SUPERUSER_ID, {})
        owner = env.ref('base.user_admin')
        # Fresh-only guard: never rotate an existing real user's credentials implicitly.
        if owner.login != 'admin':
            raise ValueError('Refusing to modify an already-enrolled owner')
        owner.write({'login': args.login, 'email': args.login, 'name': 'Synthetic JAKH staging owner', 'password': password})
        for xmlid in ('base.group_system', 'jakh_core.group_platform_owner'):
            group = env.ref(xmlid, raise_if_not_found=False)
            if group:
                owner.write({'group_ids': [odoo.fields.Command.link(group.id)]})
        env['ir.config_parameter'].sudo().set_str('auth_signup.invitation_scope', 'b2b')
        env['ir.config_parameter'].sudo().set_str('database.uuid', __import__('uuid').uuid4().hex)
        cursor.commit()
    print('Fresh synthetic owner enrolled offline; no credentials were printed.')


if __name__ == '__main__':
    main()
