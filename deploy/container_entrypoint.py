"""Root-only setup, then run the Odoo application as an unprivileged user."""
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/jakh/scripts')
from runtime_config import config_text, secret_file, write_private
from runtime_bootstrap import validate_bootstrap_metadata

UID, GID = 101, 101
DATA = Path('/var/lib/jakh-odoo')
RUNTIME = Path('/run/jakh')
CONFIG = RUNTIME / 'odoo.conf'
SOURCE = Path('/opt/jakh-runtime/vendor')


def application_addons(action, runtime=Path('/opt/jakh-runtime')):
    """Expose inert descriptors to native fresh init, never ordinary serving."""
    if action not in ('serve', 'init'):
        raise ValueError('Only serve or fresh-database init is supported')
    paths = [runtime / 'vendor/odoo/addons', runtime / 'addons']
    if action == 'init':
        paths.append(validate_bootstrap_metadata(runtime, required=True))
    return paths


def main():
    if os.getuid() != 0:
        raise SystemExit('Entry setup requires root solely to read secrets and drop privileges')
    action = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    if action not in ('serve', 'init'):
        raise SystemExit('Only serve or fresh-database init is supported')
    startup_addons = application_addons(action)
    database = os.environ['JAKH_DATABASE']
    login = os.environ.get('JAKH_OWNER_LOGIN', '')
    if action == 'init' and ('@' not in login or any(c in login for c in '\r\n\x00')):
        raise SystemExit('Explicit owner email is required for initialization')
    for directory in (DATA, RUNTIME):
        if directory.is_symlink():
            raise SystemExit('Application directories cannot be symbolic links')
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    os.chown(DATA, UID, GID)
    content = config_text(database=database, db_user=os.environ['JAKH_DB_USER'],
        db_password=secret_file('/run/secrets/db_password'),
        master_password=secret_file('/run/secrets/master_password'),
        addons=application_addons('serve'), data_dir=DATA,
        db_host='db', db_port=5432, http_host='0.0.0.0', http_port=8069,
        proxy_mode=os.environ.get('JAKH_PROXY_MODE') == 'true', workers=0)
    write_private(CONFIG, content)
    os.chown(CONFIG, UID, GID)
    # Only fresh initialization can access the owner enrollment secret. It is not needed when serving.
    owner_file = RUNTIME / 'owner-password'
    if action == 'init':
        write_private(owner_file, secret_file('/run/secrets/owner_password') + '\n')
        os.chown(owner_file, UID, GID)
    # Keep the private runtime directory root-owned until all configuration files are created.
    os.chown(RUNTIME, UID, GID)
    os.setgroups([])
    os.setgid(GID)
    os.setuid(UID)
    os.umask(0o077)
    os.chdir(SOURCE)
    if action == 'init':
        import subprocess
        # Refuse populated databases; never run a destructive reinitialization or password reset.
        import psycopg2
        # Database freshness is checked with Odoo's supplied configuration before installing modules.
        import configparser
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read(CONFIG)
        with psycopg2.connect(host='db', dbname=database, user=os.environ['JAKH_DB_USER'], password=parsed['options']['db_password']) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public')")
                if cursor.fetchone()[0]:
                    raise SystemExit('Refusing initialization of an already-enrolled database')
        subprocess.run([sys.executable, '-m', 'odoo', '-c', str(CONFIG), '--addons-path',
            ','.join(str(path) for path in startup_addons), '-i',
            'base,jakh_core,jakh_estimate,jakh_operations,jakh_assurance,jakh_migration,jakh_workflow,jakh_branding',
            '--without-demo', '--no-http', '--stop-after-init'], check=True)
        subprocess.run([sys.executable, '/opt/jakh/scripts/bootstrap_owner.py', '--config', str(CONFIG),
            '--database', database, '--login', login, '--password-file', str(owner_file)], check=True)
        (DATA / '.owner-enrolled').write_text(database + '\n')
        return
    marker = DATA / '.owner-enrolled'
    if not marker.is_file() or marker.is_symlink() or marker.read_text().strip() != database:
        raise SystemExit('Refusing HTTP: run explicit offline init/enrollment first')
    os.execv(sys.executable, [sys.executable, '-m', 'odoo', '-c', str(CONFIG)])


if __name__ == '__main__':
    main()
