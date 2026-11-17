import os, pytest
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.refund import late


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'late.sqlite'))
    client = TestClient(main.app)
    run = client.post('/api/runs', json={}).json()
    return client, run['manifest']['run_id']


def post(client, run_id, **refund):
    return client.post(f'/api/runs/{run_id}/late-refunds', json={'refund': refund})


def preview(client, run_id, **refund):
    return client.post(f'/api/runs/{run_id}/late-refunds/preview', json={'refunds': [refund]})


def balance(client, run_id, member='00001'):
    rows = client.get(f'/api/runs/{run_id}/late-refunds').json()['official_balances']
    return next(r['balance'] for r in rows if r['member'] == member)


def test_expired_part_of_late_refund_is_loss_not_revived(api):
    client, run_id = api
    assert balance(client, run_id) == 30
    body = preview(client, run_id, id='RL1', member='00001', points=20, ref='S1',
                   at='2026-11-10T09:00:00+08:00').json()['rows'][0]
    assert body['model'] == 'carryforward'
    assert body['restored'] == 0 and body['expired_refund'] == 20
    assert body['balance'] == 30
    posted = post(client, run_id, id='RL1', member='00001', points=20, ref='S1',
                  at='2026-11-10T09:00:00+08:00').json()
    assert posted['status'] == '已入账'
    assert posted['delta'] == 0 and posted['expired_refund'] == 20
    assert balance(client, run_id) == 30


def test_live_lot_late_refund_reopens_prior_period(api):
    client, run_id = api
    posted = post(client, run_id, id='RL2', member='00001', points=20, ref='S2',
                  at='2026-11-10T09:00:00+08:00').json()
    assert posted['model'] == 'reopen' and posted['model_label'] == '重开上期'
    assert posted['delta'] == 20 and posted['expired_refund'] == 0
    assert balance(client, run_id) == 50


def test_refund_traces_to_original_spend(api):
    client, run_id = api
    posted = post(client, run_id, id='RL3', member='00001', points=20, ref='S2',
                  at='2026-11-10T09:00:00+08:00').json()
    assert posted['trace']['spend_id'] == 'S2'
    assert [x['lot'] for x in posted['trace']['allocations']] == ['E2']
    assert [x['lot'] for x in posted['allocations']] == ['E2']


def test_retry_does_not_double_post(api):
    client, run_id = api
    first = post(client, run_id, id='RL4', member='00001', points=20, ref='S2',
                 at='2026-11-10T09:00:00+08:00').json()
    again = post(client, run_id, id='RL4', member='00001', points=20, ref='S2',
                 at='2026-11-10T09:00:00+08:00').json()
    assert first['applied'] is True and again['replayed'] is True
    assert again['applied'] is False
    assert balance(client, run_id) == 50
    assert len(store.posted_refunds(main.DB_PATH, run_id)) == 1


def test_same_id_different_payload_conflicts(api):
    client, run_id = api
    post(client, run_id, id='RL5', member='00001', points=20, ref='S2',
         at='2026-11-10T09:00:00+08:00')
    clash = post(client, run_id, id='RL5', member='00001', points=21, ref='S2',
                 at='2026-11-10T09:00:00+08:00')
    assert clash.status_code == 409 and clash.json()['code'] == 'REFUND_CONFLICT'
    assert balance(client, run_id) == 50


def test_refund_already_in_closed_ledger_is_not_reposted(api):
    client, run_id = api
    # R1 is already booked inside the closed ledger; reposting it must not count twice.
    response = post(client, run_id, id='R1', member='00001', points=30, ref='S1',
                    at='2026-11-02T09:00:00+08:00')
    assert response.status_code == 200
    body = response.json()
    assert body['replayed'] is True and body['applied'] is False and body['delta'] == 0
    assert store.posted_refunds(main.DB_PATH, run_id) == []
    assert balance(client, run_id) == 30
    clash = post(client, run_id, id='R1', member='00001', points=31, ref='S1',
                 at='2026-11-02T09:00:00+08:00')
    assert clash.status_code == 409


def test_failed_reconcile_keeps_old_balance(api, monkeypatch):
    client, run_id = api
    original = late.reconcile_gate

    def broken(ledger):
        return {**original(ledger), 'passed': False, 'points_balanced': False}

    monkeypatch.setattr(late, 'reconcile_gate', broken)
    blocked = post(client, run_id, id='RL6', member='00001', points=20, ref='S2',
                   at='2026-11-10T09:00:00+08:00')
    assert blocked.status_code == 409 and blocked.json()['code'] == 'RECONCILE_BLOCKED'
    assert store.balances(main.DB_PATH, run_id) == {}
    assert store.posted_refunds(main.DB_PATH, run_id) == []

    monkeypatch.setattr(late, 'reconcile_gate', original)
    retried = post(client, run_id, id='RL6', member='00001', points=20, ref='S2',
                   at='2026-11-10T09:00:00+08:00').json()
    assert retried['status'] == '已入账' and balance(client, run_id) == 50


def test_multiple_postings_share_cumulative_cap(api):
    client, run_id = api
    first = post(client, run_id, id='RL7', member='00001', points=20, ref='S2',
                 at='2026-11-10T09:00:00+08:00')
    assert first.status_code == 200
    over = post(client, run_id, id='RL8', member='00001', points=40, ref='S2',
                at='2026-11-11T09:00:00+08:00')
    assert over.status_code == 422 and over.json()['code'] == 'REFUND_CAP'
    assert balance(client, run_id) == 50


def test_invalid_refunds_rejected(api):
    client, run_id = api
    wrong_member = post(client, run_id, id='RA', member='00009', points=5, ref='S1',
                        at='2026-11-10T09:00:00+08:00')
    assert wrong_member.status_code == 422 and wrong_member.json()['code'] == 'REFUND_REFERENCE'
    bad_points = post(client, run_id, id='RB', member='00001', points=0, ref='S2',
                      at='2026-11-10T09:00:00+08:00')
    assert bad_points.json()['code'] == 'BAD_REFUND'
    bad_time = post(client, run_id, id='RC', member='00001', points=5, ref='S2', at='nope')
    assert bad_time.json()['code'] == 'BAD_REFUND'
    assert balance(client, run_id) == 30


def test_partial_expiry_restores_only_live_part(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'split.sqlite'))
    client = TestClient(main.app)
    rows = [
        'id,member,type,points,at,expires,ref,rule,version',
        'E1,00001,EARN,100,2026-10-01T09:00:00+08:00,2026-11-01T00:00:00+08:00,,常规,1',
        'E2,00001,EARN,50,2026-10-02T09:00:00+08:00,2026-12-01T00:00:00+08:00,,常规,1',
        'S1,00001,SPEND,120,2026-10-15T09:00:00+08:00,,,常规,1',
        'S2,00001,SPEND,20,2026-10-16T09:00:00+08:00,,,常规,1',
    ]
    run = client.post('/api/runs', json={'csv_text': '\n'.join(rows)}).json()
    run_id = run['manifest']['run_id']
    # S1 drew E1 100 + E2 20; S2 drew the last E2 10. A 60 refund reverses the
    # live E2 20 then the expired E1 40: 20 comes back, 40 is expiry loss.
    body = client.post(f'/api/runs/{run_id}/late-refunds/preview',
                       json={'refunds': [{'id': 'RP', 'member': '00001', 'points': 60,
                                          'ref': 'S1',
                                          'at': '2026-11-10T09:00:00+08:00'}]}).json()['rows'][0]
    assert body['model'] == 'carryforward'
    assert body['restored'] == 20 and body['expired_refund'] == 40
    assert body['restored'] + body['expired_refund'] == body['points']
    assert body['balance'] == 30  # 10 left from S2 + 20 restored


def test_store_rolls_back_stale_balance(api):
    client, run_id = api
    state = store.get(main.DB_PATH, run_id)
    first = late.apply_late_refund(state, {'id': 'RS1', 'member': '00001', 'points': 20,
                                           'ref': 'S2', 'at': '2026-11-10T09:00:00+08:00'})
    store.post_late_refund(main.DB_PATH, run_id, first['refund_id'],
                           first['payload_digest'], first)
    snapshot = {r['member']: r['balance'] for r in store.post_late_refund(
        main.DB_PATH, run_id, first['refund_id'], first['payload_digest'], first)['official_balances']}
    # A second posting computed from the pre-first-posting snapshot must be refused.
    stale = late.apply_late_refund(state, {'id': 'RS2', 'member': '00001', 'points': 10,
                                           'ref': 'S2', 'at': '2026-11-11T09:00:00+08:00'})
    stale['previous_balances'] = first['previous_balances']
    with pytest.raises(Exception) as error:
        store.post_late_refund(main.DB_PATH, run_id, 'RS2', stale['payload_digest'], stale)
    assert getattr(error.value, 'code', '') == 'STALE_BALANCE'
    assert store.balances(main.DB_PATH, run_id) == snapshot
    assert [p['refund_id'] for p in store.posted_refunds(main.DB_PATH, run_id)] == ['RS1']


def test_page_shows_delta_loss_and_balance(api):
    client, run_id = api
    post(client, run_id, id='RL-page', member='00001', points=20, ref='S2',
         at='2026-11-10T09:00:00+08:00')
    html = client.get(f'/refund?run_id={run_id}').text
    assert '跨期迟到退货' in html and 'RL-page' in html
    assert '本笔差额' in html and '过期损失' in html and '当前余额' in html
    assert '重开上期' in html and '<b>50</b>' in html


def test_archive_export_contains_late_postings(api):
    import io, json, zipfile
    client, run_id = api
    post(client, run_id, id='RL-export', member='00001', points=20, ref='S2',
         at='2026-11-10T09:00:00+08:00')
    archive = zipfile.ZipFile(io.BytesIO(client.get(f'/api/export/{run_id}').content))
    evidence = json.loads(archive.read('evidence.json'))
    assert [p['refund_id'] for p in evidence['late_postings']] == ['RL-export']
    assert evidence['late_postings'][0]['trace']['spend_id'] == 'S2'
    assert evidence['archive']['status'] == '可归档'
    assert any(r['member'] == '00001' and r['remaining'] == 50
               for r in evidence['archive']['balances'])


def test_official_balance_tamper_blocks_archive(api):
    import sqlite3
    client, run_id = api
    post(client, run_id, id='RL-tamper', member='00001', points=20, ref='S2',
         at='2026-11-10T09:00:00+08:00')
    con = sqlite3.connect(main.DB_PATH)
    con.execute("UPDATE official_balances SET balance=999 WHERE run_id=? AND member='00001'", (run_id,))
    con.commit(); con.close()
    archive = client.get(f'/api/runs/{run_id}').json()['archive']
    assert archive['status'] == '待复核'
    assert archive['unexplained']['00001'] == {'closed': 30, 'official': 999}


def test_clean_dataset_official_balances_match_archive(api):
    client, _ = api
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    official = client.get(f"/api/runs/{run['manifest']['run_id']}/late-refunds").json()['official_balances']
    assert official == [{'member': r['member'], 'balance': r['remaining']}
                        for r in run['archive']['balances']]
