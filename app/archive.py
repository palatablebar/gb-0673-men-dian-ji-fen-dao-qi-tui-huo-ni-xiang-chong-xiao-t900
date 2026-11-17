import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        evidence={'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']}
        # Cross-period late refunds are part of the auditable result: keep the
        # per-refund delta, expiry loss, trace and reconcile outcome in the bundle.
        if result.get('late_postings'):
            evidence['late_postings']=result['late_postings']
            evidence['official_balances']=result.get('official_balances')
            evidence['archive']=result.get('archive')
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
    return buffer.getvalue()
