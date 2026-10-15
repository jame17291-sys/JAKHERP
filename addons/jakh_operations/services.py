"""Pure Decimal policy calculations; no Odoo dependency or ledger posting."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _identity(message):
    return message


def decimal(value, label=None, *, translate=_identity):
    _ = translate
    if label is None:
        label = _('Amount')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(_('%(label)s must be a finite decimal.') % {'label': label})
    if not result.is_finite() or result < 0:
        raise ValueError(_('%(label)s must be a non-negative finite decimal.') % {'label': label})
    return result


def money(value, places=2, *, translate=_identity):
    _ = translate
    if places not in range(0, 5):
        raise ValueError(_('Unsupported currency precision.'))
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def commitment(lines, places=2, *, translate=_identity):
    _ = translate
    return sum((money(
        decimal(line['quantity'], _('Quantity'), translate=translate)
        * decimal(line['unit_price'], _('Unit price'), translate=translate),
        places, translate=translate) for line in lines), Decimal(0))


def availability(budget, reserved, requested, places=2, *, translate=_identity):
    _ = translate
    remaining = money(decimal(budget, translate=translate) - decimal(reserved, translate=translate), places, translate=translate)
    required = money(decimal(requested, translate=translate), places, translate=translate)
    if required > remaining:
        raise ValueError(_('The requested commitment exceeds the released WBS budget.'))
    return remaining - required


def certificate_amounts(lines, prior_gross, retention_percent, retention_cap, prior_retention,
                        recovery_percent, advance_cap, prior_recovery, other_deductions=0, places=2,
                        *, translate=_identity):
    """Quantities are cumulative; ``factor`` permits a controlled omission/credit.

    Base BOQ lines use the default factor of one.  Approved variation lines use
    one for an addition and minus one for an omission.  Retention and advance
    recovery are never created on a negative period adjustment; a negative
    period is invoiced through the native customer-credit workflow.
    """
    _ = translate
    gross = Decimal(0)
    for line in lines:
        quantity = decimal(line['cumulative_quantity'], _('Measured quantity'), translate=translate)
        prior = decimal(line['prior_quantity'], _('Previous quantity'), translate=translate)
        limit = decimal(line['contract_quantity'], _('Contract quantity'), translate=translate)
        if quantity < prior or quantity > limit:
            raise ValueError(_('Cumulative quantities must be between prior approved and contracted quantities.'))
        try:
            factor = Decimal(str(line.get('factor', 1)))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(_('Variation factor must be either one or minus one.'))
        if factor not in (Decimal(-1), Decimal(1)):
            raise ValueError(_('Variation factor must be either one or minus one.'))
        gross += money(factor * quantity * decimal(line['unit_price'], translate=translate), places, translate=translate)
    gross = money(gross, places, translate=translate)
    previous = money(decimal(prior_gross, translate=translate), places, translate=translate)
    period = gross - previous
    retention_rate = decimal(retention_percent, _('Retention percentage'), translate=translate)
    recovery_rate = decimal(recovery_percent, _('Recovery percentage'), translate=translate)
    if retention_rate > 100 or recovery_rate > 100:
        raise ValueError(_('Percentages cannot exceed 100.'))
    other = money(decimal(other_deductions, translate=translate), places, translate=translate)
    if period < 0:
        if other:
            raise ValueError(_('A negative certificate adjustment cannot include other deductions.'))
        retention = recovery = Decimal(0)
    else:
        retention_left = max(Decimal(0), money(decimal(retention_cap, translate=translate) - decimal(prior_retention, translate=translate), places, translate=translate))
        recovery_left = max(Decimal(0), money(decimal(advance_cap, translate=translate) - decimal(prior_recovery, translate=translate), places, translate=translate))
        retention = min(money(period * retention_rate / 100, places, translate=translate), retention_left)
        recovery = min(money(period * recovery_rate / 100, places, translate=translate), recovery_left)
    if period >= 0 and retention + recovery + other > period:
        raise ValueError(_('Deductions cannot exceed the period gross.'))
    return {'cumulative_gross': gross, 'previous_gross': previous, 'period_gross': period,
            'retention_amount': retention, 'recovery_amount': recovery,
            'other_deductions': other, 'net_before_tax': period - retention - recovery - other}


def aging_bucket(due_date, as_of):
    if due_date is None:
        return 'undated'
    days = (as_of - due_date).days
    if days <= 0:
        return 'current'
    if days <= 30:
        return '1_30'
    if days <= 60:
        return '31_60'
    if days <= 90:
        return '61_90'
    return 'over_90'
