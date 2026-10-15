"""Generate new private deployment secrets in an explicitly chosen new directory."""
import argparse
from pathlib import Path
import secrets
from runtime_config import write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    target = args.destination.absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('Destination must be new; existing credentials are never overwritten')
    target = target.parent.resolve(strict=True) / target.name
    target.mkdir(mode=0o700)
    for name in ('cluster-password', 'db-password', 'master-password', 'owner-password',
                 'idp-cluster-password', 'idp-db-password', 'idp-admin-password'):
        write_private(target / name, secrets.token_urlsafe(48) + '\n')
    print('Seven new owner-only secret files generated; no values were printed.')


if __name__ == '__main__':
    main()
