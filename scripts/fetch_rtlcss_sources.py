"""Fetch unmodified, version-pinned official dependency sources when npm is unreachable.

This is a source checkout, not a replacement implementation. Never executes package
scripts. New output only; each downloaded file has a source URL and SHA-256 receipt.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

SPECS = {
    'rtlcss': ('MohammadYounes/rtlcss', 'f1a08a67fc536cf84e11736fbb0f1ae0221e3d11', '4.3.0', ['bin/rtlcss.js']),
    'postcss': ('postcss/postcss', '8.5.6', '8.5.6', []),
    'nanoid': ('ai/nanoid', '3.3.11', '3.3.11', []),
    'source-map-js': ('7rulnik/source-map-js', 'v1.2.1', '1.2.1', []),
}


def fetch(url, optional=False):
    result = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '6',
                             '--max-time', '15', url], capture_output=True, timeout=20)
    if result.returncode:
        if optional and b'404' in result.stderr:
            return None
        raise ValueError('Official source download failed: ' + url)
    if len(result.stdout) > 3 * 1024 * 1024:
        raise ValueError('Unexpectedly large dependency source file')
    return result.stdout


def normalize(path, allow_root=False):
    parts = []
    for part in PurePosixPath(path).parts:
        if part == '..':
            if not parts:
                raise ValueError('Module reference escapes package')
            parts.pop()
        elif part != '.':
            parts.append(part)
    if not parts and allow_root:
        return '.'
    if not parts or PurePosixPath(path).is_absolute():
        raise ValueError('Unsafe package source path')
    return '/'.join(parts)


def package_sources(name, destination):
    repo, ref, version, entries = SPECS[name]
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Dependency source destination must be NEW')
    base = f'https://raw.githubusercontent.com/{repo}/{ref}/'
    files = {'package.json': fetch(base + 'package.json')}
    manifest = json.loads(files['package.json'])
    if manifest.get('name') != name or manifest.get('version') != version:
        raise ValueError('Official package name/version differs from reviewed pin')
    pending = set(entries)
    main = manifest.get('main', 'index.js')
    pending.add(normalize(main))
    # Download all published entry points, not just the entry used by the CLI.
    def exports(value):
        if isinstance(value, str) and value.startswith('./'):
            pending.add(normalize(value))
        elif isinstance(value, dict):
            for item in value.values():
                exports(item)
    exports(manifest.get('exports', {}))
    if isinstance(manifest.get('bin'), str):
        pending.add(normalize(manifest['bin']))
    elif isinstance(manifest.get('bin'), dict):
        pending.update(normalize(value) for value in manifest['bin'].values())
    for license_name in ('LICENSE', 'LICENSE.md', 'license', 'license.md'):
        content = fetch(base + license_name, optional=True)
        if content is not None:
            files[license_name] = content
            break
    else:
        raise ValueError('Upstream license file must be retained')

    def download(path):
        path = normalize(path)
        candidates = [path] if PurePosixPath(path).suffix else [path + '.js', path + '.cjs', path + '.json', path + '/index.js']
        for candidate in candidates:
            content = fetch(base + candidate, optional=True)
            if content is not None:
                return candidate, content
        raise ValueError('Required upstream source file was not found: ' + path)
    while pending:
        batch, pending = sorted(pending - set(files)), set()
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=8) as executor:
            for path, content in executor.map(download, batch):
                files[path] = content
                if Path(path).suffix not in ('.js', '.cjs', '.mjs'):
                    continue
                source = content.decode()
                requires = re.findall(r'''require\(\s*["'](\.[^"']+)["']\s*\)''', source)
                requires += re.findall(r'''\bfrom\s*["'](\.[^"']+)["']''', source)
                for reference in requires:
                    resolved = normalize(str(PurePosixPath(path).parent / reference), allow_root=True)
                    if resolved == '.':
                        resolved = normalize(main)
                    if not any(candidate in files for candidate in (resolved, resolved + '.js', resolved + '.cjs', resolved + '.json', resolved + '/index.js')):
                        pending.add(resolved)
    destination.mkdir(parents=True)
    receipts = []
    for path, content in sorted(files.items()):
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        receipts.append({'path': path, 'source': base + path, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    receipt = {'name': name, 'version': version, 'repository': 'https://github.com/' + repo,
               'ref': ref, 'license': manifest.get('license'), 'files': receipts}
    (destination / 'source-manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return {'name': name, 'version': version, 'files': len(files), 'destination': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', choices=sorted(SPECS), required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package_sources(args.package, args.destination)))


if __name__ == '__main__':
    main()
