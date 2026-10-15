"""Read-only, fail-closed check of a clean JAKH source publication candidate.

Default: inspect an exact Git root's tracked/index files and require their
working bytes to match the index. --tree checks a pre-Git candidate instead.
Neither mode scans Git history, follows links, reads excluded directories,
copies/deletes files, accesses a remote, nor prints matching secret values.
"""
import argparse
import base64
from dataclasses import asdict, dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FILES = 20000
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
SOURCE_DIRECTORIES = {'addons', 'deploy', 'requirements', 'scripts', 'tests', 'docs', '.github'}
ROOT_FILES = {'LICENSE', 'COPYRIGHT', 'NOTICE', 'COPYING', 'AUTHORS', 'vendor.lock.json',
              '.gitignore', '.gitattributes', '.dockerignore', '.editorconfig',
              '.env.example', '.env.template', 'requirements.txt', 'pyproject.toml'}
SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.ts', '.xml', '.csv', '.po', '.pot',
                   '.json', '.md', '.html', '.txt', '.css', '.scss', '.yaml', '.yml',
                   '.toml', '.sh', '.sql', '.svg', '.png', '.jpg', '.jpeg', '.gif',
                   '.webp', '.ico', '.ttf', '.woff', '.woff2'}
SPECIAL_SOURCE_NAMES = {'Dockerfile', 'Dockerfile.dockerignore', 'LICENSE', 'license',
                        'COPYRIGHT', 'NOTICE', 'COPYING', 'AUTHORS', '.gitignore',
                        '.dockerignore', '.env.example', '.env.template'}
EXCLUDED_PARTS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.cache', 'secrets', 'secret', 'credentials', 'authkeys',
    '.ssh', '.aws', '.config', 'runtime', 'runtimes', 'filestore', 'pgdata', 'data_dir',
    'backups', 'backup', 'dumps', 'logs', 'work', 'outputs', 'tmp', 'temp'}
PRIVATE_NAMES = {'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'authorized_keys',
    'known_hosts', 'credentials.json', 'secrets.json', 'auth.json', 'token.json',
    'tokens.json', '.npmrc', '.pypirc', '.netrc', '.pgpass', '.my.cnf'}
PRIVATE_SUFFIXES = {'.key', '.pem', '.p12', '.pfx', '.jks', '.keystore', '.log',
    '.dump', '.backup', '.bak', '.sqlite', '.sqlite3', '.db', '.db3', '.pyc', '.pyo'}
VENDOR_ARCHIVES = {'escalade-3.1.1.tgz', 'nanoid-3.3.11.tgz', 'picocolors-1.1.1.tgz',
    'postcss-8.5.6.tgz', 'rtlcss-4.3.0.tgz', 'source-map-js-1.2.1.tgz', 'strip-json-comments-3.1.1.tgz'}
LOCK_PATH = 'deploy/rtlcss/package-lock.json'

# Patterns are deliberately high confidence. Report categories and paths only.
SECRET_PATTERNS = [
    ('private-key', re.compile(r'-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{70,})\b')),
    ('openai-token', re.compile(r'\bsk-(?:proj-|svcacct-)[A-Za-z0-9_-]{30,}\b|\bsk-[A-Za-z0-9]{48}\b')),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('google-api-key', re.compile(r'\bAIza[A-Za-z0-9_-]{35}\b')),
    ('google-oauth-secret', re.compile(r'\bGOCSPX-[A-Za-z0-9_-]{24,}\b')),
    ('credential-url', re.compile(r'(?i)\b(?:postgres(?:ql)?|mysql|redis|amqps?|https?)://'
        r'[^\s/:@]+:(?!\$\{|<)(?!password@|example@|replace[-_]me@|changeme@)[^\s/@?#]{8,}@')),
    ('literal-credential', re.compile(r'''(?im)\b(?:[A-Z0-9_]*[_-])?(?:password|passwd|secret|token|api[_-]?key|auth[_-]?key)\s*[=:]\s*["']?([A-Za-z0-9+/_=-]{24,})''')),
]
MACHINE_PATH = re.compile(
    r'''/(?:Users|home|Volumes)/[A-Za-z0-9_.-]+/[^\s"'<>`]*'''
    r'''|/private/var/folders/[A-Za-z0-9]{2}/[A-Za-z0-9_-]{6,}/[^\s"'<>`]*'''
    r'''|[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/][^\s"'<>`]*''')
PLACEHOLDER = re.compile(r'(?i)^(?:YOUR_[A-Z0-9_]+|REPLACE_[A-Z0-9_]+|CHANGE_ME[A-Z0-9_]*|EXAMPLE_[A-Z0-9_]+)$')


@dataclass(frozen=True)
class Issue:
    path: str
    category: str
    severity: str = 'error'


class PublishRejected(ValueError):
    """Fixed diagnostics only; never include matched content or subprocess output."""


def path_rejection(path, *, directory=False):
    """Stat-free allowlist, suitable for selecting source before copying.

    addons/*/data XML/CSV declarations are source, unlike runtime data folders.
    Every other directory named data is excluded. Vendored archives have a
    separate content/integrity check; this function only admits their paths.
    """
    raw = str(path)
    relative = PurePosixPath(raw)
    if (not raw or relative.is_absolute() or '\\' in raw or '\x00' in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or any(part in ('', '.', '..') for part in raw.split('/'))):
        return 'unsafe-path'
    parts = relative.parts
    lower = [part.lower() for part in parts]
    if any(part in EXCLUDED_PARTS for part in lower):
        return 'excluded-runtime-or-private-path'
    if 'data' in lower and not (len(parts) >= 3 and parts[0] == 'addons' and parts[2] == 'data'
                                and (directory or relative.suffix in {'.xml', '.csv'})):
        return 'excluded-runtime-data'
    name = parts[-1]
    if name.lower() in PRIVATE_NAMES or relative.suffix.lower() in PRIVATE_SUFFIXES:
        return 'private-or-generated-file'
    if name.startswith('.env') and name not in {'.env.example', '.env.template'}:
        return 'private-environment'
    if len(parts) == 1:
        if name not in SOURCE_DIRECTORIES | ROOT_FILES and relative.suffix.lower() != '.md':
            return 'outside-source-allowlist'
    elif parts[0] not in SOURCE_DIRECTORIES:
        return 'outside-source-allowlist'
    if directory:
        return None
    if len(parts) == 1 and name in ROOT_FILES:
        return None
    if (relative.parent == PurePosixPath('deploy/rtlcss/vendor') and name in VENDOR_ARCHIVES):
        return None
    if name in SPECIAL_SOURCE_NAMES or name.endswith(('.conf.example', '.conf.template')):
        return None
    if relative.suffix.lower() not in SOURCE_SUFFIXES:
        return 'unreviewed-file-type'
    return None


def scan_content(path, payload):
    """Scan all allowed content, including binary assets; never return matches."""
    text = payload.decode('utf-8', errors='ignore')
    issues = []
    for category, pattern in SECRET_PATTERNS:
        matches = pattern.finditer(text)
        if category == 'literal-credential':
            found = any(not PLACEHOLDER.fullmatch(match.group(1)) for match in matches)
        else:
            found = next(matches, None) is not None
        if found:
            issues.append(Issue(str(path), category))
    if payload.startswith(b'PGDMP') or any(marker in payload[:2048] for marker in
            (b'-- PostgreSQL database dump', b'-- MySQL dump', b'SQLite format 3\x00')):
        issues.append(Issue(str(path), 'database-dump-content'))
    if MACHINE_PATH.search(text):
        documentation = PurePosixPath(str(path).split('!', 1)[-1]).suffix.lower() in {'.md', '.html'}
        issues.append(Issue(str(path), 'historical-machine-path' if documentation else 'machine-runtime-path',
                            'warning' if documentation else 'error'))
    return issues


def workflow_issues(path, payload):
    """Conservative stdlib check of literal read-only workflow permissions.

    This intentionally admits a small explicit YAML permissions subset. It
    does not claim to be a YAML parser or evaluate workflow expressions.
    """
    try:
        text = payload.decode('utf-8')
    except UnicodeError:
        return [Issue(path, 'unreadable-workflow-text')]
    issues, explicit = [], False
    lines = text.splitlines()
    for index, original in enumerate(lines):
        line = original.split('#', 1)[0].rstrip()
        match = re.fullmatch(r'''( *)(?:permissions|'permissions'|"permissions")\s*:\s*(.*)''', line)
        if not match:
            if re.search(r'''(?:^|[\s{,])(?:permissions|'permissions'|"permissions")\s*:''', line):
                issues.append(Issue(path, 'workflow-unreviewed-permissions-syntax'))
            continue
        indent, value = len(match.group(1)), match.group(2).strip()
        explicit |= indent == 0
        if value in {'read-all', '{}'}:
            continue
        if value.startswith('{') and value.endswith('}'):
            items = value[1:-1].strip().split(',')
            safe = all(re.fullmatch(r'\s*[a-z][a-z-]*\s*:\s*(?:read|none)\s*', item) for item in items)
        elif not value:
            safe, found = True, False
            for child in lines[index + 1:]:
                child = child.split('#', 1)[0].rstrip()
                if not child.strip():
                    continue
                if len(child) - len(child.lstrip(' ')) <= indent:
                    break
                found = True
                safe &= bool(re.fullmatch(r' +[a-z][a-z-]*\s*:\s*(?:read|none)', child))
            safe &= found
        else:
            safe = False
        if not safe:
            issues.append(Issue(path, 'workflow-write-or-unreviewed-permissions'))
    if not explicit:
        issues.append(Issue(path, 'workflow-readonly-permissions-not-explicit'))
    if re.search(r'\$\{\{(?:(?!\}\}).)*\bsecrets\s*(?:\.|\[)', text, re.S):
        issues.append(Issue(path, 'workflow-secret-context'))
    return issues


def _root(path):
    candidate = Path(path).absolute()
    if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve() != candidate:
        raise PublishRejected('Use an existing canonical candidate directory without linked ancestors.')
    return candidate


def _read_source(root, relative):
    """Walk descriptors with NOFOLLOW, including every ancestor directory."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        descriptors.append(os.open(root, flags | os.O_DIRECTORY))
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1]))
        descriptors.append(os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=descriptors[-1]))
        before = os.fstat(descriptors[-1])
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PublishRejected('unsafe-file-kind-or-link')
        if before.st_size > MAX_FILE_BYTES:
            raise PublishRejected('github-file-size-limit')
        with os.fdopen(descriptors.pop(), 'rb') as stream:
            payload = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(payload) > MAX_FILE_BYTES:
            raise PublishRejected('github-file-size-limit')
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(payload) != before.st_size:
            raise PublishRejected('file-changed-during-check')
        return payload
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _git(root, *arguments):
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
            '-c', 'core.hooksPath=/dev/null', '-C', str(root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=30,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat'})
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise PublishRejected('A readable exact Git candidate root and index are required.') from None


def tracked_entries(root):
    top = os.fsdecode(_git(root, 'rev-parse', '--show-toplevel').strip())
    if Path(top).resolve() != root:
        raise PublishRejected('The candidate must be the exact Git repository root, not a parent repository subdirectory.')
    algorithm = _git(root, 'rev-parse', '--show-object-format').strip().decode('ascii')
    if algorithm not in {'sha1', 'sha256'}:
        raise PublishRejected('Unsupported Git object format.')
    entries = []
    for raw in _git(root, 'ls-files', '--stage', '-z').split(b'\x00'):
        if not raw:
            continue
        try:
            metadata, path = raw.split(b'\t', 1)
            mode, digest, stage = metadata.decode('ascii').split(' ')
            entries.append((os.fsdecode(path), mode, digest, stage, algorithm))
        except (ValueError, UnicodeError):
            raise PublishRejected('The Git index returned an unsupported entry.') from None
    return entries


def tree_entries(root):
    """Yield names only for excluded entries; never descend or open them."""
    pending = [PurePosixPath('.')]
    while pending:
        parent = pending.pop()
        try:
            with os.scandir(root / parent) as stream:
                entries = sorted(stream, key=lambda entry: entry.name)
        except OSError:
            raise PublishRejected('An allowed candidate source directory is unreadable.') from None
        for entry in entries:
            relative = PurePosixPath(entry.name) if parent == PurePosixPath('.') else parent / entry.name
            if relative == PurePosixPath('.git'):
                continue
            rejected = path_rejection(relative, directory=True)
            if rejected:
                yield str(relative), 'excluded', '', '0', ''
                continue
            if entry.is_symlink():
                yield str(relative), '120000', '', '0', ''
            elif entry.is_dir(follow_symlinks=False):
                pending.append(relative)
            else:
                yield str(relative), '100644', '', '0', ''


def _archive_issues(root, relative, payload, admitted):
    """Check a small, pinned build dependency without extracting to disk."""
    path = str(relative)
    if LOCK_PATH not in admitted:
        return [Issue(path, 'vendor-lock-not-in-candidate')]
    try:
        lock = json.loads(_read_source(root, PurePosixPath(LOCK_PATH)))
        expected = 'file:vendor/' + relative.name
        packages = [row for row in lock['packages'].values() if row.get('resolved') == expected]
        integrity = 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode('ascii')
        if len(packages) != 1 or packages[0].get('integrity') != integrity:
            return [Issue(path, 'vendor-archive-integrity')]
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
            expanded = compressed.read(MAX_ARCHIVE_BYTES + 1)
        if len(expanded) > MAX_ARCHIVE_BYTES:
            return [Issue(path, 'vendor-archive-expanded-limit')]
        issues, count = [], 0
        with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
            for member in archive:
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    return [Issue(path, 'vendor-archive-member-limit')]
                member_path = PurePosixPath(member.name)
                if (member_path.is_absolute() or '..' in member_path.parts or '\\' in member.name
                        or any(ord(char) < 32 for char in member.name) or member.issym() or member.islnk()
                        or not (member.isfile() or member.isdir())):
                    return [Issue(path, 'unsafe-vendor-archive-member')]
                if any(part.lower() in EXCLUDED_PARTS | PRIVATE_NAMES for part in member_path.parts):
                    issues.append(Issue(path, 'private-vendor-archive-member'))
                if member.isfile():
                    stream = archive.extractfile(member)
                    content = stream.read(MAX_ARCHIVE_BYTES + 1) if stream else b''
                    if len(content) != member.size or member.size > MAX_ARCHIVE_BYTES:
                        return [Issue(path, 'vendor-archive-expanded-limit')]
                    issues.extend(scan_content(path + '!' + member.name, content))
        return issues if count else [Issue(path, 'empty-vendor-archive')]
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, PublishRejected):
        return [Issue(path, 'unreadable-vendor-lock-or-archive')]


def inspect(root, *, mode='tracked'):
    root = _root(root)
    if mode not in {'tracked', 'tree'}:
        raise PublishRejected('Select tracked or tree candidate scope.')
    entries = list(tracked_entries(root) if mode == 'tracked' else tree_entries(root))
    if not entries or len(entries) > MAX_FILES:
        raise PublishRejected('The candidate is empty or exceeds the bounded file count.')
    admitted = {path for path, *_ in entries if not path_rejection(path)}
    issues, checked, total = [], 0, 0
    seen = set()
    for path, file_mode, digest, stage, algorithm in entries:
        if path in seen:
            issues.append(Issue(path, 'duplicate-or-unmerged-index-entry'))
            continue
        seen.add(path)
        rejected = path_rejection(path)
        if rejected:
            issues.append(Issue(path, rejected))
            continue
        if stage != '0' or file_mode not in {'100644', '100755'}:
            issues.append(Issue(path, 'symlink-submodule-or-unmerged-entry'))
            continue
        relative = PurePosixPath(path)
        try:
            payload = _read_source(root, relative)
        except PublishRejected as error:
            issues.append(Issue(path, str(error)))
            continue
        except OSError:
            issues.append(Issue(path, 'unreadable-missing-or-linked-source'))
            continue
        checked += 1; total += len(payload)
        if total > MAX_TOTAL_BYTES:
            issues.append(Issue(path, 'candidate-total-size-limit'))
            break
        if digest:
            actual = hashlib.new(algorithm, b'blob ' + str(len(payload)).encode('ascii') + b'\x00' + payload).hexdigest()
            if actual != digest:
                issues.append(Issue(path, 'working-file-differs-from-index'))
        issues.extend(scan_content(path, payload))
        if relative.parent == PurePosixPath('.github/workflows') and relative.suffix in {'.yml', '.yaml'}:
            issues.extend(workflow_issues(path, payload))
        if relative.suffix == '.tgz':
            issues.extend(_archive_issues(root, relative, payload, admitted))
    issues = sorted(set(issues), key=lambda issue: (issue.path, issue.severity, issue.category))
    return {'schema': 1, 'mode': mode, 'ok': not any(issue.severity == 'error' for issue in issues),
            'candidate_files': len(entries), 'checked_files': checked, 'checked_bytes': total,
            'history_checked': False, 'issues': [asdict(issue) for issue in issues]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--tree', action='store_true', help='Check a clean pre-Git candidate instead of tracked/index source')
    parser.add_argument('--json', action='store_true', help='Print structured categories and paths only')
    args = parser.parse_args(argv)
    try:
        report = inspect(args.root, mode='tree' if args.tree else 'tracked')
    except PublishRejected as error:
        report = {'schema': 1, 'ok': False, 'issues': [{'path': '.', 'category': 'candidate-scope-rejected', 'severity': 'error'}]}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print('Publication source check: ' + ('PASS' if report['ok'] else 'FAIL'))
        for issue in report['issues']:
            print('%s %s %s' % (issue['severity'].upper(), issue['category'], json.dumps(issue['path'], ensure_ascii=True)))
        if 'checked_files' in report:
            print('%s source files checked; Git history was not scanned.' % report['checked_files'])
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
