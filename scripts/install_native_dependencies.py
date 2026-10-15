"""Install source requirements in an isolated venv; only psycopg2 uses its matching native wheel."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    args = parser.parse_args()
    target = args.build_dir.resolve(strict=True) / 'native-requirements.txt'
    if 'work' not in target.parts:
        raise ValueError('Derived native requirements must stay under work/')
    original = (args.source.resolve(strict=True) / 'requirements.txt').read_text()
    if 'psycopg2==2.9.9' not in original:
        raise ValueError('This native exception is reviewed only for the pinned Odoo Python 3.12 requirements')
    native = original.replace('psycopg2==', 'psycopg2-binary==')
    target.write_text('# Native macOS staging: same version, binary distribution only; cloud uses original source requirements.\n' + native)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '-r', str(target)], check=True)


if __name__ == '__main__':
    main()
