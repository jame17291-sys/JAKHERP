"""Operate only a newly-created, isolated, localhost synthetic Odoo staging cluster."""
import argparse
import configparser
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import sys

from runtime_config import config_text, write_private

PACKAGE = Path(__file__).resolve().parents[1]


def _default_work_root(package):
    """Keep legacy workspace state in place; standalone checkouts own work/."""
    package = Path(package)
    if package.parent.name == 'outputs':
        return package.parents[1] / 'work'
    return package / 'work'


WORK_ROOT = _default_work_root(PACKAGE)
MARKER = 'jakh-isolated-native-staging-v1'


def checked(command, **kwargs):
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def settings(root):
    root = Path(root).resolve(strict=True)
    if root == WORK_ROOT.resolve() or not root.is_relative_to(WORK_ROOT.resolve()):
        raise ValueError('Native staging is restricted to a specific child of this workspace work/')
    marker = root / 'staging.json'
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('Not an initialized isolated staging directory')
    config = json.loads(marker.read_text())
    if config.get('marker') != MARKER or Path(config.get('root', '')).resolve() != root:
        raise ValueError('Staging marker mismatch')
    return config


def update_customer_state(root, customer_id, *, initialized=None, modules=(), config=None):
    """Merge only this customer's enrollment metadata under an atomic local lock."""
    cfg = settings(root)
    directory = Path(cfg['root'])
    lock_fd = os.open(directory / '.staging.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = settings(directory)
        customer = cfg['customers'][customer_id]
        if initialized is not None:
            customer['initialized'] = initialized
        if config is not None:
            candidate = Path(config)
            config = candidate.resolve(strict=True)
            if candidate.is_symlink() or not config.is_relative_to(directory) or not config.is_file():
                raise ValueError('Customer configuration must be a private file in this staging directory')
            customer['config'] = str(config)
        customer['installedModules'] = sorted(set(customer.get('installedModules', [])) | set(modules))
        temporary = directory / ('staging-' + secrets.token_hex(12) + '.json')
        write_private(temporary, json.dumps(cfg, indent=2) + '\n')
        os.replace(temporary, directory / 'staging.json')


def pg_start(cfg):
    pg = Path(cfg['postgres']) / 'bin'
    options = f"-p {cfg['pgPort']} -h 127.0.0.1 -k {shlex.quote(cfg['socket'])}"
    checked([pg / 'pg_ctl', '-D', Path(cfg['root']) / 'pgdata', '-l', Path(cfg['root']) / 'postgres.log',
             '-o', options, '-w', 'start'])


def initialization_source(args):
    """Select one validated source/snapshot for a wholly unenrolled cluster.

    Generated snapshots use their own vendor and JAKH addons, never live PACKAGE
    addons. Their initialization-only metadata is appended by offline install,
    not by the persistent serving configuration. Full pinned source remains a
    separate synthetic-development option.
    """
    cfg = settings(args.root)
    if any(customer['initialized'] for customer in cfg['customers'].values()):
        raise ValueError('Initialization source selection is restricted to an unenrolled synthetic cluster')
    source = args.source.resolve(strict=True)
    if not source.is_relative_to(WORK_ROOT.resolve()) or not (source / 'odoo/__main__.py').is_file():
        raise ValueError('Use the verified source under this workspace work/')
    addon_paths = snapshot_addons(source) or [source / 'odoo/addons', PACKAGE / 'addons']
    for key, customer in cfg['customers'].items():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(customer['config'])
        parser['options']['addons_path'] = ','.join(str(path) for path in addon_paths)
        output = io.StringIO(); parser.write(output)
        token = hashlib.sha256(output.getvalue().encode()).hexdigest()[:12]
        destination = Path(cfg['root']) / ('customer-' + key + '-initialization-' + token + '.conf')
        if destination.exists():
            if destination.is_symlink() or destination.read_text() != output.getvalue():
                raise ValueError('A different initialization configuration already exists; refusing to overwrite it')
        else:
            write_private(destination, output.getvalue())
        customer['config'] = str(destination)
    cfg['source'] = str(source)
    temporary = Path(cfg['root']) / ('bootstrap-source-' + secrets.token_hex(12) + '.json')
    write_private(temporary, json.dumps(cfg, indent=2) + '\n')
    os.replace(temporary, Path(cfg['root']) / 'staging.json')
    print('Fresh-cluster bootstrap source selected. Existing ERP and source files were not changed.')


def prepare(args):
    root = args.root.absolute()
    # Require a new child below an existing work directory, never the work directory itself.
    if root.exists() or root.is_symlink() or not root.parent.resolve(strict=True).is_relative_to(WORK_ROOT.resolve()):
        raise ValueError('Choose a new, non-existing child directory under workspace work/')
    source, postgres = args.source.resolve(strict=True), args.postgres.resolve(strict=True)
    if not (source / 'odoo/__main__.py').is_file() or not (postgres / 'bin/initdb').is_file():
        raise ValueError('Verified Odoo source and portable PostgreSQL prefix are required')
    addon_paths = snapshot_addons(source) or [source / 'odoo/addons', PACKAGE / 'addons']
    root.mkdir(mode=0o700)
    socket = root / 'socket'
    socket.mkdir(mode=0o700)
    secret_dir = root / 'secrets'
    secret_dir.mkdir(mode=0o700)
    secret_values = {name: secrets.token_urlsafe(48) for name in ('cluster', 'a-db', 'b-db', 'a-master', 'b-master', 'a-owner', 'b-owner')}
    for name, value in secret_values.items():
        write_private(secret_dir / name, value + '\n')
    cfg = {'marker': MARKER, 'root': str(root), 'source': str(source), 'postgres': str(postgres),
           # Preserve the venv executable path: resolving its symlink would silently select base Python.
           'python': str(args.python.absolute()), 'pgPort': args.pg_port,
           'socket': str(socket), 'customers': {}}
    pg = postgres / 'bin'
    checked([pg / 'initdb', '-D', root / 'pgdata', '-U', 'jakh_staging_cluster',
             '--auth-local=scram-sha-256', '--auth-host=scram-sha-256',
             '--pwfile', secret_dir / 'cluster', '--encoding=UTF8', '--locale=C', '--data-checksums'])
    # No existing cluster is targeted. Database port is private and separate from all current apps.
    write_private(root / 'staging.json', json.dumps(cfg, indent=2) + '\n')
    pg_start(cfg)
    import psycopg2
    from psycopg2 import sql
    connection = psycopg2.connect(host='127.0.0.1', port=args.pg_port, user='jakh_staging_cluster',
                                 password=secret_values['cluster'], dbname='postgres')
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            if not 1024 <= args.http_base_port <= 65534:
                raise ValueError('Choose two adjacent non-privileged staging HTTP ports')
            for key, http_port in (('a', args.http_base_port), ('b', args.http_base_port + 1)):
                role, database = f'jakh_customer_{key}', f'jakh_staging_{key}'
                cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %s').format(sql.Identifier(role)), (secret_values[f'{key}-db'],))
                cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(database), sql.Identifier(role)))
                cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
                data_dir = root / f'customer-{key}'
                data_dir.mkdir(mode=0o700)
                config_path = root / f'customer-{key}.conf'
                write_private(config_path, config_text(database=database, db_user=role,
                    db_password=secret_values[f'{key}-db'], master_password=secret_values[f'{key}-master'],
                    addons=addon_paths, data_dir=data_dir,
                    db_port=args.pg_port, http_port=http_port, max_cron_threads=0))
                cfg['customers'][key] = {'database': database, 'role': role, 'config': str(config_path),
                    'dataDir': str(data_dir), 'httpPort': http_port,
                    'ownerLogin': 'owner@jakh-staging.test' if key == 'a' else 'owner-b@jakh-staging.test',
                    'ownerPasswordFile': str(secret_dir / f'{key}-owner'), 'initialized': False}
    finally:
        connection.close()
    # Marker contains paths, not secret values. Replace only our own freshly-created marker.
    with (root / 'staging.json').open('w') as stream:
        json.dump(cfg, stream, indent=2)
    print('Fresh synthetic cluster and two separately permissioned databases prepared. No credentials printed.')


def environment(cfg):
    env = os.environ.copy()
    env['PYTHONPATH'] = cfg['source']
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    if cfg.get('rtlcssBin'):
        compiler = Path(cfg['rtlcssBin']) / 'rtlcss'
        if not compiler.is_file() or not os.access(compiler, os.X_OK):
            raise ValueError('Configured native RTL stylesheet compiler is unavailable')
        env['PATH'] = str(compiler.parent) + os.pathsep + env.get('PATH', '')
    return env


def offline_odoo(cfg, customer, arguments, label):
    # Fail closed before any schema update if the customer web service is active.
    try:
        with socket.create_connection(('127.0.0.1', customer['httpPort']), timeout=1):
            raise ValueError('Stop this isolated customer web service before offline initialization, module updates or tests')
    except ConnectionRefusedError:
        pass
    log = Path(cfg['root']) / (label + '-' + secrets.token_hex(4) + '.log')
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    print('Offline initialization/test log: ' + str(log), flush=True)
    with os.fdopen(descriptor, 'w') as stream:
        checked([cfg['python'], '-m', 'odoo', '-c', customer['config'], '-d', customer['database'],
                 '--without-demo', '--no-http', '--max-cron-threads=0', '--stop-after-init', *arguments],
                env=environment(cfg), cwd=cfg['source'], stdout=stream, stderr=subprocess.STDOUT)


def install(args):
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    if customer['initialized']:
        raise ValueError('This command initializes a fresh database only; existing databases are never overwritten')
    arguments = ['-i', args.modules]
    addon_paths = snapshot_addons(cfg['source'], initialization=True)
    if addon_paths:
        # The override exists only for this offline initialization process. A
        # legacy fresh config may point at live PACKAGE code; write a new private
        # snapshot serving config instead of overwriting it or mixing releases.
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(customer['config'])
        serving_paths = ','.join(str(path) for path in addon_paths[:-1])
        if parser['options']['addons_path'] != serving_paths:
            parser['options']['addons_path'] = serving_paths
            output = io.StringIO(); parser.write(output)
            path = Path(cfg['root']) / ('customer-' + args.customer + '-snapshot-enrollment-' + secrets.token_hex(6) + '.conf')
            write_private(path, output.getvalue())
            customer = {**customer, 'config': str(path)}
        arguments.extend(['--addons-path', ','.join(str(path) for path in addon_paths)])
    offline_odoo(cfg, customer, arguments, 'install-' + args.customer)
    # The default admin login/password is replaced offline, BEFORE any HTTP listener may start.
    checked([cfg['python'], PACKAGE / 'scripts/bootstrap_owner.py', '--config', customer['config'],
             '--database', customer['database'], '--login', customer['ownerLogin'],
             '--password-file', customer['ownerPasswordFile']], env=environment(cfg), cwd=cfg['source'])
    update_customer_state(args.root, args.customer, initialized=True, modules=args.modules.split(','),
                          config=customer['config'] if addon_paths else None)
    print('Synthetic owner credentials replaced offline; customer may now be served on localhost.')


def add_modules(args):
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    if not customer['initialized']:
        raise ValueError('Enroll the fresh customer first')
    offline_odoo(cfg, customer, ['-i', args.modules], 'modules-' + args.customer)
    update_customer_state(args.root, args.customer, modules=args.modules.split(','))
    print('Modules installed offline; owner credentials were not changed.')


def test_modules(args):
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    if not customer['initialized']:
        raise ValueError('Enroll the synthetic customer first')
    # Odoo's test harness can start its own HTTP service despite --no-http.
    # Never let an existing preview tab attach to the test registry.
    test_port = customer['httpPort'] + 10000
    arguments = ['-u', args.modules, '--test-enable', '--test-tags', 'jakh', '--http-port', str(test_port)]
    if getattr(args, 'overwrite_translations', False):
        # Explicit release operation only; ordinary updates preserve customer terms.
        if any(not module.startswith('jakh_') for module in args.modules.split(',')):
            raise ValueError('Translation overwrite is restricted to the requested JAKH addons')
        arguments.append('--i18n-overwrite')
    offline_odoo(cfg, customer, arguments, 'tests-' + args.customer)
    print('Odoo tagged test process completed; inspect the private log for test counts and assertions.')


def serve(args):
    cfg = settings(args.root)
    customer = cfg['customers'][args.customer]
    if not customer['initialized']:
        raise ValueError('Refusing HTTP before offline owner credential initialization')
    os.chdir(cfg['source'])
    os.execve(cfg['python'], [cfg['python'], '-m', 'odoo', '-c', customer['config'],
                            '--max-cron-threads', str(args.cron_threads)], environment(cfg))


def require_stopped_customers(cfg):
    for customer in cfg['customers'].values():
        try:
            with socket.create_connection(('127.0.0.1', customer['httpPort']), timeout=1):
                raise ValueError('Stop BOTH isolated customer web services before changing runtime paths')
        except ConnectionRefusedError:
            pass


def validate_runtime(directory, installed):
    """Refuse incomplete/tampered output before changing any staging paths."""
    directory = Path(directory).resolve(strict=True)
    if directory == WORK_ROOT.resolve() or not directory.is_relative_to(WORK_ROOT.resolve()):
        raise ValueError('Runtime must be a specific child below this workspace work/')
    marker = directory / 'runtime-manifest.json'
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('A generated lean runtime manifest is required')
    manifest = json.loads(marker.read_text())
    if manifest.get('schema') != 1 or manifest.get('languages') != ['ar_001', 'en_US']:
        raise ValueError('Unsupported lean runtime manifest')
    if not set(installed).issubset(manifest.get('includedModules', [])):
        raise ValueError('Runtime does not contain every currently installed addon')
    seen = set()
    for record in manifest['files']:
        relative = Path(record['path'])
        if not relative.parts or relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in ('vendor', 'addons', 'bootstrap_addons') or relative in seen:
            raise ValueError('Unsafe or duplicate runtime manifest path')
        seen.add(relative)
        file = directory / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(directory):
            raise ValueError('A runtime file is missing or linked outside its package')
        content = file.read_bytes()
        if len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
            raise ValueError('A runtime file has changed since packaging; rebuild the runtime')
    actual_files = set()
    for file in directory.rglob('*'):
        if file.is_symlink():
            raise ValueError('Runtime contains an unreviewed symbolic link')
        if file.is_file() and file != marker:
            actual_files.add(file.relative_to(directory))
    if actual_files != seen:
        raise ValueError('Runtime file set differs from its reviewed manifest; rebuild the runtime')
    if not (directory / 'vendor/odoo/__main__.py').is_file():
        raise ValueError('Runtime framework entry point is missing')
    from build_lean_runtime import discover_addons, dependency_closure
    addons = discover_addons([directory / 'vendor/odoo/addons', directory / 'addons'])
    if set(addons) != set(manifest['includedModules']):
        raise ValueError('Runtime addon manifests differ from the included-module inventory')
    dependency_closure(installed, addons)  # Verify real manifests, not inventory labels.
    from runtime_bootstrap import validate_bootstrap_metadata
    validate_bootstrap_metadata(directory, manifest)
    return directory


def snapshot_addons(source, *, initialization=False):
    """Use one validated snapshot for init, owner enrollment and ordinary serve."""
    source = Path(source).resolve(strict=True)
    runtime = source.parent
    marker = runtime / 'runtime-manifest.json'
    if not marker.exists() and not marker.is_symlink():
        return None  # Full verified source is still supported for synthetic work.
    if source.name != 'vendor' or marker.is_symlink() or not marker.is_file():
        raise ValueError('A generated runtime must use its own vendor entry point')
    report = json.loads(marker.read_text())
    runtime = validate_runtime(runtime, report.get('includedModules', []))
    paths = [runtime / 'vendor/odoo/addons', runtime / 'addons']
    if initialization:
        from runtime_bootstrap import validate_bootstrap_metadata
        paths.append(validate_bootstrap_metadata(runtime, report, required=True))
    return paths


def change_runtime(args):
    """Atomically choose new configs; preserve prior source/configs for rollback."""
    cfg = settings(args.root)
    directory = Path(cfg['root'])
    lock_fd = os.open(directory / '.staging.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = settings(directory)
        require_stopped_customers(cfg)
        if args.action == 'restore-runtime':
            previous = cfg.get('runtimeRollback')
            if not previous:
                raise ValueError('No previous runtime paths have been saved')
            if not (Path(previous['source']) / 'odoo/__main__.py').is_file():
                raise ValueError('Previous source is unavailable; restoration aborted')
            if any(not Path(path).is_file() for path in previous['configs'].values()):
                raise ValueError('Previous private configuration is unavailable; restoration aborted')
            cfg['source'] = previous['source']
            for key, path in previous['configs'].items():
                cfg['customers'][key]['config'] = path
            for field in ('activeRuntime', 'runtimeRollback', 'rtlcssBin', 'rtlcssReady'):
                if field in previous:
                    cfg[field] = previous[field]
                else:
                    cfg.pop(field, None)
        else:
            from build_lean_runtime import native_inventory
            inventory = native_inventory(directory)
            runtime = validate_runtime(args.runtime, inventory['installedModules'])
            rtl_bin = getattr(args, 'rtlcss_bin', None) or cfg.get('rtlcssBin')
            if not rtl_bin and not getattr(args, 'allow_missing_rtlcss', False):
                raise ValueError('Arabic runtime activation requires --rtlcss-bin with the native RTL stylesheet compiler')
            candidate = dict(cfg)
            if rtl_bin:
                candidate['source'] = str(runtime / 'vendor')
                candidate['rtlcssBin'] = str(Path(rtl_bin).resolve(strict=True))
                environment(candidate)  # Validate compiler before writing any private config.
                from regenerate_rtl_assets import compiler_health
                compiler_health(candidate)  # Actual version + native LTR-to-RTL conversion, never existence alone.
            previous = {'source': cfg['source'], 'configs': {key: value['config'] for key, value in cfg['customers'].items()},
                        **{field: cfg[field] for field in ('activeRuntime', 'runtimeRollback', 'rtlcssBin', 'rtlcssReady') if field in cfg}}
            token = secrets.token_hex(6)
            for key, customer in cfg['customers'].items():
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(customer['config'])
                parser['options']['addons_path'] = ','.join(str(runtime / path) for path in ('vendor/odoo/addons', 'addons'))
                output = io.StringIO()
                parser.write(output)
                path = directory / f'customer-{key}-runtime-{token}.conf'
                write_private(path, output.getvalue())
                customer['config'] = str(path)
            cfg['runtimeRollback'] = previous
            cfg['activeRuntime'] = str(runtime)
            cfg['source'] = str(runtime / 'vendor')
            if rtl_bin:
                cfg['rtlcssBin'] = candidate['rtlcssBin']
            cfg['rtlcssReady'] = bool(rtl_bin)
        temporary = directory / ('staging-runtime-' + secrets.token_hex(12) + '.json')
        write_private(temporary, json.dumps(cfg, indent=2) + '\n')
        os.replace(temporary, directory / 'staging.json')
    print('Runtime paths updated offline. Previous source and private configurations remain intact; no database was changed.')
    if args.action == 'activate-runtime' and not cfg.get('rtlcssReady'):
        print('WARNING: explicit STAGING-ONLY override used. Native Arabic RTL asset generation is NOT ready; do not approve Arabic UI or production readiness.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'install', 'add-modules', 'test', 'serve', 'pg-start', 'pg-stop', 'activate-runtime', 'restore-runtime', 'initialization-source'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--postgres', type=Path)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--rtlcss-bin', type=Path)
    parser.add_argument('--overwrite-translations', action='store_true', help='Explicitly load reviewed JAKH translation corrections during tests/update')
    parser.add_argument('--allow-missing-rtlcss', action='store_true', help='Explicit staging-only exception; records Arabic RTL as NOT ready')
    parser.add_argument('--pg-port', type=int, default=17816)
    parser.add_argument('--http-base-port', type=int, default=8810,
                        help='Fresh staging only: first of two adjacent isolated customer HTTP ports')
    parser.add_argument('--customer', choices=['a', 'b'], default='a')
    parser.add_argument('--cron-threads', type=int, choices=[0, 1], default=1,
                        help='Enable one native scheduler in served staging; offline updates/tests never run jobs')
    parser.add_argument('--modules', default='base,jakh_core,jakh_estimate,jakh_operations,jakh_assurance,jakh_migration,jakh_workflow,jakh_branding')
    args = parser.parse_args()
    if args.action == 'prepare':
        if not args.source or not args.postgres:
            parser.error('prepare requires --source and --postgres')
        prepare(args)
    elif args.action == 'install':
        install(args)
    elif args.action == 'initialization-source':
        if not args.source:
            parser.error('initialization-source requires --source')
        initialization_source(args)
    elif args.action == 'serve':
        serve(args)
    elif args.action == 'add-modules':
        add_modules(args)
    elif args.action == 'test':
        test_modules(args)
    elif args.action in ('activate-runtime', 'restore-runtime'):
        if args.action == 'activate-runtime' and not args.runtime:
            parser.error('activate-runtime requires --runtime')
        change_runtime(args)
    else:
        cfg = settings(args.root)
        if args.action == 'pg-start':
            pg_start(cfg)
        else:
            checked([Path(cfg['postgres']) / 'bin/pg_ctl', '-D', Path(cfg['root']) / 'pgdata', '-w', '-m', 'fast', 'stop'])


if __name__ == '__main__':
    main()
