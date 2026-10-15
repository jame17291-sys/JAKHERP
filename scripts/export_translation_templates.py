"""Export the installed JAKH modules' native translation templates read-only.

The database transaction is explicitly read-only and always rolled back. Run
with the approved source on PYTHONPATH and a customer-specific configuration.
Generated POT files are build inputs, never an alternative translation runtime.
"""
import argparse
import io
from pathlib import Path

import odoo
from odoo import SUPERUSER_ID, api
from odoo.modules.registry import Registry
from odoo.tools.translate import trans_export


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--addons-path', help='Explicit reviewed addon paths when exporting a release before runtime activation')
    args = parser.parse_args()
    options = ['-c', args.config, '-d', args.database, '--no-http', '--max-cron-threads=0']
    if args.addons_path:
        options += ['--addons-path', args.addons_path]
    odoo.tools.config.parse_config(options, setup_logging=True)
    registry = Registry(args.database)
    with registry.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        env = api.Environment(cursor, SUPERUSER_ID, {'lang': 'en_US'})
        modules = env['ir.module.module'].search([
            ('name', '=like', 'jakh_%'), ('state', '=', 'installed')
        ]).mapped('name')
        args.output.mkdir(parents=True, exist_ok=True)
        for module in sorted(modules):
            buffer = io.BytesIO()
            trans_export(None, [module], buffer, 'po', env)
            (args.output / (module + '.pot')).write_bytes(buffer.getvalue())
            print(module, len(buffer.getvalue()))
        cursor.rollback()


if __name__ == '__main__':
    main()
