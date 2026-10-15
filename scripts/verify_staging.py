"""Read-only DB and HTTP tenant-isolation checks for the two synthetic staging customers."""
import argparse
import configparser
import json
from pathlib import Path

from native_staging import settings
from runtime_config import secret_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--http', action='store_true')
    args = parser.parse_args()
    cfg = settings(args.root)
    checks = {}
    authenticated_cookies = {}
    import psycopg2
    for key, customer in cfg['customers'].items():
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read(customer['config'])
        options = parsed['options']
        checks[f'{key}_fixed_database'] = options['db_name'] == customer['database'] and options['dbfilter'] == '^' + customer['database'] + '$' and options.getboolean('list_db') is False
        checks[f'{key}_private_http'] = options['http_interface'] == '127.0.0.1'
        checks[f'{key}_private_config'] = Path(customer['config']).stat().st_mode & 0o077 == 0
        connect = {'host': '127.0.0.1', 'port': cfg['pgPort'], 'user': customer['role'], 'password': options['db_password']}
        with psycopg2.connect(dbname=customer['database'], **connect) as connection:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user')
                checks[f'{key}_least_privilege_role'] = cursor.fetchone() == (False, False, False)
                cursor.execute("SELECT name FROM ir_module_module WHERE state='installed' AND name LIKE 'jakh_%' ORDER BY name")
                checks[f'{key}_all_seven_addons'] = {row[0] for row in cursor.fetchall()} == {'jakh_core', 'jakh_estimate', 'jakh_operations', 'jakh_assurance', 'jakh_migration', 'jakh_workflow', 'jakh_branding'}
                cursor.execute("SELECT count(*) FROM res_users WHERE login='admin' AND active")
                checks[f'{key}_default_admin_disabled'] = cursor.fetchone()[0] == 0
                cursor.execute('SELECT name FROM res_currency WHERE id=(SELECT currency_id FROM res_company ORDER BY id LIMIT 1)')
                checks[f'{key}_synthetic_currency'] = cursor.fetchone()[0] == ('SAR' if key == 'a' else 'AED')
        foreign = cfg['customers']['b' if key == 'a' else 'a']
        try:
            connection = psycopg2.connect(dbname=foreign['database'], **connect)
        except psycopg2.OperationalError:
            checks[f'{key}_foreign_database_denied'] = True
        else:
            connection.close()
            checks[f'{key}_foreign_database_denied'] = False
        if args.http:
            import requests
            url = f"http://127.0.0.1:{customer['httpPort']}"
            session = requests.Session()
            response = session.get(url + '/web/login', timeout=30)
            checks[f'{key}_login_page'] = response.status_code == 200
            payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {
                'db': customer['database'], 'login': customer['ownerLogin'],
                'password': secret_file(customer['ownerPasswordFile'])}}
            data = session.post(url + '/web/session/authenticate', json=payload, timeout=30).json()
            checks[f'{key}_native_login'] = bool(data.get('result', {}).get('uid')) and data.get('result', {}).get('db') == customer['database']
            authenticated_cookies[key] = session.cookies.copy()
            # Do not print cookie values, session data, passwords or error bodies.
            hostile = dict(payload, params=dict(payload['params'], db=foreign['database']))
            denied = session.post(url + '/web/session/authenticate', json=hostile, timeout=30).json()
            checks[f'{key}_http_foreign_database_denied'] = bool(denied.get('error')) and not denied.get('result', {}).get('uid')
            bad_password = dict(payload, params={'db': customer['database'], 'login': 'admin', 'password': 'admin'})
            denied = requests.post(url + '/web/session/authenticate', json=bad_password, timeout=30).json()
            checks[f'{key}_default_admin_login_denied'] = bool(denied.get('error')) and not denied.get('result', {}).get('uid')
            session.close()
    a, b = cfg['customers']['a'], cfg['customers']['b']
    checks['distinct_databases'] = a['database'] != b['database'] and a['role'] != b['role']
    checks['distinct_filestores_and_sessions'] = Path(a['dataDir']).resolve() != Path(b['dataDir']).resolve()
    if args.http:
        import requests
        for key, foreign_key in (('a', 'b'), ('b', 'a')):
            destination = cfg['customers'][foreign_key]
            response = requests.post(f"http://127.0.0.1:{destination['httpPort']}/web/session/get_session_info",
                json={'jsonrpc': '2.0', 'method': 'call', 'params': {}, 'id': 2},
                cookies=authenticated_cookies[key], timeout=30).json()
            checks[f'{key}_session_cannot_cross_customer'] = bool(response.get('error')) and not response.get('result', {}).get('uid')
    print(json.dumps({'ok': all(checks.values()), 'checks': checks}, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
