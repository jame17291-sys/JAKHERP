import importlib.util
from pathlib import Path
from datetime import date
from decimal import Decimal
import unittest

spec = importlib.util.spec_from_file_location('jakh_operations_pure', Path(__file__).parents[1] / 'services.py')
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)


class CalculationTests(unittest.TestCase):
    def test_commitments_round_line_amounts_without_float_drift(self):
        self.assertEqual(services.commitment([{'quantity': '1', 'unit_price': '.1'}, {'quantity': '1', 'unit_price': '.2'}]), Decimal('.30'))
        self.assertEqual(services.commitment([{'quantity': '3', 'unit_price': '10.005'}]), Decimal('30.02'))

    def test_budget_checks_reserved_commitments(self):
        self.assertEqual(services.availability('1000', '400', '600'), Decimal(0))
        with self.assertRaises(ValueError):
            services.availability('1000', '400', '600.01')

    def test_cumulative_measurements_cap_retention_and_advance(self):
        result = services.certificate_amounts([{'cumulative_quantity': 100, 'prior_quantity': 40, 'contract_quantity': 100, 'unit_price': 10}], 400, 10, 60, 40, 20, 100, 80)
        self.assertEqual(result['period_gross'], Decimal('600.00'))
        self.assertEqual(result['retention_amount'], Decimal('20.00'))
        self.assertEqual(result['recovery_amount'], Decimal('20.00'))
        self.assertEqual(result['net_before_tax'], Decimal('560.00'))

    def test_reduced_or_excess_measurements_and_overdeductions_fail(self):
        for measured in (39, 101):
            with self.assertRaises(ValueError):
                services.certificate_amounts([{'cumulative_quantity': measured, 'prior_quantity': 40, 'contract_quantity': 100, 'unit_price': 10}], 400, 0, 0, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            services.certificate_amounts([{'cumulative_quantity': 50, 'prior_quantity': 40, 'contract_quantity': 100, 'unit_price': 10}], 400, 0, 0, 0, 0, 0, 0, 101)

    def test_approved_omission_creates_a_credit_period_without_new_deductions(self):
        result = services.certificate_amounts([
            {'cumulative_quantity': 10, 'prior_quantity': 10, 'contract_quantity': 10, 'unit_price': 100},
            {'cumulative_quantity': 2, 'prior_quantity': 0, 'contract_quantity': 2, 'unit_price': 75, 'factor': -1},
        ], 1000, 10, 100, 100, 20, 200, 200)
        self.assertEqual(result['period_gross'], Decimal('-150.00'))
        self.assertEqual(result['retention_amount'], Decimal('0'))
        self.assertEqual(result['recovery_amount'], Decimal('0'))
        self.assertEqual(result['net_before_tax'], Decimal('-150.00'))
        with self.assertRaises(ValueError):
            services.certificate_amounts([
                {'cumulative_quantity': 10, 'prior_quantity': 10, 'contract_quantity': 10, 'unit_price': 100},
                {'cumulative_quantity': 2, 'prior_quantity': 0, 'contract_quantity': 2, 'unit_price': 75, 'factor': -1},
            ], 1000, 0, 0, 0, 0, 0, 0, 1)

    def test_variation_factor_is_explicit(self):
        with self.assertRaises(ValueError):
            services.certificate_amounts([
                {'cumulative_quantity': 1, 'prior_quantity': 0, 'contract_quantity': 1, 'unit_price': 10, 'factor': 0},
            ], 0, 0, 0, 0, 0, 0, 0)

    def test_nonfinite_values_fail(self):
        for value in ('NaN', 'Infinity', '-1'):
            with self.assertRaises(ValueError):
                services.decimal(value)

    def test_aging_has_exact_boundaries_and_does_not_invent_due_dates(self):
        as_of = date(2026, 10, 12)
        self.assertEqual(services.aging_bucket(None, as_of), 'undated')
        self.assertEqual(services.aging_bucket(as_of, as_of), 'current')
        self.assertEqual(services.aging_bucket(date(2026, 9, 12), as_of), '1_30')
        self.assertEqual(services.aging_bucket(date(2026, 9, 11), as_of), '31_60')

    def test_translator_reaches_nested_validation_without_changing_arithmetic(self):
        translations = {
            'Quantity': 'الكمية',
            'Measured quantity': 'الكمية المقاسة',
            '%(label)s must be a non-negative finite decimal.': 'يجب أن تكون %(label)s قيمة عشرية محدودة وغير سالبة.',
            'The requested commitment exceeds the released WBS budget.': 'يتجاوز الالتزام المطلوب ميزانية هيكل تجزئة العمل المعتمدة.',
        }
        translate = lambda source: translations.get(source, source)
        with self.assertRaises(ValueError) as error:
            services.commitment([{'quantity': -1, 'unit_price': 1}], translate=translate)
        self.assertEqual(str(error.exception), 'يجب أن تكون الكمية قيمة عشرية محدودة وغير سالبة.')
        with self.assertRaises(ValueError) as error:
            services.availability(100, 0, 101, translate=translate)
        self.assertEqual(str(error.exception), translations['The requested commitment exceeds the released WBS budget.'])
        with self.assertRaises(ValueError) as error:
            services.certificate_amounts([{'cumulative_quantity': -1, 'prior_quantity': 0, 'contract_quantity': 1, 'unit_price': 1}], 0, 0, 0, 0, 0, 0, 0, translate=translate)
        self.assertEqual(str(error.exception), 'يجب أن تكون الكمية المقاسة قيمة عشرية محدودة وغير سالبة.')
        arguments = ([{'cumulative_quantity': 100, 'prior_quantity': 40, 'contract_quantity': 100, 'unit_price': 10}], 400, 10, 60, 40, 20, 100, 80)
        self.assertEqual(services.certificate_amounts(*arguments, translate=translate), services.certificate_amounts(*arguments))


if __name__ == '__main__':
    unittest.main()
