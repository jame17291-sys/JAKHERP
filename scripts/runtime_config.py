"""Generate a private, single-customer Odoo configuration without printing secrets."""
import argparse
import os
from pathlib import Path
import re
import stat


def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value or ''):
        raise ValueError('Database/role identifier must be lowercase ASCII, at most 63 characters')
    return value


def single_line(value):
    value = str(value)
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Configuration values must contain no line breaks or NUL')
    return value


def secret_file(path, private=True):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (private and info.st_mode & 0o077):
        raise ValueError('Secret must be a regular, owner-only readable file')
    value = path.read_text().strip()
    if len(value) < 32 or value in ('admin', 'password'):
        raise ValueError('Secret must have at least 32 characters')
    return single_line(value)


def config_text(*, database, db_user, db_password, master_password, addons, data_dir,
                db_host='127.0.0.1', db_port=17816, http_host='127.0.0.1', http_port=8810,
                proxy_mode=False, workers=0, max_cron_threads=1):
    database, db_user = identifier(database), identifier(db_user)
    if database in ('postgres', 'template0', 'template1'):
        raise ValueError('A dedicated customer database is required, not a PostgreSQL system database')
    if db_user in ('postgres', 'jakh_cluster_admin', 'jakh_staging_cluster'):
        raise ValueError('The application must use a dedicated, non-superuser customer role')
    if len(db_password) < 32 or len(master_password) < 32:
        raise ValueError('Strong, separately supplied database and master secrets are required')
    if not 1024 <= int(http_port) <= 65535 or not 1 <= int(db_port) <= 65535:
        raise ValueError('Invalid port')
    values = {
        'addons_path': ','.join(single_line(p) for p in addons),
        'data_dir': data_dir, 'db_host': db_host, 'db_port': db_port,
        'db_user': db_user, 'db_password': db_password, 'db_name': database,
        'dbfilter': '^' + database + '$', 'list_db': False,
        'admin_passwd': master_password, 'http_interface': http_host,
        'http_port': http_port, 'proxy_mode': bool(proxy_mode), 'workers': int(workers),
        'max_cron_threads': int(max_cron_threads), 'with_demo': False, 'log_level': 'info',
        'limit_time_cpu': 120, 'limit_time_real': 240,
    }
    return '[options]\n' + ''.join(f'{key} = {single_line(value)}\n' for key, value in values.items())


def write_private(path, content):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(content)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--db-user', required=True)
    parser.add_argument('--db-password-file', type=Path, required=True)
    parser.add_argument('--master-password-file', type=Path, required=True)
    parser.add_argument('--addons', action='append', required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--db-host', default='127.0.0.1')
    parser.add_argument('--db-port', type=int, default=17816)
    parser.add_argument('--http-port', type=int, default=8810)
    args = parser.parse_args()
    write_private(args.destination, config_text(database=args.database, db_user=args.db_user,
        db_password=secret_file(args.db_password_file), master_password=secret_file(args.master_password_file),
        addons=args.addons, data_dir=args.data_dir.resolve(), db_host=args.db_host,
        db_port=args.db_port, http_port=args.http_port))
    print('Private configuration created; no credentials were printed.')


if __name__ == '__main__':
    main()
