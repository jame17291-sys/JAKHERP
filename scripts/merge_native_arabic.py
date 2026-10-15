"""Merge reviewed Arabic terms into native, exported Odoo translation references.

This is a release-time mechanical catalog merge, not a translation runtime.
Missing translations fail before any catalog is rewritten. Historical terms and
references are retained; English remains native source text.
"""
import argparse
import json
from pathlib import Path

import polib


PACKAGE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--templates', type=Path, required=True)
    parser.add_argument('--mapping', type=Path, action='append', default=[])
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    reviewed = {}
    for path in args.mapping:
        for module, terms in json.loads(path.read_text()).items():
            if not module.startswith('jakh_') or not isinstance(terms, dict):
                raise ValueError('Use reviewed custom-addon term dictionaries')
            reviewed.setdefault(module, {}).update(terms)
    pending, missing = {}, {}
    for template in sorted(args.templates.glob('jakh_*.pot')):
        module = template.stem
        target = PACKAGE / 'addons' / module / 'i18n/ar.po'
        if not target.is_file():
            raise ValueError('Native Arabic target is missing: ' + module)
        catalog = polib.pofile(target)
        index = {entry.msgid: entry for entry in catalog}
        translations = {entry.msgid: entry.msgstr for entry in catalog if entry.msgstr}
        # Reviewed development fragments are merge inputs, never loaded instead
        # of the canonical native ar.po catalog.
        for fragment in sorted(target.parent.glob('ar_*.po')):
            translations.update({entry.msgid: entry.msgstr for entry in polib.pofile(fragment) if entry.msgstr})
        translations.update(reviewed.get(module, {}))
        for entry in polib.pofile(template):
            if not entry.msgid:
                continue
            translation = translations.get(entry.msgid)
            if not translation:
                missing.setdefault(module, []).append(entry.msgid)
                continue
            current = index.get(entry.msgid)
            if current is None:
                current = polib.POEntry(msgid=entry.msgid, msgstr=translation)
                catalog.append(current)
                index[entry.msgid] = current
            current.msgstr = translation
            current.flags = [flag for flag in current.flags if flag != 'fuzzy']
            current.occurrences = sorted(set(current.occurrences + entry.occurrences))
            current.comment = '\n'.join(sorted(set(filter(None, (current.comment + '\n' + entry.comment).splitlines()))))
        pending[target] = catalog
    if not pending:
        raise ValueError('Export native templates before merging')
    if missing:
        print(json.dumps({'missing': missing}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    if not args.check:
        for path, catalog in pending.items():
            catalog.save(path)
    print(json.dumps({'mode': 'check' if args.check else 'merge',
                      'catalogs': {path.parent.parent.name: len(catalog) for path, catalog in pending.items()}}, indent=2))


if __name__ == '__main__':
    main()
