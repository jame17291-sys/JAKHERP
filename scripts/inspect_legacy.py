"""Read-only migration inventory. Never exports credentials or changes live databases."""
import argparse
import json
from pathlib import Path
import sqlite3


def connection(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected an ordinary existing database file')
    database = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    database.execute('PRAGMA query_only=ON')
    database.row_factory = sqlite3.Row
    return database


def inspect(data_dir):
    data_dir = Path(data_dir).resolve(strict=True)
    result = {'mode': 'read-only', 'credentialsExported': False, 'customers': [], 'migrationApplied': False}
    with connection(data_dir / 'control.sqlite') as control:
        tenants = control.execute('SELECT id,name,slug,demo FROM tenants ORDER BY created_at').fetchall()
        for tenant in tenants:
            identifier = tenant['id']
            if len(identifier) != 36 or any(letter not in '0123456789abcdef-' for letter in identifier.lower()):
                raise ValueError('Unexpected customer identifier')
            path = data_dir / 'tenants' / identifier / 'erp.sqlite'
            with connection(path) as database:
                database.execute('BEGIN')
                version = database.execute('SELECT version FROM workspace WHERE id=1').fetchone()[0]
                counts = [dict(row) for row in database.execute('SELECT module,menu,COUNT(*) AS records FROM records GROUP BY module,menu ORDER BY module,menu')]
                audit_count = database.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
                result['customers'].append({
                    **dict(tenant), 'workspaceVersion': version, 'registers': counts, 'auditEvents': audit_count,
                    'requiresSampleReview': bool(tenant['demo']),
                    'warning': 'A demo flag does not prove all records are illustrative. Review provenance before migration; do not treat stored values as opening balances.',
                })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.data_dir), indent=2))


if __name__ == '__main__':
    main()
