from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app.errors import BusinessError
from app import store
from app.modules.refund.late import (
    apply_late_refund, preview, closing_balances, period_boundary, PERIOD_MODELS,
)

router = APIRouter()


def _db():
    from app.main import DB_PATH
    return DB_PATH


def _state(run_id):
    return store.get(_db(), run_id)


def _official(run_id):
    return store.balances(_db(), run_id) or None


def _posted(run_id):
    return store.posted_refunds(_db(), run_id)


def _balance_rows(mapping):
    return [{'member': m, 'balance': b} for m, b in sorted(mapping.items())]


@router.get('/refund')
def page(request: Request):
    return render(request, 'refund')


@router.post('/api/refund')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=5)


@router.get('/api/runs/{run_id}/late-refunds')
def late_refunds(run_id: str):
    """Current period view: closing vs official balances and posted refunds."""
    state = _state(run_id)
    if 'point_ledger' not in state:
        raise BusinessError('INCOMPLETE_RUN', '批次尚未计算到退货回拨')
    official = _official(run_id) or closing_balances(state)
    _, gate, _ = preview(state, [], _posted(run_id))
    return {
        'run_id': run_id,
        'period_boundary': period_boundary(state).isoformat(),
        'models': PERIOD_MODELS,
        'closing_balances': _balance_rows(closing_balances(state)),
        'official_balances': _balance_rows(official),
        'posted': _posted(run_id),
        'reconcile': gate,
    }


@router.post('/api/runs/{run_id}/late-refunds/preview')
def late_refund_preview(run_id: str, payload: dict = Body(default={})):
    """Evaluate refunds without posting; returns per-row delta, expiry loss, balance."""
    state = _state(run_id)
    if 'point_ledger' not in state:
        raise BusinessError('INCOMPLETE_RUN', '批次尚未计算到退货回拨')
    refunds = payload.get('refunds')
    if refunds is None:
        refunds = [payload] if payload.get('id') else []
    if not isinstance(refunds, list):
        raise BusinessError('BAD_REFUND', '退货单须为数组')
    rows, gate, balances = preview(state, refunds, _posted(run_id))
    return {
        'run_id': run_id,
        'period_boundary': period_boundary(state).isoformat(),
        'rows': rows,
        'reconcile': gate,
        'official_balances': balances,
    }


@router.post('/api/runs/{run_id}/late-refunds')
def late_refund_post(run_id: str, payload: dict = Body(...)):
    """Post one late refund.

    The candidate is reconciled first.  Only a passed candidate is stored and
    promoted; otherwise the request fails and the old official balance stays.
    Repeating the same refund id returns the stored posting (``replayed``).
    """
    state = _state(run_id)
    if 'point_ledger' not in state:
        raise BusinessError('INCOMPLETE_RUN', '批次尚未计算到退货回拨')
    refund = payload.get('refund', payload)
    result = apply_late_refund(state, refund, posted=_posted(run_id))
    if result.get('error'):
        raise BusinessError(result['error']['code'], result['error']['detail'])
    if result.get('replayed'):
        # Already booked (either in the closed ledger or by an earlier posting):
        # never write it again, just report the recorded allocation.
        stored = result
    else:
        stored = store.post_late_refund(_db(), run_id, result['refund_id'],
                                        result['payload_digest'], result)
    official = store.balances(_db(), run_id)
    if official:
        stored['official_balances'] = _balance_rows(official)
    return stored
