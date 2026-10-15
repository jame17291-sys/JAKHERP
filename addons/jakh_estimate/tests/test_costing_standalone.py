"""Run directly with Python; this file deliberately does not import Odoo."""
import importlib.util
from pathlib import Path
from decimal import Decimal
import json
import unittest

SPEC = importlib.util.spec_from_file_location('jakh_costing', Path(__file__).resolve().parents[1] / 'services' / 'costing.py')
costing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(costing)


def line(**changes):
    return {'id': 'stable-line-1', 'code': '01.02.003', 'description': 'Illustrative concrete frame', 'cost_code': 'CIV-CON', 'uom': 'm³', 'quantity': 10, 'unit_cost': 100, 'item_markup_percent': 3, **changes}


class TestCosting(unittest.TestCase):
    def test_parent_children_cap_and_no_double_counting(self):
        group = line(id='group', code='G', quantity=1, unit_cost=1000, item_markup_percent=10, is_group=True)
        children = [line(id='a', code='A', quantity=3, unit_cost=0, parent_uuid='group', allocation_percent=40),
                    line(id='b', code='B', quantity=2, unit_cost=0, parent_uuid='group', allocation_percent=60)]
        result = costing.calculate_estimate({'lines': [group, *children]})
        self.assertEqual(result['cost_total'], Decimal('1000.00'))
        self.assertEqual(result['sell_total'], Decimal('1100.00'))
        self.assertEqual(result['lines'][1]['cost_total'], Decimal('400.00'))
        snapshot = json.loads(json.dumps(costing.json_value(result)))
        self.assertEqual(costing.calculate_estimate(snapshot)['sell_total'], result['sell_total'])
        self.assertEqual(costing.compare_versions(snapshot, snapshot)['modified'], [])
        children[1]['allocation_percent'] = 61
        with self.assertRaisesRegex(ValueError, 'exceed'):
            costing.calculate_estimate({'lines': [group, *children]})

    def test_parent_manual_monetary_and_selling_caps(self):
        group = line(id='group', code='G', quantity=1, unit_cost=100, item_markup_percent=0, is_group=True)
        for child in [line(parent_uuid='group', quantity=2, unit_cost=60, item_markup_percent=0),
                      line(parent_uuid='group', quantity=1, unit_cost=100, item_markup_percent=1),
                      line(parent_uuid='unknown'), line(parent_uuid='group', is_group=True)]:
            with self.subTest(child=child), self.assertRaises(ValueError):
                costing.calculate_estimate({'lines': [group, child]})

    def test_separate_indirect_overhead_profit_and_rounding(self):
        result = costing.calculate_estimate({'lines': [line(quantity=1, unit_cost=200, item_markup_percent=0),
                                                      line(id='second', code='02', quantity=1, unit_cost=100, item_markup_percent=0)],
                                             'indirect_amount': 30, 'overhead_percent': 10, 'profit_percent': 20})
        self.assertEqual(result['cost_total'], Decimal('300.00'))
        self.assertEqual(result['budget_total'], Decimal('330.00'))
        self.assertEqual(result['sell_total'], Decimal('435.60'))
        self.assertEqual([row['indirect_allocated'] for row in result['lines']], [Decimal('20.00'), Decimal('10.00')])
        self.assertEqual(result['margin'], Decimal('105.60'))
        self.assertEqual(costing.calculate_estimate(json_value := costing.json_value(result))['sell_total'], result['sell_total'])
        tiny = costing.calculate_estimate({'lines': [line(id=str(i), code=str(i), quantity=1, unit_cost=1, item_markup_percent=0) for i in range(3)], 'indirect_amount': '0.02'})
        self.assertEqual(sum(row['indirect_allocated'] for row in tiny['lines']), Decimal('0.02'))
        with self.assertRaises(ValueError):
            costing.calculate_estimate({'lines': [line(quantity=0)], 'indirect_amount': 1})
        draft = costing.calculate_estimate({'lines': [], 'indirect_amount': 100, 'allow_unallocated_indirect': True})
        self.assertEqual(draft['unallocated_indirect'], Decimal('100'))
        self.assertEqual(draft['budget_total'], Decimal('0.00'))
        self.assertEqual(draft['sell_total'], Decimal('0.00'))
        with self.assertRaises(ValueError):
            costing.calculate_estimate({'lines': [], 'indirect_amount': 100})

    def test_repeated_buildings_and_multiplicative_markup(self):
        result = costing.calculate_estimate({'lines': [line()], 'unit_count': 11, 'header_markup_percent': 8})
        self.assertEqual(result['lines'][0]['total_quantity'], Decimal('110'))
        self.assertEqual(result['cost_total'], Decimal('11000.00'))
        self.assertEqual(result['sell_total'], Decimal('12236.40'))
        self.assertEqual(result['sell_minor'], 1223640)

    def test_fractional_rates_round_each_final_line_once(self):
        result = costing.calculate_estimate({'lines': [line(quantity='0.1', unit_cost='0.2', item_markup_percent=0), line(id='other', code='02', quantity=3, unit_cost='0.333333', item_markup_percent=0)]})
        self.assertEqual(result['cost_total'], Decimal('1.02'))
        self.assertEqual(result['lines'][1]['cost_total'], Decimal('1.00'))

    def test_resources_determine_unit_cost(self):
        resources = [{'type': 'material', 'description': 'Sample material', 'quantity': '0.1', 'unit_cost': '0.2'}, {'type': 'labour', 'description': 'Sample labour', 'quantity': '0.2', 'unit_cost': '0.3'}]
        result = costing.calculate_estimate({'unit_count': 11, 'lines': [line(unit_cost=999, item_markup_percent=0, resources=resources)]})
        self.assertEqual(result['lines'][0]['unit_cost'], Decimal('0.080000'))
        self.assertEqual(result['cost_total'], Decimal('8.80'))

    def test_building_overrides_replace_not_add_quantities(self):
        result = costing.calculate_estimate({'unit_count': 11, 'lines': [line()], 'buildings': [{'number': 1, 'name': 'Villa 01', 'overrides': {'stable-line-1': 0}}, {'number': 2, 'name': 'Villa 02', 'overrides': {'stable-line-1': 12}}]})
        self.assertEqual(result['lines'][0]['total_quantity'], Decimal('102'))
        self.assertEqual(result['cost_total'], Decimal('10200.00'))

    def test_dual_units_keep_primary_pricing_basis(self):
        result = costing.calculate_estimate({'lines': [line(quantity=3, secondary_uom='batch', conversion_factor='0.5')]})
        self.assertEqual(result['lines'][0]['secondary_quantity'], Decimal('1.500000'))
        self.assertEqual(result['lines'][0]['cost_total'], Decimal('300.00'))

    def test_version_ids_match_independently_of_code_and_position(self):
        before = {'lines': [line(), line(id='removed', code='02')]}
        after = {'lines': [line(id='added', code='03'), line(code='01.02.004', quantity=12)]}
        comparison = costing.compare_versions(before, after)
        self.assertEqual([item['id'] for item in comparison['added']], ['added'])
        self.assertEqual([item['id'] for item in comparison['removed']], ['removed'])
        self.assertEqual(comparison['modified'][0]['changed_fields'], ['code', 'quantity'])

    def test_building_changes_and_currency_changes_are_explicit(self):
        before = {'currency_code': 'SAR', 'unit_count': 11, 'lines': [line()]}
        after = {**before, 'buildings': [{'number': 1, 'name': 'Villa 01', 'overrides': {'stable-line-1': 12}}]}
        comparison = costing.compare_versions(before, after)
        self.assertIn('buildings', comparison['headers'])
        self.assertEqual(comparison['sell_delta'], Decimal('206.00'))
        foreign = costing.compare_versions(before, {**before, 'currency_code': 'USD'})
        self.assertIsNone(foreign['sell_delta'])

    def test_json_snapshot_roundtrip_retains_exact_cost_and_version_basis(self):
        payload = {'lines': [line()], 'unit_count': 11, 'header_markup_percent': 8, 'currency_code': 'SAR'}
        first = costing.calculate_estimate(payload)
        decoded = json.loads(json.dumps(costing.json_value(first)))
        second = costing.calculate_estimate(decoded)
        self.assertEqual(first['sell_minor'], second['sell_minor'])
        self.assertEqual(costing.compare_versions(decoded, decoded)['modified'], [])

    def test_malformed_inputs_and_duplicate_ids_are_rejected(self):
        for quantity in [-1, float('nan'), float('inf'), '', True, '0.0000001']:
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                costing.calculate_estimate({'lines': [line(quantity=quantity)]})
        for payload in [
            {'lines': [line(), line(code='02')]},
            {'lines': [line(), line(id='second')]},
            {'lines': [line(id='__proto__')]},
            {'lines': [line(product_id={'unsafe': 'object'})]},
            {'lines': [line(resources=[{'type': 'unknown'}])]},
            {'lines': [line(secondary_uom='batch', conversion_factor=0)]},
            {'lines': [line()], 'unit_count': 1.5},
            {'lines': [line()], 'unit_count': 0},
            {'lines': [line()], 'header_markup_percent': 1001},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                costing.calculate_estimate(payload)

    def test_bounds_and_invalid_building_references(self):
        for payload in [
            {'lines': [line(id=str(i), code=str(i)) for i in range(501)]},
            {'lines': [line(quantity=1000000000, unit_cost=1000000000)]},
            {'lines': [line()], 'unit_count': 100001},
            {'lines': [line()], 'buildings': [{'number': 2, 'name': 'Outside', 'overrides': {}}]},
            {'lines': [line()], 'buildings': [{'number': 1, 'name': 'Unknown', 'overrides': {'unknown': 1}}]},
            {'lines': [line()], 'buildings': [{'number': 1, 'name': 'A', 'overrides': {}}, {'number': 1, 'name': 'B', 'overrides': {}}]},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                costing.calculate_estimate(payload)

    def test_translator_localizes_errors_and_labels_without_changing_snapshots(self):
        translations = {
            'BOQ quantity': 'كمية جدول الكميات',
            '%(label)s is outside the supported range.': '%(label)s خارج النطاق المدعوم.',
            'Repeated units must be an integer between 1 and 100,000.': 'عدد الوحدات المتكررة يجب أن يكون عدداً صحيحاً بين 1 و100,000.',
            'Line %(number)s': 'البند %(number)s',
            '%(label)s must be a safe dictionary.': 'يجب أن يكون %(label)s قاموساً آمناً.',
        }
        translate = lambda source: translations.get(source, source)
        cases = [
            ({'lines': [line(quantity=-1)]}, 'كمية جدول الكميات خارج النطاق المدعوم.'),
            ({'unit_count': 0}, translations['Repeated units must be an integer between 1 and 100,000.']),
            ({'lines': [None]}, 'يجب أن يكون البند 1 قاموساً آمناً.'),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError) as error:
                costing.calculate_estimate(payload, translate=translate)
            self.assertEqual(str(error.exception), expected)
        valid = {'lines': [line()], 'unit_count': 11, 'currency_code': 'SAR'}
        self.assertEqual(costing.calculate_estimate(valid, translate=translate), costing.calculate_estimate(valid))
        with self.assertRaises(ValueError) as error:
            costing.compare_versions(valid, {'lines': [line(quantity=-1)]}, translate=translate)
        self.assertEqual(str(error.exception), 'كمية جدول الكميات خارج النطاق المدعوم.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
