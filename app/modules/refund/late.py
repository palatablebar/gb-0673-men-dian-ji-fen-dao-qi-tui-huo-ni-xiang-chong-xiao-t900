"""Cross-period late refunds (M5 extension).

A store may receive a return after the period has closed and its closing
vouchers were already issued.  Two treatments are possible:

* ``重开上期`` — reopening the closed period is only defensible when the refund
  can still be matched to the original spend *and* the returned points can still
  be consumed (the originating lots are live).
* ``差额结转`` — otherwise the difference is carried into the current period.

Invariants, independent of the chosen treatment:

* every refund traces back to the same member's successful spend;
* points already expired inside that spend are booked as expiry loss and are
  never revived; only the still-valid part returns to current usable points;
* a candidate balance is published as the official balance only when
  reconciliation passes; on failure the previous official balance stays;
* retrying the same refund id replays the stored posting rather than posting
  twice; a different payload under the same id is a conflict;
* posted refunds are replayed onto the closed ledger in receipt order, so
  cumulative refund caps and lot balances stay correct across several postings.

The closed ledger itself (``state['point_ledger']``) is never mutated.
"""
import json, hashlib
from app.coerce import instant
from app.errors import BusinessError, require
from app.ledger import from_finalized, apply_refund, member_balance, reconcile_gate


PERIOD_MODELS = {'reopen': '重开上期', 'carryforward': '差额结转'}
REQUIRED = ('id', 'member', 'points', 'ref', 'at')


def normalize(refund):
    require(isinstance(refund, dict), 'BAD_REFUND', '退货单须为对象')
    missing = [f for f in REQUIRED if f not in refund]
    require(not missing, 'BAD_REFUND', f'退货单缺少字段 {",".join(missing)}')
    row = {**refund, 'id': str(refund['id']).strip(), 'member': str(refund['member']).strip(),
           'ref': str(refund['ref']).strip(), 'at': str(refund['at'])}
    require(row['id'], 'BAD_REFUND', '退货单编号不能为空')
    try:
        row['points'] = int(refund['points'])
    except (TypeError, ValueError):
        raise BusinessError('BAD_REFUND', '退货积分须为正整数')
    require(row['points'] > 0, 'BAD_REFUND', '退货积分须为正整数')
    try:
        row['_at'] = instant(row['at'])
    except BusinessError:
        raise BusinessError('BAD_REFUND', '退货时刻无效')
    return row


def digest(run_id, refund):
    body = {'run_id': run_id, 'refund': {k: refund.get(k) for k in REQUIRED}}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def closing_balances(state):
    """Closing balances of the closed ledger, before any late posting."""
    balances = {}
    for lot in state['point_ledger']['lots']:
        balances[lot['member']] = balances.get(lot['member'], 0) + lot['remaining']
    return balances


def period_boundary(state):
    return instant(state['context']['as_of'])


def _as_rows(mapping):
    return [{'member': m, 'balance': b} for m, b in sorted(mapping.items())]


def _ledger_balances(ledger):
    members = sorted({r['member'] for r in ledger['lots'].values()})
    return {m: member_balance(ledger, m) for m in members}


def _base_ledger(state, posted):
    """Closed ledger with every already-posted late refund replayed in order."""
    ledger = from_finalized(state['point_ledger'])
    for entry in posted:
        source = entry.get('input') or {}
        if not source:
            continue
        apply_refund(ledger, {
            'id': entry['refund_id'], 'member': entry['member'], 'type': 'REFUND',
            'points': entry['points'], 'at': source['at'], 'ref': entry['ref'], '_line': 0,
        }, instant(source['at']))
    return ledger


def decide_model(state, refund, ledger=None):
    """Pick the period treatment and return the evidence behind the choice."""
    ledger = ledger or from_finalized(state['point_ledger'])
    original = ledger['spends'].get(refund.get('ref', ''))
    late = refund['_at'] > period_boundary(state)
    if original is None or original['member'] != refund['member']:
        return {'model': 'carryforward', 'label': PERIOD_MODELS['carryforward'],
                'late': late, 'reopenable': False, 'reason': 'REFUND_REFERENCE',
                'detail': '无法追溯到同会员原消费，只结转差额并隔离'}
    live = [x for x in original['allocations']
            if instant(ledger['lots'][x['lot']]['expires']) > refund['_at']]
    live_points = sum(x['points'] - x['refunded'] for x in live)
    if late and live_points >= refund['points']:
        return {'model': 'reopen', 'label': PERIOD_MODELS['reopen'], 'late': True,
                'reopenable': True, 'reason': 'REOPENABLE',
                'detail': '原批次仍可消费，可重开上期核对'}
    if late:
        return {'model': 'carryforward', 'label': PERIOD_MODELS['carryforward'],
                'late': True, 'reopenable': False, 'reason': 'EXPIRED_OR_PARTIAL',
                'detail': '原批次已过期或不足，差额结转到当前期'}
    return {'model': 'carryforward', 'label': '当期回拨',
            'late': False, 'reopenable': True, 'reason': 'CURRENT_PERIOD',
            'detail': '当期退货，直接回拨'}


def _recorded(ledger, refund_id):
    for record in ledger['refunds']:
        if record['id'] == refund_id:
            return record
    return None


def _same_refund(record, row):
    return (record['member'] == row['member'] and record['ref'] == row['ref']
            and record['points'] == row['points'])


def _assemble(state, row, decision, record, previous, published, gate,
              error=None, replayed=False):
    restored = record['restored'] if record else 0
    expired_refund = record['expired_refund'] if record else 0
    if error is not None:
        restored = expired_refund = 0
        decision = {'model': 'carryforward', 'label': PERIOD_MODELS['carryforward'],
                    'late': decision.get('late'), 'reopenable': False,
                    'reason': error[0], 'detail': error[1]}
    applied = bool(gate['passed'] and error is None and not replayed)
    if not applied:
        published = dict(previous)
    result = {
        'run_id': state['manifest']['run_id'], 'refund_id': row['id'],
        'member': row['member'], 'ref': row['ref'], 'points': row['points'],
        'model': decision['model'], 'model_label': decision['label'],
        'decision': decision,
        'restored': restored, 'expired_refund': expired_refund,
        'delta': 0 if replayed else restored,
        'allocations': record['allocations'] if record else [],
        'previous_balances': _as_rows(previous),
        'official_balances': _as_rows(published),
        'reconcile': gate,
        'status': '已存在' if replayed else ('已入账' if applied else '未切换'),
        'applied': applied, 'replayed': replayed,
        'input': {k: row[k] for k in REQUIRED},
        'trace': {
            'refund_id': row['id'], 'spend_id': row['ref'], 'member': row['member'],
            'refund_at': row['at'],
            'allocations': [dict(x) for x in (record['allocations'] if record else [])],
        },
        'payload_digest': digest(state['manifest']['run_id'], row),
    }
    if error is not None:
        result['error'] = {'code': error[0], 'detail': error[1]}
    return result


def apply_late_refund(state, refund, posted=None):
    """Compute one candidate posting without touching the closed ledger.

    Returns previous and candidate ``official_balances`` plus the reconcile
    evidence.  The caller persists it through ``store.post_late_refund``, which
    promotes only when ``reconcile.passed`` is true.
    """
    posted = posted or []
    row = normalize(refund)
    ledger = _base_ledger(state, posted)
    previous = _ledger_balances(ledger)

    existing = next((p for p in posted if p['refund_id'] == row['id']), None)
    if existing is not None:
        if existing.get('payload_digest') not in (None, digest(state['manifest']['run_id'], row)):
            raise BusinessError('REFUND_CONFLICT', '同一退货单的提交内容与已入账记录不一致', 409)
        record = _recorded(ledger, row['id'])
        gate = reconcile_gate(ledger)
        return _assemble(state, row, existing['decision'], record, previous, previous,
                         gate, replayed=True)

    # A same-id refund may already exist inside the closed ledger itself.
    booked = _recorded(ledger, row['id'])
    if booked is not None:
        if not _same_refund(booked, row):
            raise BusinessError('REFUND_CONFLICT', '同一退货单的提交内容与已入账记录不一致', 409)
        return _assemble(state, row, decide_model(state, row, ledger), booked, previous,
                         previous, reconcile_gate(ledger), replayed=True)

    decision = decide_model(state, row, ledger)
    record, error = apply_refund(ledger, {
        'id': row['id'], 'member': row['member'], 'type': 'REFUND',
        'points': row['points'], 'at': row['at'], 'ref': row['ref'],
        '_line': refund.get('_line', 0),
    }, row['_at'])
    gate = reconcile_gate(ledger)
    published = _ledger_balances(ledger) if gate['passed'] and error is None else previous
    return _assemble(state, row, decision, record, previous, published, gate, error=error)


def preview(state, refunds, posted=None):
    """Evaluate a batch on one ledger snapshot, in order, without posting.

    Returns per-refund rows, the reconciliation gate and the resulting balance
    map.  Nothing here mutates persisted state.
    """
    posted = posted or []
    ledger = _base_ledger(state, posted)
    previous = _ledger_balances(ledger)
    cursor = dict(previous)
    rows = []
    seen = {p['refund_id'] for p in posted}
    for refund in refunds:
        try:
            row = normalize(refund)
        except BusinessError as error:
            rows.append({'id': str(refund.get('id', '')), 'member': str(refund.get('member', '')),
                         'ref': str(refund.get('ref', '')), 'points': refund.get('points'),
                         'model': 'carryforward', 'model_label': PERIOD_MODELS['carryforward'],
                         'decision': {'model': 'carryforward', 'label': PERIOD_MODELS['carryforward'],
                                      'reason': error.code, 'detail': error.message},
                         'restored': 0, 'expired_refund': 0, 'delta': 0,
                         'balance': cursor.get(str(refund.get('member', '')), 0),
                         'error': {'code': error.code, 'detail': error.message}})
            continue
        decision = decide_model(state, row, ledger)
        if row['id'] in seen:
            record = _recorded(ledger, row['id'])
            rows.append({'id': row['id'], 'member': row['member'], 'ref': row['ref'],
                         'points': row['points'], 'model': decision['model'],
                         'model_label': decision['label'], 'decision': decision,
                         'restored': record['restored'] if record else 0,
                         'expired_refund': record['expired_refund'] if record else 0,
                         'delta': 0, 'balance': cursor.get(row['member'], 0),
                         'replayed': True, 'error': None})
            continue
        booked = _recorded(ledger, row['id'])
        if booked is not None:
            error = None if _same_refund(booked, row) else ('REFUND_CONFLICT', '同一退货单的提交内容与已入账记录不一致')
            rows.append({'id': row['id'], 'member': row['member'], 'ref': row['ref'],
                         'points': row['points'], 'model': decision['model'],
                         'model_label': decision['label'], 'decision': decision,
                         'restored': booked['restored'] if error is None else 0,
                         'expired_refund': booked['expired_refund'] if error is None else 0,
                         'delta': 0, 'balance': cursor.get(row['member'], 0),
                         'replayed': error is None,
                         'error': {'code': error[0], 'detail': error[1]} if error else None})
            continue
        record, error = apply_refund(ledger, {
            'id': row['id'], 'member': row['member'], 'type': 'REFUND',
            'points': row['points'], 'at': row['at'], 'ref': row['ref'],
            '_line': refund.get('_line', 0),
        }, row['_at'])
        restored = record['restored'] if record and error is None else 0
        if error is None:
            cursor[row['member']] = cursor.get(row['member'], 0) + restored
        seen.add(row['id'])
        rows.append({
            'id': row['id'], 'member': row['member'], 'ref': row['ref'],
            'points': row['points'], 'model': decision['model'],
            'model_label': decision['label'], 'decision': decision,
            'restored': restored,
            'expired_refund': record['expired_refund'] if record else 0,
            'delta': restored, 'balance': cursor.get(row['member'], 0),
            'replayed': False,
            'error': {'code': error[0], 'detail': error[1]} if error else None,
        })
    return rows, reconcile_gate(ledger), _as_rows(cursor)
