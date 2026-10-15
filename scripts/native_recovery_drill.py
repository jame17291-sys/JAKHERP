"""Bounded backup/restore rehearsal for marked, stopped synthetic staging only.

Never enrolls/serves the recovered clone, drops a database, overwrites a target,
or changes the source database/filestore. Evidence and every partial recovery
are retained privately. This is not production availability or RPO acceptance.
"""
import argparse
import configparser
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

import native_staging
from runtime_config import identifier, secret_file, write_private

ROOT_ROLE = 'jakh_staging_cluster'
MAX_FILES = 50000
MAX_DIRECTORIES = 10000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TREE_BYTES = 5 * 1024 * 1024 * 1024
MAX_ROWS_PER_TABLE = 200000
MAX_TOTAL_ROWS = 1000000
MAX_ROW_BYTES = 8 * 1024 * 1024
MAX_CANONICAL_BYTES = 1024 * 1024 * 1024


class RecoveryRejected(ValueError):
    """Safe, fixed diagnostic; never include credentials or source payloads."""


class Deadline:
    def __init__(self, seconds):
        if isinstance(seconds, bool) or not 60 <= int(seconds) <= 1800:
            raise RecoveryRejected('Use a rehearsal timeout between 60 and 1800 seconds.')
        self.started = time.monotonic()
        self.expires = self.started + int(seconds)

    def remaining(self):
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise RecoveryRejected('The bounded rehearsal deadline was exceeded; partial evidence was retained.')
        return remaining


def private_path(root, path, *, kind='directory', exists=True):
    """Refuse traversal and symlink components, including an ancestor link."""
    root, path = Path(root).absolute(), Path(path).absolute()
    if path == root or not path.is_relative_to(root):
        raise RecoveryRejected('A specific private child of the marked staging directory is required.')
    current = root
    for component in path.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise RecoveryRejected('Linked recovery/source paths are not permitted.')
    if exists:
        info = path.lstat()
        valid = stat.S_ISDIR(info.st_mode) if kind == 'directory' else stat.S_ISREG(info.st_mode)
        if not valid:
            raise RecoveryRejected('A regular source file or directory is required.')
    elif path.exists():
        raise RecoveryRejected('A recovery destination already exists; it will not be overwritten.')
    return path


def require_quiet_http(cfg):
    """Also refuse the separate HTTP ports used by native test registries."""
    native_staging.require_stopped_customers(cfg)
    for customer in cfg['customers'].values():
        port = int(customer['httpPort']) + 10000
        if port <= 65535:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    raise RecoveryRejected('Stop the isolated native test HTTP service before recovery rehearsal.')
            except ConnectionRefusedError:
                pass


def validate_customer(cfg, customer_id):
    if customer_id not in ('a', 'b'):
        raise RecoveryRejected('Select one of the two enrolled synthetic customers.')
    customer = cfg['customers'][customer_id]
    if not customer.get('initialized'):
        raise RecoveryRejected('The synthetic source customer must be enrolled before recovery rehearsal.')
    if customer.get('database') != 'jakh_staging_' + customer_id or customer.get('role') != 'jakh_customer_' + customer_id:
        raise RecoveryRejected('Only the original marked synthetic database and dedicated customer role are permitted.')
    root = Path(cfg['root'])
    data = private_path(root, customer['dataDir'])
    if data != root / ('customer-' + customer_id):
        raise RecoveryRejected('The source filestore must belong to this enrolled synthetic customer.')
    config_file = private_path(root, customer['config'], kind='file')
    if config_file.stat().st_mode & 0o077:
        raise RecoveryRejected('The source runtime configuration must be private.')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_file)
    options = parser['options']
    expected = {'db_name': customer['database'], 'db_user': customer['role'],
                'db_host': '127.0.0.1', 'db_port': str(cfg['pgPort']), 'data_dir': str(data)}
    if any(options.get(key) != value for key, value in expected.items()):
        raise RecoveryRejected('The private configuration does not match the marked synthetic customer.')
    password = secret_file(private_path(root, root / 'secrets' / (customer_id + '-db'), kind='file'))
    if options.get('db_password') != password:
        raise RecoveryRejected('The source customer credential does not match its private enrollment secret.')
    return customer, parser


def _connect(cfg, password, database='postgres', user=ROOT_ROLE):
    import psycopg2
    return psycopg2.connect(host='127.0.0.1', port=int(cfg['pgPort']), user=user,
                           password=password, dbname=database, connect_timeout=5,
                           application_name='jakh_synthetic_recovery_drill')


def require_quiet_databases(connection, cfg):
    with connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = ANY(%s) AND pid <> pg_backend_pid()',
                       ([item['database'] for item in cfg['customers'].values()],))
        if cursor.fetchone()[0]:
            raise RecoveryRejected('An isolated customer database has another session; stop initialization, tests and web workers first.')


def verify_cluster(connection, cfg):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_setting('data_directory'), current_setting('port')")
        role, directory, port = cursor.fetchone()
        if role != ROOT_ROLE or Path(directory).resolve() != (Path(cfg['root']) / 'pgdata').resolve() or int(port) != int(cfg['pgPort']):
            raise RecoveryRejected('The connected PostgreSQL server is not this marked synthetic cluster.')
        cursor.execute('SELECT rolsuper FROM pg_roles WHERE rolname = %s', (ROOT_ROLE,))
        if cursor.fetchone() != (True,):
            raise RecoveryRejected('Use the private synthetic cluster administrator only.')


def _digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def inventory_tree(directory, deadline):
    """Exact relative tree and content inventory; reject links/devices/huge data."""
    directory = Path(directory)
    if directory.is_symlink():
        raise RecoveryRejected('A linked source filestore is not permitted.')
    directories, files, total = [], {}, 0
    if directory.exists():
        if not directory.is_dir():
            raise RecoveryRejected('The source filestore is not a directory.')
        for parent, dirnames, filenames in os.walk(directory, followlinks=False):
            deadline.remaining()
            dirnames.sort(); filenames.sort()
            for name in dirnames:
                path = Path(parent) / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise RecoveryRejected('Linked or special filestore entries are not permitted.')
                directories.append(path.relative_to(directory).as_posix())
                if len(directories) > MAX_DIRECTORIES:
                    raise RecoveryRejected('The synthetic filestore exceeds the rehearsal directory bound.')
            for name in filenames:
                path = Path(parent) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise RecoveryRejected('A filestore entry is linked, special or exceeds the per-file rehearsal bound.')
                total += info.st_size
                if len(files) >= MAX_FILES or total > MAX_TREE_BYTES:
                    raise RecoveryRejected('The synthetic filestore exceeds the rehearsal file/size bound.')
                sha256, sha1 = hashlib.sha256(), hashlib.sha1()
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                        raise RecoveryRejected('The filestore changed while its snapshot was being read.')
                    while chunk := stream.read(1024 * 1024):
                        deadline.remaining(); sha256.update(chunk); sha1.update(chunk)
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                        raise RecoveryRejected('The filestore changed while its snapshot was being read.')
                files[path.relative_to(directory).as_posix()] = {'bytes': info.st_size,
                    'sha256': sha256.hexdigest(), 'sha1': sha1.hexdigest()}
    result = {'directories': sorted(directories), 'files': files, 'fileCount': len(files), 'bytes': total}
    result['sha256'] = _digest_json(result)
    return result


def copy_tree_new(source, destination, inventory, deadline):
    """Create only a new private target; preserve exact content, not broad modes."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for relative in inventory['directories']:
        (destination / relative).mkdir(mode=0o700, parents=False, exist_ok=False)
    for relative, evidence in inventory['files'].items():
        deadline.remaining()
        input_fd = os.open(source / relative, os.O_RDONLY | os.O_NOFOLLOW)
        output_fd = os.open(destination / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(input_fd, 'rb') as input_stream, os.fdopen(output_fd, 'wb') as output_stream:
            sha, copied = hashlib.sha256(), 0
            while chunk := input_stream.read(1024 * 1024):
                deadline.remaining(); output_stream.write(chunk); sha.update(chunk); copied += len(chunk)
            output_stream.flush(); os.fsync(output_stream.fileno())
            if copied != evidence['bytes'] or sha.hexdigest() != evidence['sha256']:
                raise RecoveryRejected('The source filestore changed during its private copy.')
    if inventory_tree(destination, deadline) != inventory:
        raise RecoveryRejected('The copied filestore does not match the exact snapshot tree.')


def dump_inventory(path, deadline):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TREE_BYTES or info.st_mode & 0o077:
        raise RecoveryRejected('The database dump is not a bounded private regular file.')
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise RecoveryRejected('The database dump is not a native PostgreSQL custom archive.')
        stream.seek(0)
        while chunk := stream.read(1024 * 1024):
            deadline.remaining(); digest.update(chunk)
    return {'bytes': info.st_size, 'sha256': digest.hexdigest()}


def canonical_database(cfg, password, database, filestore, deadline):
    """Hash every public row, sequence, ownership/ACL and source-id record.

    Only counts, digests and posted-ledger summaries enter private evidence.
    User credentials, attachment bodies and configuration values never do.
    """
    from psycopg2 import sql
    connection = _connect(cfg, password, database)
    try:
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute('SET LOCAL statement_timeout = %s', (int(deadline.remaining() * 1000),))
            cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            table_names = [row[0] for row in cursor.fetchall()]
        tables, total_rows, total_bytes = {}, 0, 0
        for name in table_names:
            deadline.remaining()
            digest, count = hashlib.sha256(), 0
            with connection.cursor(name='jakh_recovery_' + secrets.token_hex(5)) as cursor:
                cursor.itersize = 500
                cursor.execute(sql.SQL('SELECT to_jsonb(t)::text FROM {} t ORDER BY to_jsonb(t)::text COLLATE "C"').format(sql.Identifier('public', name)))
                for row in cursor:
                    deadline.remaining()
                    encoded = row[0].encode()
                    count += 1; total_rows += 1; total_bytes += len(encoded)
                    if count > MAX_ROWS_PER_TABLE or total_rows > MAX_TOTAL_ROWS or len(encoded) > MAX_ROW_BYTES or total_bytes > MAX_CANONICAL_BYTES:
                        raise RecoveryRejected('The database exceeds the bounded synthetic canonical-inventory size.')
                    digest.update(len(encoded).to_bytes(8, 'big')); digest.update(encoded)
            tables[name] = {'rows': count, 'sha256': digest.hexdigest()}
        with connection.cursor() as cursor:
            cursor.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename")
            sequence_names = [row[0] for row in cursor.fetchall()]
            sequences = {}
            for name in sequence_names:
                cursor.execute(sql.SQL('SELECT last_value, is_called FROM {}').format(sql.Identifier('public', name)))
                sequences[name] = list(cursor.fetchone())
            cursor.execute("SELECT nspname, pg_get_userbyid(nspowner), nspacl::text FROM pg_namespace WHERE nspname = 'public'")
            schema = cursor.fetchall()
            cursor.execute("SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname")
            relations = cursor.fetchall()
            cursor.execute("SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_userbyid(p.proowner), p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)")
            functions = cursor.fetchall()
            cursor.execute('SELECT pg_get_userbyid(datdba), datacl::text FROM pg_database WHERE datname=%s', (database,))
            db_permissions = cursor.fetchone()
            permissions = {'schema': schema, 'relations': relations, 'functions': functions, 'database': db_permissions}
            posted_gl = None
            if {'account_move', 'account_move_line'}.issubset(tables):
                cursor.execute("SELECT m.company_id, c.currency_id, r.name, count(DISTINCT m.id), count(l.id), COALESCE(sum(l.debit),0)::text, COALESCE(sum(l.credit),0)::text, COALESCE(sum(l.balance),0)::text FROM account_move m JOIN res_company c ON c.id=m.company_id JOIN res_currency r ON r.id=c.currency_id LEFT JOIN account_move_line l ON l.move_id=m.id WHERE m.state='posted' GROUP BY m.company_id,c.currency_id,r.name ORDER BY m.company_id,c.currency_id")
                posted_gl = {'basis': 'posted journals; separate company currency ledgers', 'companyLedgers': [
                    dict(zip(('companyId', 'currencyId', 'currency', 'postedMoves', 'lines', 'debit', 'credit', 'balance'), values))
                    for values in cursor.fetchall()]}
                # Each currency company's books must balance independently.
                cursor.execute("SELECT company_id FROM account_move_line WHERE parent_state='posted' GROUP BY company_id HAVING abs(sum(balance)) > 0.000001")
                if cursor.fetchone():
                    raise RecoveryRejected('The synthetic posted company ledger is not balanced before/after restoration.')
            attachment_count = 0
            if 'ir_attachment' in tables:
                cursor.execute("SELECT store_fname, checksum FROM ir_attachment WHERE store_fname IS NOT NULL AND store_fname <> '' ORDER BY id")
                for name, checksum in cursor:
                    relative = PurePosixPath(name)
                    if not relative.parts or relative.is_absolute() or '..' in relative.parts or '\\' in name:
                        raise RecoveryRejected('An attachment has an unsafe filestore reference.')
                    evidence = filestore['files'].get(relative.as_posix())
                    if not evidence or (checksum and checksum != evidence['sha1']):
                        raise RecoveryRejected('A database attachment is missing or differs from its native file checksum.')
                    attachment_count += 1
        result = {'tables': tables, 'sequences': sequences, 'permissions': permissions,
                  'totalRows': total_rows, 'postedGL': posted_gl, 'checkedFileAttachments': attachment_count}
        result['sha256'] = _digest_json(result)
        return result
    finally:
        connection.close()


def verify_customer_permissions(connection, cfg, customer, restored_database=None):
    """No new login/role grants; source role owns only its own recovery clone."""
    role = customer['role']
    other = next(item for item in cfg['customers'].values() if item['role'] != role)
    with connection.cursor() as cursor:
        cursor.execute('SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit FROM pg_roles WHERE rolname=%s', (role,))
        if cursor.fetchone() != (True, False, False, False, False):
            raise RecoveryRejected('The enrolled customer role does not have the isolated non-administrative permissions.')
        cursor.execute('SELECT has_database_privilege(%s,%s,\'CONNECT\'), has_database_privilege(%s,%s,\'CONNECT\')',
                       (role, customer['database'], role, other['database']))
        if cursor.fetchone() != (True, False):
            raise RecoveryRejected('The original customer database connection isolation is not intact.')
        if restored_database:
            cursor.execute('SELECT has_database_privilege(%s,%s,\'CONNECT\'), has_database_privilege(%s,%s,\'CONNECT\')',
                           (role, restored_database, other['role'], restored_database))
            if cursor.fetchone() != (True, False):
                raise RecoveryRejected('The recovered database is not restricted to its original customer role.')


def _run_pg(cfg, password, executable, arguments, log, deadline):
    binary = Path(cfg['postgres']) / 'bin' / executable
    if binary.is_symlink() or not binary.is_file() or not binary.resolve().is_relative_to(native_staging.WORK_ROOT.resolve()):
        raise RecoveryRejected('Use the pinned PostgreSQL utilities under this workspace work/ only.')
    env = native_staging.environment(cfg)
    for key in ('PGSERVICE', 'PGSERVICEFILE', 'PGPASSFILE', 'PGOPTIONS'):
        env.pop(key, None)
    env['PGPASSWORD'] = password
    command = [str(binary), '--host=127.0.0.1', '--port=' + str(cfg['pgPort']), '--username=' + ROOT_ROLE, '--no-password', *arguments]
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, check=True,
                       timeout=deadline.remaining(), cwd=cfg['root'])


def _save_json(path, value):
    write_private(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n')


def run_drill(root, customer_id, timeout_seconds=900):
    """Retain a new dump, two exact filestores, recovered DB and private proof."""
    deadline = Deadline(timeout_seconds)
    cfg = native_staging.settings(root)
    root = Path(cfg['root'])
    lock_fd = os.open(root / '.staging.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryRejected('Another isolated staging enrollment/release operation is running.') from None
        cfg = native_staging.settings(root)
        require_quiet_http(cfg)
        customer, parser = validate_customer(cfg, customer_id)
        password = secret_file(private_path(root, root / 'secrets' / 'cluster', kind='file'))
        administrator = _connect(cfg, password)
        administrator.autocommit = True
        evidence = None
        directory = None
        try:
            verify_cluster(administrator, cfg)
            require_quiet_databases(administrator, cfg)
            verify_customer_permissions(administrator, cfg, customer)
            parent = root / 'recovery-drills'
            if parent.exists():
                private_path(root, parent)
                if parent.stat().st_mode & 0o077:
                    raise RecoveryRejected('The recovery evidence directory must be private.')
            else:
                parent.mkdir(mode=0o700)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            token = secrets.token_hex(8)
            directory = private_path(root, parent / (stamp + '-' + customer_id + '-' + token), exists=False)
            directory.mkdir(mode=0o700)
            restored = identifier('jakh_dr_' + customer_id + '_' + token)
            evidence = {'schema': 1, 'scope': 'marked isolated synthetic staging recovery rehearsal only',
                'status': 'running', 'sourceDatabase': customer['database'], 'restoredDatabase': restored,
                'customerRole': customer['role'], 'startedAtUTC': datetime.now(timezone.utc).isoformat(),
                'productionAcceptance': False, 'productionRPOValidated': False, 'productionAvailabilityValidated': False,
                'quiescence': 'observed stopped HTTP/test services and no other customer database sessions; no production fence',
                'retainedEvidenceDirectory': str(directory), 'timingsSeconds': {}, 'sourceUnmodified': True,
                'cloneEnrolledOrServed': False, 'phase': 'source-snapshot'}
            backup = directory / 'backup'; backup.mkdir(mode=0o700)
            recovered_runtime = directory / 'recovered-runtime'; recovered_runtime.mkdir(mode=0o700)
            recovered_parent = recovered_runtime / 'filestore'; recovered_parent.mkdir(mode=0o700)
            source_store = root / ('customer-' + customer_id) / 'filestore' / customer['database']
            private_path(root, source_store, exists=False) if not source_store.exists() else private_path(root, source_store)
            phase = time.monotonic()
            source_files = inventory_tree(source_store, deadline)
            source_data = canonical_database(cfg, password, customer['database'], source_files, deadline)
            copy_tree_new(source_store, backup / 'filestore', source_files, deadline)
            require_quiet_http(cfg); require_quiet_databases(administrator, cfg)
            dump = backup / 'database.dump'
            # Restrictive inherited mode for pg_dump's new file, restored below.
            previous_umask = os.umask(0o077)
            try:
                _run_pg(cfg, password, 'pg_dump', ['--format=custom', '--file=' + str(dump), customer['database']],
                        directory / 'pg-dump.log', deadline)
            finally:
                os.umask(previous_umask)
            if not dump.is_file() or dump.is_symlink() or dump.stat().st_size <= 0:
                raise RecoveryRejected('PostgreSQL did not create a valid new private dump.')
            evidence['databaseDump'] = dump_inventory(dump, deadline)
            evidence['timingsSeconds']['snapshot'] = round(time.monotonic() - phase, 3)
            require_quiet_http(cfg); require_quiet_databases(administrator, cfg)
            if inventory_tree(source_store, deadline) != source_files or canonical_database(cfg, password, customer['database'], source_files, deadline) != source_data:
                raise RecoveryRejected('The stopped source changed during the snapshot; do not accept this rehearsal.')
            from psycopg2 import sql
            evidence['phase'] = 'new-database-and-filestore-restore'
            with administrator.cursor() as cursor:
                cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (restored,))
                if cursor.fetchone():
                    raise RecoveryRejected('The unique recovered database already exists; it will not be overwritten.')
                cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(restored), sql.Identifier(customer['role'])))
                cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(restored)))
            phase = time.monotonic()
            _run_pg(cfg, password, 'pg_restore', ['--exit-on-error', '--dbname=' + restored, str(dump)],
                    directory / 'pg-restore.log', deadline)
            recovered_store = recovered_parent / restored
            copy_tree_new(backup / 'filestore', recovered_store, source_files, deadline)
            evidence['timingsSeconds']['restoreDatabaseAndFilestore'] = round(time.monotonic() - phase, 3)
            # Private config is retained for inspection only, never registered or
            # served. It preserves the original tenant role and native user IDs.
            parser['options']['db_name'] = restored
            parser['options']['dbfilter'] = '^' + restored + '$'
            parser['options']['data_dir'] = str(recovered_runtime)
            parser['options']['list_db'] = 'False'
            parser['options']['max_cron_threads'] = '0'
            parser['options']['http_enable'] = 'False'
            config_text = io.StringIO(); parser.write(config_text)
            write_private(directory / 'recovered-customer.conf', config_text.getvalue())
            phase = time.monotonic()
            evidence['phase'] = 'canonical-and-permission-reconciliation'
            restored_files = inventory_tree(recovered_store, deadline)
            restored_data = canonical_database(cfg, password, restored, restored_files, deadline)
            if restored_files != source_files or restored_data != source_data:
                raise RecoveryRejected('Recovered canonical rows, source IDs, permissions, posted GL or filestore differ from the snapshot.')
            verify_customer_permissions(administrator, cfg, customer, restored)
            # Prove the native customer login still connects, without changing
            # credentials or granting the recovery role any administrative power.
            customer_connection = _connect(cfg, parser['options']['db_password'], restored, customer['role'])
            customer_connection.close()
            require_quiet_http(cfg); require_quiet_databases(administrator, cfg)
            if inventory_tree(source_store, deadline) != source_files or canonical_database(cfg, password, customer['database'], source_files, deadline) != source_data:
                raise RecoveryRejected('The source changed during recovery verification; retain but reject this rehearsal.')
            evidence['timingsSeconds']['reconciliation'] = round(time.monotonic() - phase, 3)
            _save_json(directory / 'filestore-inventory.json', source_files)
            _save_json(directory / 'canonical-database-inventory.json', source_data)
            evidence.update(status='passed', completedAtUTC=datetime.now(timezone.utc).isoformat(),
                canonicalDatabaseSHA256=source_data['sha256'], exactFilestoreSHA256=source_files['sha256'],
                fileAttachmentsChecked=source_data['checkedFileAttachments'], postedGL=source_data['postedGL'],
                recoveredRuntimeConfiguration=str(directory / 'recovered-customer.conf'),
                nativeCustomerPermissionsVerified=True, phase='complete')
            evidence['timingsSeconds']['total'] = round(time.monotonic() - deadline.started, 3)
            _save_json(directory / 'recovery-evidence.json', evidence)
            return directory / 'recovery-evidence.json'
        except Exception as error:
            if evidence is not None:
                # No raw exception, command/environment or SQL values in evidence.
                evidence.update(status='failed', failureStage=evidence['phase'], errorType=type(error).__name__,
                                completedAtUTC=datetime.now(timezone.utc).isoformat(),
                                retainedPartialRecovery=True)
                if isinstance(error, RecoveryRejected):
                    evidence['safeFailureReason'] = str(error)
                evidence['timingsSeconds']['total'] = round(time.monotonic() - deadline.started, 3)
                _save_json(directory / 'recovery-evidence.json', evidence)
                raise RecoveryRejected('Recovery rehearsal failed; private backup/partial clone/evidence were retained at ' + str(directory)) from None
            raise
        finally:
            administrator.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--customer', required=True, choices=('a', 'b'))
    parser.add_argument('--timeout-seconds', type=int, default=900)
    args = parser.parse_args()
    try:
        proof = run_drill(args.root, args.customer, args.timeout_seconds)
    except Exception as error:
        # Fixed safe messages only. PostgreSQL exceptions can contain SQL/data,
        # so never print their raw content or a traceback.
        if isinstance(error, RecoveryRejected):
            print(str(error), file=sys.stderr)
        else:
            print('Synthetic recovery rehearsal refused or failed; no credentials printed. Inspect private retained evidence if created.', file=sys.stderr)
        return 1
    print('Synthetic recovery rehearsal passed; private evidence: ' + str(proof))
    print('Production availability, residency, RPO and SLA acceptance remain separate gates.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
