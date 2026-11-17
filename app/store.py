import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS late_postings (run_id TEXT NOT NULL, refund_id TEXT NOT NULL, payload_digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, PRIMARY KEY(run_id,refund_id))")
    con.execute("CREATE TABLE IF NOT EXISTS official_balances (run_id TEXT NOT NULL, member TEXT NOT NULL, balance INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(run_id,member))")
    return con

def _now():
    return datetime.now(timezone.utc).isoformat()


def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        official={r['member']:r['balance'] for r in con.execute('SELECT member,balance FROM official_balances WHERE run_id=?',(run_id,))}
        posted=[json.loads(r['result']) for r in con.execute('SELECT result FROM late_postings WHERE run_id=? ORDER BY rowid',(run_id,))]
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    if official or posted:
        # Fold the late postings back into the balance view so the KPIs, the
        # reconciliation panel and the archive all describe the same official
        # balance.  The closed ledger itself is not rewritten.
        result['official_balances']=official
        result['late_postings']=posted
        if 'archive' in result:
            from importlib import import_module
            import_module('app.modules.reconcile.rules').apply(result)
            import_module('app.modules.archive.rules').apply(result)
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def balances(path,run_id):
    with connect(path) as con:
        return {r['member']:r['balance'] for r in con.execute('SELECT member,balance FROM official_balances WHERE run_id=? ORDER BY member',(run_id,))}

def posted_refunds(path,run_id):
    """Promoted late-refund postings in receipt order (for ledger replay)."""
    with connect(path) as con:
        rows=con.execute('SELECT result FROM late_postings WHERE run_id=? ORDER BY rowid',(run_id,)).fetchall()
    return [json.loads(r['result']) for r in rows]

def posting(path,run_id,refund_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM late_postings WHERE run_id=? AND refund_id=?',(run_id,refund_id)).fetchone()
    if not row: return None
    result=json.loads(row['result']);result.update(revision=row['revision'],created_at=row['created_at'],payload_digest=row['payload_digest'])
    return result

def post_late_refund(path,run_id,refund_id,payload_digest,result):
    """Persist a late-refund posting and, only when reconciliation passed,
    promote the candidate official balance.

    One transaction: either the posting and the whole balance map are written
    together, or nothing is.  A failed switch therefore leaves the previous
    official balance in force.

    Idempotent per (run_id, refund_id): an identical retry replays the stored
    record without re-posting, while a different payload under the same id is a
    conflict.  A posting that fails reconciliation is rejected and not stored,
    so the official balance cannot drift.
    """
    with connect(path) as con:
        try:
            existing=con.execute('SELECT * FROM late_postings WHERE run_id=? AND refund_id=?',(run_id,refund_id)).fetchone()
            if existing:
                if existing['payload_digest']!=payload_digest:
                    raise BusinessError('REFUND_CONFLICT','同一退货单的提交内容与已入账记录不一致',409)
                stored=json.loads(existing['result'])
                stored.update(revision=existing['revision'],created_at=existing['created_at'],
                              payload_digest=existing['payload_digest'],replayed=True,applied=False)
                return stored
            if not result['reconcile']['passed']:
                raise BusinessError('RECONCILE_BLOCKED','对账未通过，保留原余额',409)
            current={r['member']:r['balance'] for r in con.execute('SELECT member,balance FROM official_balances WHERE run_id=?',(run_id,))}
            expected={r['member']:r['balance'] for r in result['previous_balances']}
            if current and current!=expected:
                raise BusinessError('STALE_BALANCE','官方余额已被其他操作更新，请重新预览后提交',409)
            con.execute('INSERT INTO late_postings(run_id,refund_id,payload_digest,result,created_at) VALUES(?,?,?,?,?)',
                        (run_id,refund_id,payload_digest,json.dumps(result,ensure_ascii=False),_now()))
            now=_now()
            for row in result['official_balances']:
                con.execute('INSERT INTO official_balances(run_id,member,balance,revision,updated_at) VALUES(?,?,?,1,?) '
                            'ON CONFLICT(run_id,member) DO UPDATE SET balance=excluded.balance,revision=official_balances.revision+1,updated_at=excluded.updated_at',
                            (run_id,row['member'],row['balance'],now))
            con.commit()
        except Exception:
            con.rollback()
            raise
    return posting(path,run_id,refund_id)



