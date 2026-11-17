import hashlib, json


def _closed_balances(state):
    balances = {}
    for lot in state['point_ledger']['lots']:
        balances[lot['member']] = balances.get(lot['member'], 0) + lot['remaining']
    return balances


def _expected_official(closed, postings):
    expected = dict(closed)
    for posting in postings:
        if not posting.get('reconcile', {}).get('passed'):
            continue
        member = posting['member']
        expected[member] = expected.get(member, 0) + posting.get('restored', 0)
    return expected


def decide(state):
    closed = _closed_balances(state)
    postings = state.get('late_postings') or []
    official = state.get('official_balances') or closed
    expected = _expected_official(closed, postings)
    # Divergence that no passed posting explains means the official map cannot
    # be reproduced from evidence, so archiving is blocked pending review.
    unexplained = {m: {'closed': closed.get(m, 0), 'official': official.get(m, 0)}
                   for m in sorted(set(official) | set(expected))
                   if official.get(m, 0) != expected.get(m, 0)}
    all_postings_balanced = all(p.get('reconcile', {}).get('passed') for p in postings)
    balanced = (state['balance']['row_balanced'] and state['balance']['points_balanced']
                and not state['refund_issues'] and all_postings_balanced
                and not unexplained)
    rows = [{'member': m, 'remaining': b} for m, b in sorted(official.items())]
    drift = {m: {'closed': closed.get(m, 0), 'official': b}
             for m, b in official.items() if closed.get(m, 0) != b}
    return {
        'status': '可归档' if balanced else '待复核', 'members': len(rows),
        'seal': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        'balances': rows,
        'closed_balances': [{'member': m, 'remaining': b} for m, b in sorted(closed.items())],
        'late_postings': len(postings),
        'drift': drift,
        'unexplained': unexplained,
    }


def apply(state):
    state['archive'] = decide(state)
    state['views']['archive'] = {
        'title': '期末归档', 'rows': state['archive']['balances'],
        'metrics': {'会员数': state['archive']['members'],
                    '可用积分': state['balance']['remaining'],
                    '状态': state['archive']['status']},
    }
