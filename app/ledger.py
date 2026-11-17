"""Incremental point-ledger primitives shared by consumption replay and late refunds.

The ledger is intentionally a plain dict so it can be deep-copied for the
"candidate -> reconcile -> promote/rollback" switch used by late refunds.
"""
from copy import deepcopy
from app.coerce import instant


def new_ledger(lots):
    """Build a mutable ledger from issued lots (all remaining points start at 0)."""
    return {
        'lots': {r['id']: dict(r, remaining=0) for r in lots},
        'spends': {},
        'refunds': [],
        'entries': [],
        'expired': 0,
        'errors': [],
    }


def clone(ledger):
    return deepcopy(ledger)


def from_finalized(final):
    """Rebuild a mutable ledger from a finalized point_ledger result.

    Used by late refunds, which must anchor on the closed-period ledger without
    re-expiring unrelated lots.
    """
    return {
        'lots': {r['id']: deepcopy(r) for r in final['lots']},
        'spends': {r['id']: deepcopy(r) for r in final['spends']},
        'refunds': [deepcopy(r) for r in final['refunds']],
        'entries': [deepcopy(r) for r in final.get('ledger', [])],
        'expired': final.get('expired', 0),
        'errors': [],
    }


def member_balance(ledger, member):
    return sum(r['remaining'] for r in ledger['lots'].values() if r['member'] == member)


def expire(ledger, at):
    """Expire every live lot whose half-open availability window has closed."""
    for lot in ledger['lots'].values():
        if instant(lot['expires']) <= at and lot['remaining']:
            amount = lot['remaining']
            lot['remaining'] = 0
            ledger['expired'] += amount
            ledger['entries'].append({
                'id': lot['id'], 'member': lot['member'], 'action': 'EXPIRE',
                'points': amount, 'at': at.isoformat(),
            })


def apply_earn(ledger, row):
    lot = ledger['lots'].get(row['id'])
    if lot is None:
        return ('UNKNOWN_LOT', '发放批次不存在')
    lot['remaining'] = row['points']
    ledger['entries'].append({
        'id': row['id'], 'member': row['member'], 'action': 'EARN',
        'points': row['points'], 'at': row['at'],
    })
    return None


def apply_spend(ledger, row):
    member = row['member']
    amount = row['points']
    available = sorted(
        (r for r in ledger['lots'].values() if r['member'] == member and r['remaining'] > 0),
        key=lambda r: (r['expires'], r['at'], r['id']),
    )
    if sum(r['remaining'] for r in available) < amount:
        return ('INSUFFICIENT_POINTS', '余额不足，消费未落账')
    allocations = []
    rest = amount
    for lot in available:
        used = min(rest, lot['remaining'])
        lot['remaining'] -= used
        rest -= used
        allocations.append({
            'lot': lot['id'], 'points': used, 'refunded': 0, 'expires': lot['expires'],
        })
        if rest == 0:
            break
    ledger['spends'][row['id']] = {
        'id': row['id'], 'member': member, 'points': amount, 'refunded': 0,
        'allocations': allocations, 'at': row['at'],
    }
    ledger['entries'].append({
        'id': row['id'], 'member': member, 'action': 'SPEND',
        'points': amount, 'at': row['at'],
    })
    return None


def find_original(ledger, row):
    original = ledger['spends'].get(row.get('ref', ''))
    if original is None or original['member'] != row['member']:
        return None
    return original


def apply_refund(ledger, row, at=None):
    """Apply a refund to its original spend. Returns (record, error).

    Expired allocations are recorded as expiry loss and are never revived.
    """
    at = at or instant(row['at'])
    original = find_original(ledger, row)
    if original is None:
        return None, ('REFUND_REFERENCE', '退款未关联同会员有效消费')
    amount = row['points']
    if amount > original['points'] - original['refunded']:
        return None, ('REFUND_CAP', '累计退款超过原消费')
    restored = 0
    expired_refund = 0
    allocations = []
    rest = amount
    for allocation in reversed(original['allocations']):
        count = min(rest, allocation['points'] - allocation['refunded'])
        if not count:
            continue
        allocation['refunded'] += count
        rest -= count
        lot = ledger['lots'][allocation['lot']]
        is_expired = instant(lot['expires']) <= at
        if is_expired:
            expired_refund += count
        else:
            lot['remaining'] += count
            restored += count
        allocations.append({'lot': lot['id'], 'points': count, 'expired': is_expired})
        if not rest:
            break
    original['refunded'] += amount
    record = {
        'id': row['id'], 'member': row['member'], 'ref': row['ref'], 'points': amount,
        'restored': restored, 'expired_refund': expired_refund,
        'allocations': allocations, 'line': row.get('_line', 0), 'at': row['at'],
    }
    ledger['refunds'].append(record)
    ledger['entries'].append({
        'id': row['id'], 'member': row['member'], 'action': 'REFUND',
        'points': restored, 'at': row['at'],
    })
    return record, None


def totals(ledger):
    issued = sum(r['issued'] for r in ledger['lots'].values())
    remaining = sum(r['remaining'] for r in ledger['lots'].values())
    spent = sum(r['points'] for r in ledger['spends'].values())
    refunded = sum(r['points'] for r in ledger['refunds'])
    expired = ledger['expired'] + sum(r['expired_refund'] for r in ledger['refunds'])
    return {
        'issued': issued, 'remaining': remaining, 'spent': spent,
        'refunded': refunded, 'expired': expired,
    }


def reconcile_gate(ledger):
    """Point-level reconciliation used before a candidate balance is promoted.

    Besides conservation this checks that every allocation is internally
    consistent, that no lot was refunded beyond what was issued and that no
    balance went negative.  The result is embedded in the audit record.
    """
    t = totals(ledger)
    allocations_valid = True
    for spend in ledger['spends'].values():
        if sum(x['points'] for x in spend['allocations']) != spend['points']:
            allocations_valid = False
        for x in spend['allocations']:
            if x['refunded'] < 0 or x['refunded'] > x['points']:
                allocations_valid = False
            if x['lot'] not in ledger['lots']:
                allocations_valid = False
    for refund in ledger['refunds']:
        if sum(x['points'] for x in refund['allocations']) != refund['points']:
            allocations_valid = False
        if refund['restored'] + refund['expired_refund'] != refund['points']:
            allocations_valid = False
        original = ledger['spends'].get(refund['ref'])
        if original is None or original['refunded'] > original['points']:
            allocations_valid = False
    balances_nonnegative = all(r['remaining'] >= 0 for r in ledger['lots'].values())
    over_issued = any(r['remaining'] > r['issued'] for r in ledger['lots'].values())
    points_balanced = t['issued'] == t['remaining'] + t['spent'] - t['refunded'] + t['expired']
    passed = bool(points_balanced and allocations_valid and balances_nonnegative and not over_issued)
    return {
        'passed': passed,
        'points_balanced': points_balanced,
        'allocations_valid': allocations_valid,
        'balances_nonnegative': balances_nonnegative,
        'within_issued': not over_issued,
        **t,
    }


def finalize(ledger):
    return {
        'lots': list(ledger['lots'].values()),
        'spends': list(ledger['spends'].values()),
        'refunds': ledger['refunds'],
        'ledger': ledger['entries'],
        'errors': ledger['errors'],
        'expired': ledger['expired'],
    }
