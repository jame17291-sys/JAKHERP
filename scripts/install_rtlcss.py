"""Verify and install the source-built native compiler without contacting npm.

Source receipts identify official upstream files, not registry archive provenance.
Lifecycle scripts are disabled; a new, project-local destination is required.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

BUNDLE = Path(__file__).resolve().parents[1] / 'deploy/rtlcss'
VERSIONS = {'rtlcss': '4.3.0', 'postcss': '8.5.6', 'nanoid': '3.3.11',
            'source-map-js': '1.2.1', 'escalade': '3.1.1',
            'picocolors': '1.1.1', 'strip-json-comments': '3.1.1'}


def verify_bundle(bundle=BUNDLE):
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'package.json').read_text())
    lock = json.loads((bundle / 'package-lock.json').read_text())
    expected = {name: f'file:vendor/{name}-{version}.tgz' for name, version in VERSIONS.items()}
    if manifest['dependencies'] != expected or lock['packages']['']['dependencies'] != expected:
        raise ValueError('Compiler dependency pins differ from the reviewed offline closure')
    if set(lock['packages']) != {'', *(f'node_modules/{name}' for name in VERSIONS)}:
        raise ValueError('Unexpected packages in compiler lock')
    receipts = json.loads((bundle / 'provenance/dependency-sources.json').read_text())
    receipts += [json.loads((bundle / f'provenance/{name}-{version}.json').read_text())
                 for name, version in VERSIONS.items()
                 if name not in ('escalade', 'picocolors', 'strip-json-comments')]
    originals = {}
    for receipt in receipts:
        if VERSIONS.get(receipt.get('name')) != receipt.get('version'):
            continue
        originals[receipt['name']] = {item.get('path', item.get('file')): item['sha256']
                                       for item in receipt['files'] if 'sha256' in item}
    build = json.loads((bundle / 'provenance/escalade-build.json').read_text())
    for item in build['files']:
        originals['escalade'][item['cjs']] = item['cjsSha256']
        originals['escalade'][item['esm']] = item['esmSha256']
    summaries = []
    for name, version in VERSIONS.items():
        entry = lock['packages'][f'node_modules/{name}']
        relative = f'vendor/{name}-{version}.tgz'
        if entry['version'] != version or entry['resolved'] != f'file:{relative}':
            raise ValueError(f'Invalid lock entry: {name}')
        archive = bundle / relative
        integrity = 'sha512-' + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
        if integrity != entry['integrity']:
            raise ValueError(f'Compiler archive integrity failure: {name}')
        names = []
        with tarfile.open(archive) as packed:
            for member in packed.getmembers():
                parts = PurePosixPath(member.name).parts
                if parts[0] != 'package' or '..' in parts or not member.isfile():
                    raise ValueError('Unexpected archive member')
                path = '/'.join(parts[1:])
                content = packed.extractfile(member).read()
                names.append(path)
                if path == 'source-manifest.json':
                    if json.loads(content) not in receipts:
                        raise ValueError(f'Unrecognized generated source receipt: {name}')
                elif hashlib.sha256(content).hexdigest() != originals[name].get(path):
                    raise ValueError(f'Source provenance mismatch: {name}/{path}')
                if path == 'package.json':
                    package = json.loads(content)
                    if package['name'] != name or package['version'] != version:
                        raise ValueError('Archive package identity mismatch')
        if not any(path.lower() in ('license', 'license.md') for path in names):
            raise ValueError(f'Missing upstream license: {name}')
        summaries.append({'name': name, 'version': version, 'files': len(names), 'integrity': integrity})
    return summaries


def install(destination, bundle=BUNDLE):
    verify_bundle(bundle)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Compiler installation destination must be NEW')
    # npm treats macOS /var vs /private/var aliases as different local projects.
    destination = destination.resolve()
    destination.mkdir(parents=True)
    for name in ('package.json', 'package-lock.json'):
        shutil.copy2(Path(bundle) / name, destination / name)
    for name in ('vendor', 'provenance'):
        shutil.copytree(Path(bundle) / name, destination / name)
    subprocess.run(['npm', 'ci', '--prefix', str(destination), '--offline', '--ignore-scripts',
                    '--omit=dev', '--no-audit', '--no-fund', '--cache', str(destination / '.npm-cache')],
                   cwd=destination, check=True)
    subprocess.run(['npm', 'ls', '--prefix', str(destination), '--offline', '--all'], cwd=destination, check=True)
    subprocess.run([str(destination / 'node_modules/.bin/rtlcss'), '--version'], check=True)
    return destination / 'node_modules/.bin'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=BUNDLE)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        print(json.dumps({'sourceBuiltPackages': verify_bundle(args.bundle)}))
    elif args.destination:
        print(json.dumps({'rtlcssBin': str(install(args.destination, args.bundle))}))
    else:
        parser.error('--destination or --verify-only is required')
