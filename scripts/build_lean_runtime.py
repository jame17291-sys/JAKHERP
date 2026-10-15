"""Build a NEW dependency-complete Arabic/English runtime without deleting source.

The native inventory connection is strictly read-only. Installed addons are the
minimum safe removal boundary: an addon with no visible menu can still provide
models, security, assets, or auto-installed glue used by operational modules.
"""
import argparse
import ast
from collections import Counter
import configparser
import hashlib
import json
from pathlib import Path
import re
import shutil
from runtime_bootstrap import DIRECTORY, MODE, initialization_dependencies, module_descriptor_manifest, missing_category_descriptors

LANGUAGES = ('ar_001', 'en_US')
PDF_LOCALES = Path('odoo/addons/web/static/lib/pdfjs/web/locale')
CALENDAR_LOCALES = Path('odoo/addons/web/static/lib/fullcalendar/core/locales-all.global.js')


def permitted_language(code):
    return re.split(r'[_-]', code.lower())[0] in ('ar', 'en')


def validate_active_languages(languages):
    unsupported = [code for code in languages if code not in LANGUAGES]
    if unsupported:
        raise ValueError(f'Unsupported active languages: {unsupported}; use only ar_001 and en_US')


def native_inventory(root):
    """Read installed state, not enrollment metadata, without printing secrets."""
    from native_staging import settings
    import psycopg2
    cfg = settings(root)
    customers = {}
    for key, customer in sorted(cfg['customers'].items()):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(customer['config'])
        options = parser['options']
        with psycopg2.connect(host=options['db_host'], port=int(options['db_port']),
                             user=options['db_user'], password=options['db_password'],
                             dbname=customer['database']) as connection:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name")
                modules = [row[0] for row in cursor.fetchall()]
                cursor.execute('SELECT code FROM res_lang WHERE active ORDER BY code')
                active_languages = [row[0] for row in cursor.fetchall()]
                validate_active_languages(active_languages)
                customers[key] = {'installedModules': modules, 'activeLanguages': active_languages}
    return {'schema': 1, 'languages': list(LANGUAGES), 'customers': customers,
            'installedModules': sorted({name for value in customers.values() for name in value['installedModules']})}


def discover_addons(roots):
    result = {}
    for root_index, root in enumerate(roots):
        for directory in sorted(root.iterdir()):
            if directory.is_symlink():
                raise ValueError(f'Symlink is not allowed in addon source: {directory.name}')
            manifest = directory / '__manifest__.py'
            if directory.is_dir() and manifest.is_file():
                if directory.name in result:
                    raise ValueError(f'Ambiguous addon in more than one source root: {directory.name}')
                value = ast.literal_eval(manifest.read_text())
                if not isinstance(value, dict):
                    raise ValueError(f'Invalid addon manifest: {directory.name}')
                result[directory.name] = {'root': directory, 'manifest': value,
                                          'runtimePrefix': 'vendor/odoo/addons' if root_index == 0 else 'addons'}
    return result


def dependency_closure(installed, addons):
    if not installed or not {'base', 'web'}.issubset(installed):
        raise ValueError('Inventory must include installed base and web addons')
    pending, selected = list(installed), set()
    while pending:
        name = pending.pop()
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', name):
            raise ValueError('Unsafe addon identifier')
        if name in selected:
            continue
        if name not in addons:
            raise ValueError(f'Installed addon or required dependency is missing from source: {name}')
        selected.add(name)
        dependencies = addons[name]['manifest'].get('depends', [])
        if not isinstance(dependencies, list):
            raise ValueError(f'Invalid dependency declaration: {name}')
        pending.extend(dependencies)
    return selected


def slim_calendar(text):
    """Keep the vendor's exact locale definitions; reject unfamiliar bundle shapes."""
    starts = list(re.finditer(r'^    var (l\d+) = \{\n', text, re.M))
    tail = re.search(r'^    var localesAll = \[\s*([l\d,\s]+)\];\s*\n', text, re.M)
    if not starts or not tail or tail.start() <= starts[-1].start():
        raise ValueError('Unrecognized FullCalendar locale bundle; review this vendor version')
    names = [match.group(1) for match in starts]
    if re.findall(r'l\d+', tail.group(1)) != names:
        raise ValueError('FullCalendar locale registration differs from its definitions')
    kept, blocks = [], []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else tail.start()
        block = text[match.start():end]
        codes = re.findall(r"^        code: '([^']+)',\s*$", block, re.M)
        if len(codes) != 1:
            raise ValueError('Unrecognized FullCalendar locale definition')
        if permitted_language(codes[0]):
            if not block.rstrip().endswith('};'):
                raise ValueError('Supported FullCalendar locale has an unfamiliar helper dependency')
            kept.append(match.group(1))
            blocks.append(block)
    if not kept:
        raise ValueError('FullCalendar bundle has no supported locale')
    return text[:starts[0].start()] + ''.join(blocks) + '    var localesAll = [' + ', '.join(kept) + '];\n' + text[tail.end():]


def exclusion(relative, selected, *, vendor):
    parts = relative.parts
    if '__pycache__' in parts or relative.suffix in ('.pyc', '.pyo') or relative.name == '.DS_Store':
        return 'generatedCache'
    addon_parts = parts[2:] if vendor and parts[:2] == ('odoo', 'addons') else (parts if not vendor else ())
    if addon_parts and addon_parts[0] not in selected:
        return 'uninstalledAddon'
    if any(part in ('i18n', 'i18n_extra') for part in parts) and relative.suffix in ('.po', '.mo'):
        if not permitted_language(relative.stem):
            return 'otherLanguageCatalog'
    if vendor and relative.is_relative_to(PDF_LOCALES):
        rest = relative.relative_to(PDF_LOCALES).parts
        if len(rest) > 1 and not permitted_language(rest[0]):
            return 'otherPdfLanguage'
    return None


def build_runtime(source, custom_addons, profile, destination):
    source, custom_addons = Path(source).resolve(strict=True), Path(custom_addons).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Runtime destination must be NEW; existing directories are never overwritten')
    destination = destination.parent.resolve(strict=True) / destination.name
    for root in (source, custom_addons):
        if destination.is_relative_to(root) or root.is_relative_to(destination):
            raise ValueError('Runtime destination must not overlap either source tree')
    if not (source / 'odoo/__main__.py').is_file():
        raise ValueError('A verified vendor source tree is required')
    if profile.get('schema') != 1 or profile.get('languages') != list(LANGUAGES):
        raise ValueError('Unsupported runtime inventory profile')
    addons = discover_addons([source / 'odoo/addons', custom_addons])
    selected = dependency_closure(profile.get('installedModules', []), addons)
    descriptors, bootstrap_assets, bootstrap_notices = initialization_dependencies(addons, selected)
    tasks, omitted = [], Counter()
    source_bytes = 0
    for root, target, vendor in ((source, 'vendor', True), (custom_addons, 'addons', False)):
        for file in sorted(root.rglob('*')):
            if file.is_symlink():
                raise ValueError('Source contains a symlink; review before packaging')
            if not file.is_file():
                continue
            relative, size = file.relative_to(root), file.stat().st_size
            source_bytes += size
            reason = exclusion(relative, selected, vendor=vendor)
            if reason:
                omitted[reason + 'Files'] += 1
                omitted[reason + 'Bytes'] += size
                continue
            # Transform known standalone language resources only; framework locale,
            # currency, country and timezone data remain untouched.
            content = None
            if vendor and relative == PDF_LOCALES / 'locale.json':
                index = json.loads(file.read_text())
                content = (json.dumps({key: value for key, value in index.items() if permitted_language(key)},
                                      ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            elif vendor and relative == CALENDAR_LOCALES:
                content = slim_calendar(file.read_text()).encode()
            tasks.append((file, Path(target) / relative, content))
    for descriptor in descriptors:
        content = (repr(module_descriptor_manifest(descriptor)) + '\n').encode()
        tasks.append((None, Path(DIRECTORY) / descriptor['name'] / '__manifest__.py', content))
        # Native addons-path validation requires this sentinel. It is exactly
        # zero bytes: no models, imports, hooks or other executable addon code.
        tasks.append((None, Path(DIRECTORY) / descriptor['name'] / '__init__.py', b''))
    for dependency in bootstrap_assets + bootstrap_notices:
        relative = Path(dependency['path'])
        original = addons[relative.parts[1]]['root'].joinpath(*relative.parts[2:])
        content = original.read_bytes()
        if len(content) != dependency['bytes'] or hashlib.sha256(content).hexdigest() != dependency['sourceSha256']:
            raise ValueError('Initialization source dependency changed during packaging')
        tasks.append((None, relative, content))
    # All validation precedes creating this new output. Never touch the input tree.
    destination.mkdir(mode=0o755)
    (destination / 'vendor').mkdir()
    (destination / 'addons').mkdir()
    (destination / DIRECTORY).mkdir()
    files, output_bytes = [], 0
    for file, relative, content in tasks:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            shutil.copy2(file, target)
            content = target.read_bytes()
        else:
            target.write_bytes(content)
        output_bytes += len(content)
        files.append({'path': relative.as_posix(), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    report = {'schema': 1, 'languages': list(LANGUAGES), 'inventory': profile,
              'includedModules': sorted(selected), 'omittedModules': sorted(set(addons) - selected),
              'sourceBytes': source_bytes, 'runtimePayloadBytes': output_bytes,
              'savedBytes': source_bytes - output_bytes, 'omitted': dict(sorted(omitted.items())),
              'bootstrapMetadata': {'schema': 2, 'mode': MODE, 'directory': DIRECTORY, 'descriptors': descriptors,
                                    'assets': bootstrap_assets, 'notices': bootstrap_notices},
              'retained': ['installed addon dependency closure', 'framework and locale/country data',
                           'inert original-name initialization metadata and explicit source-hashed native XML PNG dependencies',
                           'licenses and copyright notices', 'tests, migrations and manifest-referenced demo data'],
              'files': files}
    (destination / 'runtime-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--addons', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    inventory = parser.add_mutually_exclusive_group(required=True)
    inventory.add_argument('--native-root', type=Path)
    inventory.add_argument('--profile', type=Path)
    args = parser.parse_args()
    profile = native_inventory(args.native_root) if args.native_root else json.loads(args.profile.read_text())
    report = build_runtime(args.source, args.addons, profile, args.destination)
    print(json.dumps({key: report[key] for key in ('languages', 'sourceBytes', 'runtimePayloadBytes', 'savedBytes', 'omitted')}, indent=2))
    print(f"Retained {len(report['includedModules'])} addons; omitted {len(report['omittedModules'])} uninstalled addons.")
    print('New runtime: ' + str(args.destination.absolute()))


if __name__ == '__main__':
    main()
