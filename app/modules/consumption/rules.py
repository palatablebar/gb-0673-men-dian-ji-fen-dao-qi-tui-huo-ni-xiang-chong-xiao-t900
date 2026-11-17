from app.coerce import instant
from app.reasons import reject
from app.ledger import (
    new_ledger, expire, apply_earn, apply_spend, apply_refund, finalize,
)


def replay(rows, lots, as_of):
    ledger = new_ledger(lots)
    priority = {'EARN': 0, 'SPEND': 1, 'REFUND': 2}
    for row in sorted(rows, key=lambda r: (instant(r['at']), priority[r['type']], r['id'])):
        at = instant(row['at'])
        expire(ledger, at)
        if row['type'] == 'EARN':
            error = apply_earn(ledger, row)
        elif row['type'] == 'SPEND':
            error = apply_spend(ledger, row)
        else:
            _, error = apply_refund(ledger, row, at)
        if error:
            ledger['errors'].append((row, *error))
    expire(ledger, instant(as_of))
    return finalize(ledger)


def apply(state):
    result = replay(state['records'], state['lots'], state['context']['as_of'])
    rejected = {r['id'] for r, _, _ in result.pop('errors')}
    # 错误事件独立重放后按编号补充原因，保证失败消费不扣任何积分。
    detailed = replay(state['records'], state['lots'], state['context']['as_of'])['errors']
    for row, code, detail in detailed:
        reject(state, row, code, detail)
    state['records'] = [r for r in state['records'] if r['id'] not in rejected]
    state['point_ledger'] = result
    state['views']['consumption'] = {
        'title': '消费与到期', 'rows': result['ledger'],
        'metrics': {'消费笔': len(result['spends']), '自然到期': result['expired'],
                    '可用积分': sum(x['remaining'] for x in result['lots'])},
    }
