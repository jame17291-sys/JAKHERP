"""Inert native identities/assets used ONLY during fresh database initialization.

Native initialization derives base category XML identifiers from addon manifest
metadata, including uninstallable addons. Removing unused addon code must not
remove the metadata needed by retained base data. These generated dictionaries
contain no executable module, dependencies, data, templates or translations.
Folders have literal uninstallable metadata, a zero-byte discovery sentinel,
explicit source-hashed PNG field dependencies and preserved legal notices.
"""
import ast
import hashlib
import io
import json
from pathlib import Path
import re

from lxml import etree
from PIL import Image

DIRECTORY = 'bootstrap_addons'
MODE = 'native_initialization_dependencies'
LEGACY_MODE = 'native_category_descriptors'
PREFIX = 'jakh_bootstrap_category_'
NOTICE_NAMES = {'LICENSE', 'LICENCE', 'COPYRIGHT', 'COPYING', 'NOTICE', 'LICENSE.TXT',
                'NOTICE.TXT', 'COPYRIGHT.TXT', 'LICENSE.MD', 'LICENSE.RST'}


def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _safe_path(value):
    if not isinstance(value, str):
        raise ValueError('Unsafe initialization dependency path')
    path = Path(value)
    if (not path.parts or path.is_absolute() or '..' in path.parts
            or '\\' in value or path.as_posix() != value):
        raise ValueError('Unsafe initialization dependency path')
    return path


def _valid_png(content):
    if not content.startswith(b'\x89PNG\r\n\x1a\n') or len(content) > 2 * 1024 * 1024:
        raise ValueError('Initialization assets must be bounded native PNG images')
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != 'PNG' or image.width * image.height > 16_000_000:
                raise ValueError('Initialization PNG image dimensions are unsafe')
            image.verify()
    except (OSError, SyntaxError) as error:
        raise ValueError('Invalid native initialization PNG image') from error


def module_descriptor(name, addon):
    """Keep original module XML identity, never its operational manifest hooks."""
    manifest = addon['manifest']
    category = manifest.get('category', 'Uncategorized')
    category_identifiers(category)
    return {'name': name, 'category': category, 'label': manifest.get('name') or name,
            'author': manifest.get('author') or 'Upstream contributors',
            'license': manifest.get('license', 'LGPL-3'),
            'sourceManifestSha256': _sha256((addon['root'] / '__manifest__.py').read_bytes())}


def module_descriptor_manifest(descriptor):
    expected = {'name', 'category', 'label', 'author', 'license', 'sourceManifestSha256'}
    if not isinstance(descriptor, dict) or set(descriptor) != expected:
        raise ValueError('Unsafe initialization module descriptor')
    if (not isinstance(descriptor['name'], str) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', descriptor['name'])
            or not isinstance(descriptor['sourceManifestSha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', descriptor['sourceManifestSha256'])):
        raise ValueError('Invalid original module identity or source provenance')
    for field in ('label', 'author', 'license'):
        value = descriptor[field]
        if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(char) < 32 for char in value):
            raise ValueError('Invalid initialization module label or notice')
    category_identifiers(descriptor['category'])
    return {'name': descriptor['label'], 'version': '20.0.1.0.0',
            'category': descriptor['category'], 'summary': 'Inert fresh-database module identity',
            'description': 'Initialization metadata only; not an operational addon.',
            'author': descriptor['author'], 'license': descriptor['license'], 'depends': [], 'data': [],
            'installable': False, 'auto_install': False, 'application': False}


def initialization_dependencies(addons, selected):
    """Only original-name metadata, explicitly referenced images and notices.

    Retained XML can reference base.module_<omitted> or load an omitted module's
    binary icon without declaring an operational dependency. All original
    identities retain the native category universe. Only field file references
    of native bytes/base64 type may bring back bounded PNGs, never code/data.
    """
    omitted = sorted(set(addons) - selected)
    descriptors = [module_descriptor(name, addons[name]) for name in omitted]
    for descriptor in descriptors:
        module_descriptor_manifest(descriptor)
    assets = {}
    for name in sorted(selected):
        addon = addons[name]
        for xml in sorted(addon['root'].rglob('*.xml')):
            if xml.is_symlink() or not xml.resolve().is_relative_to(addon['root'].resolve()):
                raise ValueError('Retained native XML cannot be linked outside its source addon')
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            try:
                tree = etree.parse(str(xml), parser)
            except etree.XMLSyntaxError as error:
                raise ValueError('Retained native XML cannot be scanned safely: ' + str(xml)) from error
            for node in tree.iter():
                reference = node.get('file') if isinstance(node.tag, str) else None
                if not reference:
                    continue
                path = _safe_path(reference)
                module = path.parts[0]
                if module in selected:
                    continue
                if (module not in addons or module not in omitted or len(path.parts) < 3
                        or path.parts[1] != 'static' or path.suffix.lower() != '.png'
                        or node.tag != 'field' or node.get('type') not in ('bytes', 'base64')):
                    raise ValueError('Review an unsupported external native XML file dependency: ' + reference)
                original = addons[module]['root'].joinpath(*path.parts[1:])
                if (original.is_symlink() or not original.is_file()
                        or not original.resolve().is_relative_to(addons[module]['root'].resolve())
                        or any(parent.is_symlink() for parent in original.parents if parent.is_relative_to(addons[module]['root']))):
                    raise ValueError('External initialization image is missing or linked: ' + reference)
                content = original.read_bytes()
                _valid_png(content)
                relative = (Path(DIRECTORY) / path).as_posix()
                xml_relative = addon.get('runtimePrefix', 'vendor/odoo/addons') + '/' + name + '/' + xml.relative_to(addon['root']).as_posix()
                item = assets.setdefault(relative, {'path': relative, 'bytes': len(content),
                    'sourceSha256': _sha256(content), 'references': []})
                if xml_relative not in item['references']:
                    item['references'].append(xml_relative)
    notices = []
    for name in omitted:
        for file in sorted(addons[name]['root'].iterdir()):
            if file.is_file() and file.name.upper() in NOTICE_NAMES:
                if file.is_symlink():
                    raise ValueError('Initialization legal notices cannot be linked')
                content = file.read_bytes()
                notices.append({'path': (Path(DIRECTORY) / name / file.name).as_posix(),
                                'bytes': len(content), 'sourceSha256': _sha256(content)})
    return descriptors, list(assets.values()), notices


def category_identifiers(category):
    """Mirror native db.create_categories normalization without altering source."""
    if not isinstance(category, str) or not category or len(category) > 512 or any(ord(char) < 32 for char in category):
        raise ValueError('Invalid native addon category metadata')
    identifier = 'module_category'
    result = []
    for component in category.split('/'):
        if not component:
            raise ValueError('Empty native addon category component')
        identifier += '_' + component.lower().replace('&', 'and').replace(' ', '_')
        result.append(identifier)
    return result


def descriptor_name(category):
    return PREFIX + hashlib.sha256(category.encode()).hexdigest()[:20]


def descriptor_manifest(category):
    category_identifiers(category)
    return {'name': 'JAKH initialization category metadata', 'version': '20.0.1.0.0',
            'category': category, 'summary': 'Inert fresh-database category descriptor',
            'description': 'Initialization metadata only; not an operational addon.',
            'author': 'JAKH', 'license': 'LGPL-3', 'depends': [], 'data': [],
            'installable': False, 'auto_install': False, 'application': False}


def missing_category_descriptors(addons, selected):
    """Retain each missing native category branch once, not omitted module code.

    Only maximal branches need dictionaries: native initialization creates their
    parents automatically. Preserve the verified source category universe so a
    retained base data reference cannot depend on an omitted addon by accident.
    """
    present = set()
    branches = {}
    for name, addon in sorted(addons.items()):
        category = addon['manifest'].get('category', 'Uncategorized')
        identifiers = category_identifiers(category)
        branches.setdefault(identifiers[-1], (category, identifiers))
        if name in selected:
            present.update(identifiers)
    missing = {identifier for _, identifiers in branches.values() for identifier in identifiers} - present
    candidates = [(category, identifiers) for category, identifiers in branches.values() if identifiers[-1] in missing]
    ancestors = {identifier for _, identifiers in candidates for identifier in identifiers[:-1]}
    categories = sorted(category for category, identifiers in candidates if identifiers[-1] not in ancestors)
    return [{'name': descriptor_name(category), 'category': category} for category in categories]


def _validate_legacy_metadata(directory, report=None, *, required=False):
    """Reject executable or installable content even if its file hash was edited."""
    directory = Path(directory)
    marker = directory / 'runtime-manifest.json'
    if report is None:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError('A generated lean runtime manifest is required for initialization')
        report = json.loads(marker.read_text())
    if not isinstance(report, dict) or report.get('schema') != 1 or report.get('languages') != ['ar_001', 'en_US']:
        raise ValueError('Unsupported lean runtime manifest for initialization')
    metadata = report.get('bootstrapMetadata')
    root = directory / DIRECTORY
    if metadata is None:
        if required or root.exists() or root.is_symlink():
            raise ValueError('Rebuild the lean runtime with fresh-initialization category metadata')
        return None
    if (not isinstance(metadata, dict) or set(metadata) != {'schema', 'mode', 'directory', 'descriptors'}
            or metadata.get('schema') != 1 or metadata.get('mode') != LEGACY_MODE or metadata.get('directory') != DIRECTORY
            or not isinstance(metadata.get('descriptors'), list)):
        raise ValueError('Unsupported initialization category metadata')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Initialization metadata directory is missing or linked')
    expected = set()
    expected_directories = set()
    records = {record['path']: record for record in report.get('files', [])}
    categories = set()
    for descriptor in metadata['descriptors']:
        if not isinstance(descriptor, dict) or set(descriptor) != {'name', 'category'}:
            raise ValueError('Unsafe initialization category descriptor')
        category = descriptor['category']
        manifest = descriptor_manifest(category)
        name = descriptor_name(category)
        if descriptor['name'] != name or category in categories or name in report.get('includedModules', []):
            raise ValueError('Duplicate or operational initialization category descriptor')
        categories.add(category)
        relative = Path(DIRECTORY) / name / '__manifest__.py'
        expected.add(relative)
        expected_directories.add(relative.parent)
        file = directory / relative
        if file.is_symlink() or not file.is_file() or file.parent.is_symlink():
            raise ValueError('Initialization category dictionary is missing or linked')
        content = file.read_bytes()
        record = records.get(relative.as_posix())
        if not record or len(content) != record['bytes'] or hashlib.sha256(content).hexdigest() != record['sha256']:
            raise ValueError('Initialization category dictionary has changed since packaging')
        try:
            value = ast.literal_eval(content.decode())
        except (ValueError, SyntaxError, UnicodeError) as error:
            raise ValueError('Initialization category dictionary is not inert literal metadata') from error
        if (not isinstance(value, dict) or value != manifest or set(value) != set(manifest)
                or any(type(value[key]) is not type(expected_value) for key, expected_value in manifest.items())):
            raise ValueError('Initialization category dictionary must remain inert and uninstallable')
        initializer_relative = relative.with_name('__init__.py')
        expected.add(initializer_relative)
        initializer = directory / initializer_relative
        if initializer.is_symlink() or not initializer.is_file():
            raise ValueError('Initialization category requires an empty native initializer sentinel')
        initializer_content = initializer.read_bytes()
        if initializer_content != b'':
            raise ValueError('Initialization category initializer must remain exactly zero bytes')
        initializer_record = records.get(initializer_relative.as_posix())
        if (not initializer_record or initializer_record['bytes'] != 0
                or initializer_record['sha256'] != hashlib.sha256(b'').hexdigest()):
            raise ValueError('Initialization category initializer has changed since packaging')
    actual = set()
    for file in root.rglob('*'):
        if file.is_symlink():
            raise ValueError('Initialization metadata contains a symbolic link')
        if file.is_file():
            actual.add(file.relative_to(directory))
        elif file.is_dir() and file.relative_to(directory) not in expected_directories:
            raise ValueError('Initialization metadata cannot contain unreviewed code or data directories')
    reported = {Path(record['path']) for record in report.get('files', []) if Path(record['path']).parts[:1] == (DIRECTORY,)}
    if actual != expected or reported != expected:
        raise ValueError('Initialization metadata may contain only reviewed manifest dictionaries and empty native initializer sentinels')
    return root


def validate_bootstrap_metadata(directory, report=None, *, required=False):
    """Validate inert identities and exact source-hashed XML image dependencies.

    Bootstrap paths never join the serving runtime. Old category-only packages
    remain verifiable for rollback, while new initialization uses original IDs.
    Even a self-consistent manifest cannot permit executable addon content.
    """
    directory = Path(directory)
    marker = directory / 'runtime-manifest.json'
    if report is None:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError('A generated lean runtime manifest is required for initialization')
        report = json.loads(marker.read_text())
    if not isinstance(report, dict) or report.get('schema') != 1 or report.get('languages') != ['ar_001', 'en_US']:
        raise ValueError('Unsupported lean runtime manifest for initialization')
    metadata = report.get('bootstrapMetadata')
    root = directory / DIRECTORY
    if metadata is None or (isinstance(metadata, dict) and metadata.get('schema') == 1):
        return _validate_legacy_metadata(directory, report, required=required)
    if (not isinstance(metadata, dict) or set(metadata) != {'schema', 'mode', 'directory', 'descriptors', 'assets', 'notices'}
            or metadata.get('schema') != 2 or metadata.get('mode') != MODE or metadata.get('directory') != DIRECTORY
            or any(not isinstance(metadata.get(key), list) for key in ('descriptors', 'assets', 'notices'))):
        raise ValueError('Unsupported initialization dependency metadata')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Initialization metadata directory is missing or linked')
    records = {record['path']: record for record in report.get('files', [])}
    expected, expected_directories, module_names = set(), set(), set()
    def validate_file(relative, *, source_sha=False, size=None):
        relative = _safe_path(relative)
        if relative.parts[0] != DIRECTORY or len(relative.parts) < 3:
            raise ValueError('Initialization dependencies must stay in their original module folder')
        if relative in expected:
            raise ValueError('Duplicate initialization dependency file')
        expected.add(relative)
        parent = relative.parent
        while parent != Path(DIRECTORY):
            expected_directories.add(parent)
            parent = parent.parent
        file = directory / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError('Initialization dependency file is missing or linked')
        content = file.read_bytes()
        record = records.get(relative.as_posix())
        if (not record or len(content) != record['bytes'] or _sha256(content) != record['sha256']
                or (source_sha and _sha256(content) != source_sha) or (size is not None and len(content) != size)):
            raise ValueError('Initialization dependency has changed since verified source packaging')
        return content
    for descriptor in metadata['descriptors']:
        manifest = module_descriptor_manifest(descriptor)
        name = descriptor['name']
        if name in module_names or name in report.get('includedModules', []):
            raise ValueError('Duplicate or operational initialization module descriptor')
        module_names.add(name)
        relative = Path(DIRECTORY) / name / '__manifest__.py'
        content = validate_file(relative.as_posix())
        try:
            value = ast.literal_eval(content.decode())
        except (ValueError, SyntaxError, UnicodeError) as error:
            raise ValueError('Initialization category dictionary is not inert literal metadata') from error
        if (not isinstance(value, dict) or value != manifest or set(value) != set(manifest)
                or any(type(value[key]) is not type(expected_value) for key, expected_value in manifest.items())):
            raise ValueError('Initialization category dictionary must remain inert and uninstallable')
        initializer_relative = relative.with_name('__init__.py')
        initializer = directory / initializer_relative
        if initializer.is_symlink() or not initializer.is_file():
            raise ValueError('Initialization category requires an empty native initializer sentinel')
        if initializer.read_bytes() != b'':
            raise ValueError('Initialization category initializer must remain exactly zero bytes')
        validate_file(initializer_relative.as_posix(), source_sha=_sha256(b''), size=0)
    if module_names != set(report.get('omittedModules', [])):
        raise ValueError('Initialization descriptors must preserve exactly the original omitted-module identities')
    for asset in metadata['assets']:
        if not isinstance(asset, dict) or set(asset) != {'path', 'bytes', 'sourceSha256', 'references'}:
            raise ValueError('Unsafe initialization asset metadata')
        relative = _safe_path(asset['path'])
        if (len(relative.parts) < 4 or relative.parts[:1] != (DIRECTORY,) or relative.parts[1] not in module_names
                or relative.parts[2] != 'static' or relative.suffix.lower() != '.png'
                or not isinstance(asset['bytes'], int) or asset['bytes'] < 1
                or not isinstance(asset['sourceSha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', asset['sourceSha256'])
                or not isinstance(asset['references'], list) or not asset['references']):
            raise ValueError('Only explicitly referenced native PNGs may be initialization assets')
        content = validate_file(asset['path'], source_sha=asset['sourceSha256'], size=asset['bytes'])
        _valid_png(content)
        original_reference = '/'.join(relative.parts[1:])
        for reference in asset['references']:
            xml_relative = _safe_path(reference)
            if (xml_relative.suffix != '.xml' or xml_relative.as_posix() not in records
                    or xml_relative.parts[0] == DIRECTORY):
                raise ValueError('Initialization PNG must be referenced by retained native XML')
            xml = directory / xml_relative
            if (xml.is_symlink() or not xml.is_file() or _sha256(xml.read_bytes()) != records[reference]['sha256']):
                raise ValueError('Retained native XML asset provenance has changed')
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            tree = etree.parse(str(xml), parser)
            if not any(node.tag == 'field' and node.get('type') in ('bytes', 'base64')
                       and node.get('file') == original_reference for node in tree.iter() if isinstance(node.tag, str)):
                raise ValueError('Initialization PNG lacks an explicit native bytes/base64 XML reference')
    for notice in metadata['notices']:
        if not isinstance(notice, dict) or set(notice) != {'path', 'bytes', 'sourceSha256'}:
            raise ValueError('Unsafe initialization legal-notice metadata')
        relative = _safe_path(notice['path'])
        if (len(relative.parts) != 3 or relative.parts[0] != DIRECTORY or relative.parts[1] not in module_names
                or relative.name.upper() not in NOTICE_NAMES or not isinstance(notice['bytes'], int)
                or not isinstance(notice['sourceSha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', notice['sourceSha256'])):
            raise ValueError('Only source legal notices may accompany initialization metadata')
        validate_file(notice['path'], source_sha=notice['sourceSha256'], size=notice['bytes'])
    actual = set()
    for file in root.rglob('*'):
        if file.is_symlink():
            raise ValueError('Initialization metadata contains a symbolic link')
        if file.is_file():
            actual.add(file.relative_to(directory))
        elif file.is_dir() and file.relative_to(directory) not in expected_directories:
            raise ValueError('Initialization metadata cannot contain unreviewed code or data directories')
    reported = {Path(record['path']) for record in report.get('files', []) if Path(record['path']).parts[:1] == (DIRECTORY,)}
    if actual != expected or reported != expected:
        raise ValueError('Initialization metadata may contain only reviewed manifest dictionaries, empty native initializer sentinels, explicit native PNG dependencies and legal notices')
    return root
