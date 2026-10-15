"""Verify a pinned vendor archive before safely extracting it; never execute it."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request

PACKAGE = Path(__file__).resolve().parents[1]

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def validate_members(members, archive_root):
    paths = set()
    total = 0
    if len(members) > 200000:
        raise ValueError('Too many archive members')
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != archive_root:
            raise ValueError(f'Unsafe archive path: {member.name}')
        if '\\' in member.name or '\x00' in member.name or str(path) in paths:
            raise ValueError(f'Ambiguous or duplicate archive path: {member.name}')
        if not (member.isfile() or member.isdir()):
            raise ValueError(f'Links and special archive members are not allowed: {member.name}')
        if member.size < 0 or member.size > 1024 * 1024 * 1024:
            raise ValueError('Archive member is unexpectedly large')
        paths.add(str(path))
        total += member.size
    if total > 16 * 1024 * 1024 * 1024:
        raise ValueError('Archive is unexpectedly large')

def extract_verified(archive, destination, spec):
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('Archive must be an ordinary file')
    if archive.stat().st_size != spec['bytes'] or sha256(archive) != spec['sha256']:
        raise ValueError('Vendor archive does not match the reviewed lock file')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Extraction destination must be a new directory; existing files are never overwritten')
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    with tarfile.open(archive, mode='r:gz') as source:
        members = source.getmembers()
        validate_members(members, spec['archiveRoot'])
        destination.mkdir(mode=0o700)
        source.extractall(destination, members=members, filter='data')
    return destination / spec['archiveRoot']

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', choices=['odoo', 'postgresql'], default='odoo')
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads((PACKAGE / 'vendor.lock.json').read_text())[args.component]
    result = extract_verified(args.archive, args.destination, spec)
    print(json.dumps({'component': args.component, 'source': str(result), 'sha256': spec['sha256'], 'verified': True}))

if __name__ == '__main__':
    main()
