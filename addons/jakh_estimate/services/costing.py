"""Independent construction arithmetic. No Odoo imports or executable snapshots."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

ZERO = Decimal('0')
HUNDRED = Decimal('100')
MAX_AMOUNT = Decimal('90000000000000')
MAX_QUANTITY = Decimal('1000000000')
RESOURCE_TYPES = {'material', 'labour', 'equipment', 'subcontract', 'other'}
UNSAFE_KEYS = {'__proto__', 'constructor', 'prototype'}
VERSION_FIELDS = ('code', 'description', 'cost_code', 'product_id', 'uom', 'quantity', 'unit_cost', 'item_markup_percent', 'resources', 'secondary_uom', 'conversion_factor', 'parent_uuid', 'is_group', 'allocation_percent')


def _identity(message):
    return message


def _object(value, label, translate):
    _ = translate
    if not isinstance(value, dict) or UNSAFE_KEYS.intersection(value):
        raise ValueError(_('%(label)s must be a safe dictionary.') % {'label': label})
    return value


def _decimal(value, label, maximum=MAX_QUANTITY, default=None, *, translate):
    _ = translate
    if value is None and default is not None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(_('%(label)s must be a decimal number.') % {'label': label})
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(_('%(label)s must be a decimal number.') % {'label': label}) from None
    if not result.is_finite() or result < ZERO or result > maximum:
        raise ValueError(_('%(label)s is outside the supported range.') % {'label': label})
    if result.as_tuple().exponent < -6:
        raise ValueError(_('%(label)s supports at most six decimal places.') % {'label': label})
    return result


def _text(value, label, limit=1000, optional=False, *, translate):
    _ = translate
    if optional and value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise ValueError(_('%(label)s must be text.') % {'label': label})
    value = value.strip()
    if (not value and not optional) or len(value) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(_('%(label)s is empty, too long or contains control characters.') % {'label': label})
    return value


def _round(value, decimals):
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def json_value(value):
    """Keep Decimal values exact in JSON snapshots using decimal strings."""
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def calculate_estimate(payload, *, translate=_identity):
    # The ERP supplies its request-scoped translator. Standalone arithmetic
    # stays dependency-free and snapshots never contain translated field keys.
    _ = translate
    _object(payload, _('Estimate'), translate)
    unit_count = payload.get('unit_count', 1)
    if isinstance(unit_count, bool) or not isinstance(unit_count, int) or not 1 <= unit_count <= 100000:
        raise ValueError(_('Repeated units must be an integer between 1 and 100,000.'))
    decimals = payload.get('currency_decimals', 2)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 6:
        raise ValueError(_('Currency precision must be between zero and six.'))
    header = _decimal(payload.get('header_markup_percent', 0), _('Header markup'), Decimal('1000'), translate=translate)
    indirect = _decimal(payload.get('indirect_amount', 0), _('Indirect cost'), MAX_AMOUNT, translate=translate)
    overhead = _decimal(payload.get('overhead_percent', 0), _('Overhead'), Decimal('1000'), translate=translate)
    profit = _decimal(payload.get('profit_percent', 0), _('Profit'), Decimal('1000'), translate=translate)
    lines = payload.get('lines', [])
    if not isinstance(lines, list) or len(lines) > 500:
        raise ValueError(_('An estimate supports up to 500 BOQ lines.'))
    ids, codes, normalized = set(), set(), []
    with localcontext() as context:
        context.prec = 64
        for index, source in enumerate(lines, 1):
            _object(source, _('Line %(number)s') % {'number': index}, translate)
            line_id = _text(source.get('id'), _('Stable line ID'), 100, translate=translate)
            code = _text(source.get('code'), _('BOQ code'), 100, translate=translate)
            if line_id in ids or code in codes or line_id in UNSAFE_KEYS:
                raise ValueError(_('BOQ line IDs and codes must be unique and non-reserved.'))
            ids.add(line_id)
            codes.add(code)
            quantity = _decimal(source.get('quantity'), _('BOQ quantity'), translate=translate)
            rate = _decimal(source.get('unit_cost'), _('Unit cost'), default=0, translate=translate)
            markup = _decimal(source.get('item_markup_percent', 0), _('Item markup'), Decimal('1000'), translate=translate)
            resources = source.get('resources', [])
            if not isinstance(resources, list) or len(resources) > 100:
                raise ValueError(_('A resource assembly supports up to 100 resources.'))
            assembly, resource_cost = [], ZERO
            for resource in resources:
                _object(resource, _('Resource'), translate)
                if resource.get('type') not in RESOURCE_TYPES:
                    raise ValueError(_('Unsupported resource type.'))
                resource_quantity = _decimal(resource.get('quantity'), _('Resource quantity'), translate=translate)
                resource_rate = _decimal(resource.get('unit_cost'), _('Resource unit cost'), translate=translate)
                resource_cost += resource_quantity * resource_rate
                assembly.append({'type': resource['type'], 'description': _text(resource.get('description'), _('Resource description'), translate=translate), 'quantity': resource_quantity, 'unit_cost': resource_rate})
            if assembly:
                rate = _round(resource_cost, 6)
                if rate > MAX_QUANTITY:
                    raise ValueError(_('Resource assembly unit cost exceeds the supported range.'))
            product_id = source.get('product_id') or False
            if product_id is not False and (isinstance(product_id, bool) or not isinstance(product_id, int) or product_id < 1):
                raise ValueError(_('Product reference must be a positive integer or absent.'))
            line = {'id': line_id, 'code': code, 'description': _text(source.get('description'), _('Description'), translate=translate), 'cost_code': _text(source.get('cost_code'), _('Cost code'), 100, True, translate=translate), 'product_id': product_id, 'uom': _text(source.get('uom'), _('Primary unit'), 100, translate=translate), 'quantity': quantity, 'unit_cost': rate, 'item_markup_percent': markup, 'resources': assembly}
            is_group = source.get('is_group', False)
            if not isinstance(is_group, bool):
                raise ValueError(_('BOQ group flag must be a boolean.'))
            line.update(parent_uuid=_text(source.get('parent_uuid'), _('Parent BOQ identity'), 100, True, translate=translate), is_group=is_group,
                        allocation_percent=_decimal(source.get('allocation_percent', 0), _('Parent allocation'), HUNDRED, translate=translate))
            if source.get('source_provenance'):
                provenance = _object(source['source_provenance'], _('BOQ source provenance'), translate)
                allowed = {'batch_id', 'file_id', 'filename', 'sheet', 'row', 'reviewed_by', 'reviewed_at'}
                if set(provenance) - allowed or any(isinstance(value, (dict, list)) for value in provenance.values()):
                    raise ValueError(_('BOQ source provenance must contain only reviewed scalar identifiers.'))
                line['source_provenance'] = dict(provenance)
            if source.get('secondary_uom') or source.get('conversion_factor'):
                secondary = _text(source.get('secondary_uom'), _('Secondary unit'), 100, translate=translate)
                factor = _decimal(source.get('conversion_factor'), _('Secondary units per primary unit'), translate=translate)
                if secondary == line['uom'] or factor == ZERO:
                    raise ValueError(_('Dual units require a different secondary unit and a positive conversion factor.'))
                line.update(secondary_uom=secondary, conversion_factor=factor)
            normalized.append(line)
        buildings = payload.get('buildings', [])
        if not isinstance(buildings, list) or len(buildings) > 1000:
            raise ValueError(_('An estimate supports up to 1,000 explicit building overrides.'))
        building_numbers, adjustments, normalized_buildings = set(), {}, []
        for building in buildings:
            _object(building, _('Building'), translate)
            number = building.get('number')
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= unit_count or number in building_numbers:
                raise ValueError(_('Each building number must be unique and within the repeated-unit count.'))
            building_numbers.add(number)
            overrides = _object(building.get('overrides', {}), _('Building overrides'), translate)
            cleaned = {}
            for line_id, value in overrides.items():
                if line_id not in ids:
                    raise ValueError(_('Building override refers to an unknown BOQ line.'))
                cleaned[line_id] = _decimal(value, _('Building override quantity'), translate=translate)
                adjustments.setdefault(line_id, []).append(cleaned[line_id])
            normalized_buildings.append({'number': number, 'name': _text(building.get('name'), _('Building name'), 160, translate=translate), 'overrides': cleaned})
        cost_total, sell_total = ZERO, ZERO
        for line in normalized:
            overrides = adjustments.get(line['id'], [])
            quantity = line['quantity'] * (unit_count - len(overrides)) + sum(overrides, ZERO)
            if quantity > Decimal('1000000000000'):
                raise ValueError(_('Total quantity exceeds the supported range.'))
            line.update(total_quantity=quantity, cost_total=_round(quantity * line['unit_cost'], decimals))
            if line.get('secondary_uom'):
                line['secondary_quantity'] = _round(quantity * line['conversion_factor'], 6)
        by_id = {line['id']: line for line in normalized}
        groups = [line for line in normalized if line['is_group']]
        for line in normalized:
            parent = by_id.get(line['parent_uuid']) if line['parent_uuid'] else None
            if line['parent_uuid'] and (not parent or not parent['is_group'] or line['is_group'] or parent['parent_uuid']):
                raise ValueError(_('A BOQ allocation must reference a top-level group in the same study.'))
            if line['allocation_percent'] and not parent:
                raise ValueError(_('A percentage allocation requires a parent BOQ group.'))
            if line['allocation_percent']:
                if line['resources'] or line['total_quantity'] <= ZERO:
                    raise ValueError(_('Percentage children require a positive quantity and no independent resource assembly.'))
                line['cost_total'] = _round(parent['cost_total'] * line['allocation_percent'] / HUNDRED, decimals)
                line['unit_cost'] = _round(line['cost_total'] / line['total_quantity'], 6)
                # The parent's priced value is allocated, not independently marked up.
                line['item_markup_percent'] = parent['item_markup_percent']
        for parent in groups:
            children = [line for line in normalized if line['parent_uuid'] == parent['id']]
            if sum((line['allocation_percent'] for line in children), ZERO) > HUNDRED:
                raise ValueError(_('Child percentage allocations exceed the parent BOQ value.'))
            if children and all(line['allocation_percent'] for line in children) and sum((line['allocation_percent'] for line in children), ZERO) == HUNDRED:
                children[-1]['cost_total'] = parent['cost_total'] - sum((line['cost_total'] for line in children[:-1]), ZERO)
                if children[-1]['cost_total'] < ZERO:
                    raise ValueError(_('Percentage allocation rounding exceeds the parent BOQ monetary cap.'))
                children[-1]['unit_cost'] = _round(children[-1]['cost_total'] / children[-1]['total_quantity'], 6)
            if sum((line['cost_total'] for line in children), ZERO) > parent['cost_total']:
                raise ValueError(_('Child direct costs exceed the parent BOQ monetary cap.'))
            parent_price = parent['cost_total'] * (1 + parent['item_markup_percent'] / HUNDRED)
            child_price = sum((line['cost_total'] * (1 + line['item_markup_percent'] / HUNDRED) for line in children), ZERO)
            if child_price > parent_price:
                raise ValueError(_('Child selling values exceed the parent BOQ monetary cap.'))
        leaves = [line for line in normalized if not line['is_group']]
        cost_total = sum((line['cost_total'] for line in leaves), ZERO)
        unallocated_indirect = ZERO
        if indirect and cost_total <= ZERO:
            if payload.get('allow_unallocated_indirect') is True:
                unallocated_indirect = indirect
            else:
                raise ValueError(_('Allocate indirect cost only across positive direct-cost BOQ items.'))
        indirect = _round(indirect, decimals)
        remaining = indirect
        positive = [line for line in leaves if line['cost_total'] > ZERO]
        for line in leaves:
            allocated = ZERO
            if line['cost_total'] > ZERO and indirect:
                allocated = remaining if line is positive[-1] else min(remaining, _round(indirect * line['cost_total'] / cost_total, decimals))
                remaining -= allocated
            line['indirect_allocated'] = allocated
            line['budget_total'] = line['cost_total'] + allocated
            basis = line['unit_cost']
            if line['allocation_percent']:
                basis = line['cost_total'] / line['total_quantity']
            if allocated and line['total_quantity']:
                basis += allocated / line['total_quantity']
            sell_rate = basis * (1 + overhead / HUNDRED) * (1 + profit / HUNDRED) * (1 + line['item_markup_percent'] / HUNDRED) * (1 + header / HUNDRED)
            sell = _round(line['total_quantity'] * sell_rate, decimals)
            if line['cost_total'] > MAX_AMOUNT or sell > MAX_AMOUNT:
                raise ValueError(_('Line amount exceeds the supported monetary range.'))
            line.update(sell_total=sell, selling_unit_rate=sell_rate, margin=sell - line['budget_total'], cost_minor=int(line['cost_total'].scaleb(decimals)), sell_minor=int(sell.scaleb(decimals)))
            sell_total += sell
        for parent in groups:
            children = [line for line in leaves if line['parent_uuid'] == parent['id']]
            parent.update(indirect_allocated=sum((line['indirect_allocated'] for line in children), ZERO),
                          budget_total=sum((line['budget_total'] for line in children), ZERO),
                          sell_total=sum((line['sell_total'] for line in children), ZERO),
                          selling_unit_rate=ZERO, margin=ZERO, cost_minor=0, sell_minor=0)
        if cost_total + indirect > MAX_AMOUNT or sell_total > MAX_AMOUNT:
            raise ValueError(_('Estimate amount exceeds the supported monetary range.'))
        return {'unit_count': unit_count, 'header_markup_percent': header, 'indirect_amount': indirect, 'overhead_percent': overhead, 'profit_percent': profit,
                'unallocated_indirect': unallocated_indirect, 'budget_total': cost_total + indirect - unallocated_indirect,
                'currency_decimals': decimals, 'currency_code': _text(payload.get('currency_code'), _('Currency code'), 10, True, translate=translate), 'buildings': normalized_buildings, 'lines': normalized, 'cost_total': cost_total, 'sell_total': sell_total, 'margin': sell_total - cost_total - indirect + unallocated_indirect, 'cost_minor': int(cost_total.scaleb(decimals)), 'sell_minor': int(sell_total.scaleb(decimals))}


def compare_versions(before, after, *, translate=_identity):
    """Compare trustworthy normalized snapshots by stable IDs, including building quantities."""
    before = calculate_estimate(before, translate=translate)
    after = calculate_estimate(after, translate=translate)
    old = {line['id']: line for line in before['lines']}
    new = {line['id']: line for line in after['lines']}
    changes = {'added': [line for key, line in new.items() if key not in old], 'removed': [line for key, line in old.items() if key not in new], 'modified': []}
    for key, line in new.items():
        if key in old:
            fields = [field for field in VERSION_FIELDS if old[key].get(field) != line.get(field)]
            if fields:
                changes['modified'].append({'id': key, 'changed_fields': fields, 'before': old[key], 'after': line})
    changes['headers'] = {key: {'before': before[key], 'after': after[key]} for key in ('unit_count', 'header_markup_percent', 'indirect_amount', 'overhead_percent', 'profit_percent', 'buildings', 'currency_code') if before[key] != after[key]}
    same_currency = before['currency_code'] == after['currency_code'] and before['currency_decimals'] == after['currency_decimals']
    changes['sell_delta'] = after['sell_total'] - before['sell_total'] if same_currency else None
    changes['cost_delta'] = after['cost_total'] - before['cost_total'] if same_currency else None
    return changes
